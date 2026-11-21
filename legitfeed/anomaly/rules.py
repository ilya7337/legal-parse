"""Правила детекции аномалий: каждая проверка даёт (hit, explanation, metric).

Правила разделены на «качество данных» (мусор, спам, кодировки), «контент»
(сенсационность, несоответствие тегам) и «ПДн/юридические» (в источнике
встретились ПДн — значит, нужен ручной просмотр).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from legitfeed.models import CleanDoc

_SPAM_PATTERNS = [
    re.compile(r"(?:https?://\S+){4,}", re.IGNORECASE),
    re.compile(r"\b(казино|ставки|займ|кредит без отказа|crypto pump|free money)\b", re.IGNORECASE),
    re.compile(r"[A-ZА-Я]{12,}"),                       # КАПС-ШУМ
    re.compile(r"(?:!{3,}|\?{3,}|!!!\s*!!!)"),
]
_SENSATIONAL = re.compile(
    r"\b(шок|сенсаци\w+|скандал\w*|!!!|shocking|you won't believe|BREAKING)\b", re.IGNORECASE
)
_BROKEN_ENCODING = re.compile(r"(Ã[\x80-\xBF]|â€|Ð[\x80-\xBF]|\ufffd)")
_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass
class RuleHit:
    rule: str
    explanation: str
    weight: float = 1.0
    metrics: dict = field(default_factory=dict)


def _garbage_ratio(text: str) -> float:
    """Доля неалфавитных/управляющих символов."""
    if not text:
        return 0.0
    bad = sum(1 for c in text if not (c.isalnum() or c.isspace() or c in ".,;:!?\"'()-–—%$€₽/№«»"))
    return bad / len(text)


def rule_health(doc: CleanDoc, *, min_chars: int = 120) -> RuleHit | None:
    """Мусор, обрезанный/битый текст."""
    problems: list[str] = []
    metrics: dict = {}
    if doc.char_count < min_chars:
        problems.append(f"слишком короткий текст ({doc.char_count} симв.)")
    ratio = _garbage_ratio(doc.text)
    metrics["garbage_ratio"] = round(ratio, 4)
    if ratio > 0.12:
        problems.append(f"высокая доля мусорных символов ({ratio:.0%})")
    if _BROKEN_ENCODING.search(doc.text):
        problems.append("признаки сломанной кодировки (mojibake)")
    if len(_WORD_RE.findall(doc.text)) and doc.word_count / max(1, len(_WORD_RE.findall(doc.text))) < 0.4:
        problems.append("аномально низкая доля слов (код/мусор вместо текста)")
    if not problems:
        return None
    return RuleHit(
        rule="data_quality",
        explanation="Проблема качества данных: " + "; ".join(problems),
        weight=1.5,
        metrics=metrics,
    )


def rule_spam(doc: CleanDoc) -> RuleHit | None:
    hits = [p.pattern for p in _SPAM_PATTERNS if p.search(doc.text)]
    if not hits:
        return None
    return RuleHit(
        rule="spam_like",
        explanation=(
            "Текст похож на спам/накрутку: обнаружены множественные ссылки, "
            "рекламные маркеры, КАПС или повторяющаяся пунктуация"
        ),
        weight=1.2,
        metrics={"patterns": hits},
    )


def rule_sensational(doc: CleanDoc) -> RuleHit | None:
    matches = _SENSATIONAL.findall(doc.text)
    if len(matches) < 3:
        return None
    return RuleHit(
        rule="sensational",
        explanation=f"Сенсационный стиль: {len(matches)} маркеров кликбейта в тексте",
        weight=0.8,
        metrics={"matches": matches[:8]},
    )


def rule_pii_present(doc: CleanDoc) -> RuleHit | None:
    if not doc.pii_masked:
        return None
    return RuleHit(
        rule="pii_masked",
        explanation=(
            "В тексте были персональные данные ("
            + ", ".join(doc.pii_masked)
            + ") — они замаскированы; запись требует ручной проверки"
        ),
        weight=1.0,
        metrics={"pii_types": doc.pii_masked},
    )


def rule_duplicate_flag(doc: CleanDoc) -> RuleHit | None:
    if "near_duplicate" in doc.cleaning_flags or "duplicate" in doc.cleaning_flags:
        return RuleHit(
            rule="duplicate",
            explanation="Запись является дубликатом или почти-дубликатом ранее собранной",
            weight=2.0,
        )
    return None


ALL_RULES = [
    rule_health,
    rule_spam,
    rule_sensational,
    rule_pii_present,
    rule_duplicate_flag,
]
