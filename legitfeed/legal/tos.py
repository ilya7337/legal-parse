"""Эвристики проверки Terms of Service по тексту условий использования.

Полноценный правовой аудит делает человек; здесь — автоматический
предварительный скрининг, который:
  * ищет явные запреты автоматизированного доступа;
  * помечает найденные фразы как цитаты для отчёта;
  * никогда не «разрешает» источник сам — итоговый вердикт даёт человек
    (approved_by/approved_at в sources.yaml).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Русскоязычные и англоязычные формулировки запрета автоматизированного сбора
_BLOCK_PATTERNS: list[tuple[str, str]] = [
    (r"запрещ\w*[^.]{0,80}(автоматизирован\w+|робот\w*|парсин\w+|скрипт\w*)",
     "ToS запрещает автоматизированный сбор"),
    (r"не\s+(допускается|осуществл\w+|производить|выполнять)[^.]{0,80}(автоматизирован\w+|парсин\w+|сбор\w+)",
     "ToS не допускает автоматизированный сбор"),
    (r"обязан\w*[^.]{0,80}воздержаться[^.]{0,80}(автоматизирован\w+|парсин\w+|сбор\w+)",
     "ToS обязывает воздержаться от автоматизированного сбора"),
    # Английские: "may not ... robot/spider/crawl/scrape/automated"
    (r"may\s+not[^.]{0,80}(crawl|scrap\w*|spider|robot|automated\s+(means|access|collection))",
     "ToS запрещает краулинг/парсинг"),
    (r"(without|no)\s+(prior\s+)?(written\s+)?permission[^.]{0,80}(crawl|scrap|robot|spider|automated)",
     "ToS требует письменного разрешения на обход"),
    (r"(crawl|scrap\w*|spider|robot|automated)[^.]{0,80}without\s+(prior\s+)?(written\s+)?permission",
     "ToS требует письменного разрешения на обход"),
    (r"(prohibit|forbid|not\s+permitted)\w*[^.]{0,80}(crawl|scrap|spider|data\s+mining|automated\s+access)",
     "ToS запрещает краулинг/парсинг"),
    (r"data\s+mining[^.]{0,40}(prohibit|forbid|not\s+permitted)",
     "ToS запрещает data mining"),
]

_ALLOW_PATTERNS: list[tuple[str, str]] = [
    (r"(rss|atom|feed)[^.]{0,60}(free|publicly|available|разреш|доступ)",
     "ToS/документация упоминает публичный RSS/Atom"),
    (r"(open\s+data|открытые\s+данные|creative\s+commons|cc\s+by)",
     "ToS ссылается на открытую лицензию"),
    (r"api[^.]{0,60}(free|public|publicly\s+available)",
     "ToS упоминает публичный API"),
]


@dataclass(frozen=True)
class TosSignal:
    kind: str          # "block" | "allow"
    rule: str          # человекочитаемое описание
    quote: str         # фрагмент текста
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "rule": self.rule, "quote": self.quote, "reason": self.reason}


def _snippet(text: str, start: int, end: int, pad: int = 80) -> str:
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    return " ".join(text[s:e].split())


def analyze_tos(text: str) -> list[TosSignal]:
    """Найти признаки запрета/разрешения в тексте ToS."""
    if not text or not text.strip():
        return []
    lowered = text.lower()
    signals: list[TosSignal] = []
    for pattern, reason in _BLOCK_PATTERNS:
        for m in re.finditer(pattern, lowered, flags=re.IGNORECASE | re.DOTALL):
            signals.append(
                TosSignal(
                    kind="block",
                    rule="automated_access_prohibited",
                    quote=_snippet(text, m.start(), m.end()),
                    reason=reason,
                )
            )
    for pattern, reason in _ALLOW_PATTERNS:
        for m in re.finditer(pattern, lowered, flags=re.IGNORECASE | re.DOTALL):
            signals.append(
                TosSignal(
                    kind="allow",
                    rule="public_feed_license",
                    quote=_snippet(text, m.start(), m.end()),
                    reason=reason,
                )
            )
    return signals


def verdict(signals: list[TosSignal]) -> str:
    """Сводный вердикт по тексту: block | allow | unknown."""
    kinds = {s.kind for s in signals}
    if "block" in kinds:
        return "block"
    if "allow" in kinds:
        return "allow"
    return "unknown"
