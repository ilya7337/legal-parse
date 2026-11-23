"""Правовой аудит источника: robots.txt + ToS + реестровые признаки.

Результат аудита — ``AuditResult`` с вердиктом ``allowed`` / ``denied`` и
человекочитаемыми причинами. Сборщик не имеет права обращаться к источнику,
если ``AuditResult.allowed is False``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from config.settings import get_settings
from legitfeed.legal.policy import LegalPolicy, load_policy
from legitfeed.legal.registry import Source
from legitfeed.legal.robots import RobotsTxt
from legitfeed.legal.tos import TosSignal, analyze_tos, verdict


@dataclass
class AuditResult:
    source_id: str
    allowed: bool
    robots_status: int | None
    robots_url: str
    robots_allowed_paths: dict[str, bool]
    crawl_delay: float | None
    request_rate: tuple[float, float] | None
    tos_signals: list[dict[str, str]] = field(default_factory=list)
    tos_verdict: str = "unknown"
    source_audited: bool = False
    reasons: list[str] = field(default_factory=list)
    checked_at: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["request_rate"] = list(self.request_rate) if self.request_rate else None
        return d


def _robots_url(site_url: str) -> str:
    parts = urlsplit(site_url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


class RobotsFetcher:
    """Загрузка и кэширование robots.txt.

    Кэш на диске важен не только для скорости: повторные запросы к robots.txt
    увеличивают нагрузку на чужой сервер, а также позволяют воспроизвести
    аудит offline (для демонстрации и тестов).
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        user_agent: str | None = None,
        timeout: float | None = None,
        offline: bool | None = None,
    ):
        settings = get_settings()
        self.cache_dir = cache_dir or settings.path("cache") / "robots"
        self.user_agent = user_agent or settings.user_agent
        self.timeout = timeout or settings.request_timeout
        self.offline = settings.offline if offline is None else offline

    def _cache_path(self, url: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in url)
        return self.cache_dir / f"{safe}.json"

    def get(self, site_url: str) -> RobotsTxt:
        url = _robots_url(site_url)
        cache = self._cache_path(url)
        if cache.exists():
            try:
                payload = json.loads(cache.read_text(encoding="utf-8"))
                status = payload.get("status", 200)
                if status in (401, 403) or status >= 500:
                    return RobotsTxt.unavailable_response(status=status, url=url)
                return RobotsTxt.parse(
                    payload.get("text", ""), status=status, source_url=url
                )
            except (json.JSONDecodeError, OSError):
                pass

        if self.offline:
            # offline: нет кэша -> robots недоступен -> fail-closed
            return RobotsTxt.unavailable_response(status=503, url=url)

        try:
            resp = requests.get(
                url,
                headers={"User-Agent": self.user_agent, "Accept": "text/plain"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            robots = RobotsTxt(
                raw_text=f"# network error: {exc}",
                groups=[],
                status=599,
                source_url=url,
                unavailable=True,
            )
            return robots

        if resp.status_code in (401, 403) or resp.status_code >= 500:
            return RobotsTxt.unavailable_response(status=resp.status_code, url=url)
        if resp.status_code >= 400:
            # 404 и прочие 4xx: robots.txt отсутствует -> ограничений нет (RFC 9309 §2.3.1.3)
            robots = RobotsTxt.allow_all(note=f"no robots.txt (HTTP {resp.status_code})")
            robots.source_url = url
            robots.status = resp.status_code
        else:
            robots = RobotsTxt.parse(resp.text, status=resp.status_code, source_url=url)

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps({"text": robots.raw_text, "status": robots.status}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
        return robots


def audit_source(
    source: Source,
    *,
    paths: list[str] | None = None,
    check_robots: bool = True,
    policy: LegalPolicy | None = None,
    robots_fetcher: RobotsFetcher | None = None,
    tos_text: str | None = None,
    strict: bool = True,
) -> AuditResult:
    """Провести правовой аудит одного источника.

    ``paths`` — URL/пути, проверяемые против robots.txt (для API и HTML-страниц).
    Для фидов по умолчанию берётся ``source.feed_url``. ``check_robots=False`` —
    пропустить robots.txt (для готовых датасетов, где robots неприменим).
    """
    policy = policy or load_policy()
    settings = get_settings()
    reasons: list[str] = []
    allowed = True

    # 1. Реестровые признаки
    if policy.terms_of_service.require_tos_allow_feed and not source.tos_allow_feed:
        allowed = False
        reasons.append("ToS: источник не подтвердил разрешение на чтение фида")
    if policy.terms_of_service.require_source_audit and strict and not source.is_audited:
        allowed = False
        reasons.append("Правовой аудит источника не проведён (approved_by/approved_at пусты)")
    if not source.license.strip():
        allowed = False
        reasons.append("Не указана лицензия контента источника")

    # 2. robots.txt (пропускается для готовых датасетов, check_robots=False)
    checked_paths: dict[str, bool] = {}
    robots_status: int | None = None
    robots_url = ""
    crawl_delay: float | None = None
    request_rate: tuple[float, float] | None = None

    if check_robots:
        fetcher = robots_fetcher or RobotsFetcher(
            user_agent=settings.user_agent, offline=settings.offline
        )
        robots = fetcher.get(str(source.site_url))
        robots_status = robots.status
        robots_url = robots.source_url or _robots_url(str(source.site_url))

        if paths is None:
            paths = [str(source.feed_url)] if source.feed_url else []
        paths_to_check = list(paths)
        if policy.robots.check_both_site_and_feed:
            paths_to_check.append(urljoin(str(source.site_url), "/"))

        for p in paths_to_check:
            ok = robots.is_allowed(p, settings.user_agent)
            checked_paths[p] = ok
            if not ok:
                allowed = False
                reasons.append(f"robots.txt запрещает доступ к {p} (RFC 9309)")

        if robots.unavailable and policy.robots.fail_closed_on_server_error:
            allowed = False
            reasons.append(
                f"robots.txt недоступен (HTTP {robots.status}); fail-closed: источник пропущен"
            )
        if robots.forbidden_all and policy.robots.disallow_on_auth_required:
            allowed = False
            reasons.append("robots.txt требует авторизации (401/403): доступ запрещён")

        crawl_delay = robots.crawl_delay(settings.user_agent) if policy.robots.honor_crawl_delay else None
        request_rate = (
            robots.request_rate(settings.user_agent) if policy.robots.honor_request_rate else None
        )

    # 3. ToS-эвристики по тексту (если передан)
    signals: list[TosSignal] = analyze_tos(tos_text) if tos_text else []
    tos_verdict = verdict(signals) if tos_text else "unknown"
    if tos_verdict == "block":
        allowed = False
        reasons.append("ToS-текст содержит запрет автоматизированного сбора (см. цитаты)")

    if source.contains_personal_data:
        reasons.append(
            "Источник публикует ПДн: включена минимизация и маскирование (152-ФЗ), "
            "поля с ПДн не попадают в датасет"
        )

    return AuditResult(
        source_id=source.id,
        allowed=allowed,
        robots_status=robots_status,
        robots_url=robots_url,
        robots_allowed_paths=checked_paths,
        crawl_delay=crawl_delay,
        request_rate=request_rate,
        tos_signals=[s.as_dict() for s in signals],
        tos_verdict=tos_verdict,
        source_audited=source.is_audited,
        reasons=reasons,
        checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
