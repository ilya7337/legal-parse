"""Парсер и оценщик robots.txt по RFC 9309.

Реализация сознательно не использует внешние зависимости, чтобы правила
RFC были видны и тестируемы построчно.

Ключевые положения RFC 9309, которые здесь реализованы:
  * §2.2.1 — группировка правил по ``User-agent``; правила группы применяются,
    только если группа выбрана (наиболее специфичное совпадение product-token).
  * §2.2.2 — ``Allow`` и ``Disallow``; пустой ``Disallow:`` разрешает всё.
  * §2.2.3 — шаблоны ``*`` и ``$``; побеждает **самое длинное** совпадение,
    при равенстве длины ``Allow`` побеждает ``Disallow``.
  * §2.2.4 — ``Crawl-delay`` и (legacy) ``Request-rate``.
  * §2.3.1.2 — при 5xx robots.txt считается недоступным (краулер «должен»
    отказаться от обхода; мы работаем fail-closed).
  * §2.3.1.1 — при 401/403 доступ полностью запрещён.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


def _normalize_path(raw: str) -> tuple[str, str]:
    """Нормализовать правило/путь: вернуть (path, query).

    RFC 9309 §2.2.2: правила применяются к path + query.
    """
    if not raw:
        return "/", ""
    if raw.startswith("http://") or raw.startswith("https://"):
        parts = urlsplit(raw)
        path = parts.path or "/"
        query = parts.query
    else:
        path, _, query = raw.partition("?")
        if not path:
            path = "/"
    return path, query


def _rule_to_regex(pattern: str) -> re.Pattern[str]:
    """Преобразовать pattern robots.txt в regex.

    ``*`` = любая последовательность символов; завершающий ``$`` — конец пути.
    """
    path, query = _normalize_path(pattern)
    full = path + (f"?{query}" if query else "")
    anchors_end = full.endswith("$")
    if anchors_end:
        full = full[:-1]
    escaped = re.escape(full).replace(r"\*", ".*")
    if anchors_end:
        return re.compile(f"^{escaped}$", re.IGNORECASE)
    return re.compile(f"^{escaped}", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str
    length: int
    regex: re.Pattern[str]

    @classmethod
    def parse(cls, allow: bool, pattern: str) -> Rule:
        path, query = _normalize_path(pattern)
        full = path + (f"?{query}" if query else "")
        return cls(allow=allow, pattern=pattern, length=len(full), regex=_rule_to_regex(pattern))

    def matches(self, path_with_query: str) -> bool:
        return bool(self.regex.match(path_with_query))


@dataclass
class RobotsGroup:
    agents: list[str]
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None
    request_rate: tuple[float, float] | None = None  # (requests, seconds)


@dataclass
class RobotsTxt:
    """Разобранный robots.txt."""

    raw_text: str
    groups: list[RobotsGroup]
    status: int = 200
    source_url: str = ""
    sitemaps: list[str] = field(default_factory=list)
    unavailable: bool = False   # fail-closed: robots недоступен
    forbidden_all: bool = False  # полный запрет (401/403)

    # ---------- парсинг ----------

    @classmethod
    def parse(cls, text: str, *, status: int = 200, source_url: str = "") -> RobotsTxt:
        groups: list[RobotsGroup] = []
        sitemaps: list[str] = []
        current: RobotsGroup | None = None
        expecting_new_group = True

        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()

            if key == "user-agent":
                if current is None or not expecting_new_group:
                    current = RobotsGroup(agents=[])
                    groups.append(current)
                    expecting_new_group = True
                current.agents.append(value.lower())
                continue

            if key == "sitemap":
                if value:
                    sitemaps.append(value)
                continue

            if current is None:
                continue

            expecting_new_group = False

            if key == "disallow":
                if value:  # пустой Disallow: -> ничего не запрещает
                    current.rules.append(Rule.parse(False, value))
            elif key == "allow":
                if value:
                    current.rules.append(Rule.parse(True, value))
            elif key == "crawl-delay":
                try:
                    current.crawl_delay = max(0.0, float(value))
                except ValueError:
                    pass
            elif key == "request-rate":
                parts = re.split(r"[/\s]+", value)
                if len(parts) == 2:
                    try:
                        current.request_rate = (float(parts[0]), float(parts[1]))
                    except ValueError:
                        pass

        return cls(
            raw_text=text, groups=groups, status=status, source_url=source_url, sitemaps=sitemaps
        )

    # ---------- выбор группы ----------

    @staticmethod
    def _product_token(user_agent: str) -> str:
        # "LegitFeedBot/0.1 (+https://...)" -> "legitfeedbot"
        token = user_agent.strip().split()[0] if user_agent.strip() else ""
        return token.split("/")[0].lower()

    def _select_groups(self, user_agent: str) -> list[RobotsGroup]:
        """RFC 9309 §2.2.1: группы с самым специфичным (длинным) совпавшим токеном."""
        token = self._product_token(user_agent)
        best_len = -1
        selected: list[RobotsGroup] = []
        for group in self.groups:
            for agent in group.agents:
                a = agent.strip().lower()
                if not a:
                    continue
                matched = a == "*" or a in token
                if not matched:
                    continue
                specificity = 0 if a == "*" else len(a)
                if specificity > best_len:
                    best_len = specificity
                    selected = [group]
                elif specificity == best_len:
                    selected.append(group)
        return selected

    # ---------- запрос ----------

    def is_allowed(self, url_or_path: str, user_agent: str) -> bool:
        """Разрешён ли доступ к URL/пути для указанного User-Agent."""
        if self.forbidden_all:
            return False
        if self.unavailable:
            return False  # fail-closed

        parts = urlsplit(url_or_path)
        path = parts.path or "/"
        path_with_query = path + (f"?{parts.query}" if parts.query else "")

        groups = self._select_groups(user_agent)
        if not groups:
            return True  # нет правил для нас -> разрешено

        best: Rule | None = None
        for group in groups:
            for rule in group.rules:
                if not rule.matches(path_with_query):
                    continue
                if best is None or rule.length > best.length:
                    best = rule
                elif rule.length == best.length and rule.allow and not best.allow:
                    best = rule  # Allow побеждает при равенстве длины
        return True if best is None else best.allow

    def crawl_delay(self, user_agent: str) -> float | None:
        groups = self._select_groups(user_agent)
        delays = [g.crawl_delay for g in groups if g.crawl_delay is not None]
        return max(delays) if delays else None

    def request_rate(self, user_agent: str) -> tuple[float, float] | None:
        groups = self._select_groups(user_agent)
        rates = [g.request_rate for g in groups if g.request_rate is not None]
        if not rates:
            return None
        reqs, period = rates[0]  # type: ignore[misc]
        for r, p in rates:
            reqs = min(reqs, r)
            period = max(period, p)
        return reqs, period

    def min_delay_from_rate(self, user_agent: str) -> float | None:
        rr = self.request_rate(user_agent)
        if not rr:
            return None
        reqs, period = rr
        return period / reqs if reqs > 0 else None

    # ---------- удобные конструкторы ----------

    @classmethod
    def allow_all(cls, note: str = "no robots.txt") -> RobotsTxt:
        return cls(raw_text=f"# {note}", groups=[], status=200)

    @classmethod
    def unavailable_response(cls, status: int, url: str = "") -> RobotsTxt:
        if status in (401, 403):
            return cls(raw_text="", groups=[], status=status, source_url=url, forbidden_all=True)
        return cls(raw_text="", groups=[], status=status, source_url=url, unavailable=True)


def canonical_url(url: str) -> str:
    """Канонический URL для дедупликации: без фрагмента, нижний регистр хоста,
    без ``www.``, без trailing slash у пути."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))

