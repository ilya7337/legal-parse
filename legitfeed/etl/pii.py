"""Маскирование персональных данных (152-ФЗ, принцип минимизации).

Мы не «находим ПДн, чтобы их использовать», а наоборот — удаляем их из текста
до сохранения в БД и датасет. Маскирование обратимо по смыслу (пользователь
видит, что там были ПДн), но не по значению.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- шаблоны (порядок важен: сначала более специфичные) ---

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# Российские телефоны: +7/8 (999) 123-45-67, +79991234567, 8-999-123-45-67
_PHONE_RU = re.compile(
    r"(?<![\w-])(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?![\w-])"
)
_PHONE_INTL = re.compile(r"(?<![\w-])\+\d{1,3}[\s\-]?\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{2,4}(?![\w-])")
# СНИЛС: 123-456-789 00
_SNILS = re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[\s\-]?\d{2}(?!\d)")
# Паспорт РФ: серия 4 цифры + номер 6 цифр
_PASSPORT = re.compile(r"(?<!\d)\d{2}\s?\d{2}\s?(?:№\s?)?\d{6}(?!\d)")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_TOKEN_URL = re.compile(
    r"(https?://[^\s?]+)\?([^\s#]*?(?:token|key|sig|auth|session|password)=)[^\s&#]+", re.IGNORECASE
)
_INN = re.compile(r"(?<!\d)ИНН[\s:]*\d{10,12}(?!\d)", re.IGNORECASE)

PATTERNS: dict[str, tuple[re.Pattern[str], str]] = {
    "email": (_EMAIL, "[EMAIL]"),
    "phone": (_PHONE_RU, "[PHONE]"),
    "phone_intl": (_PHONE_INTL, "[PHONE]"),
    "snils": (_SNILS, "[SNILS]"),
    "passport": (_PASSPORT, "[DOC]"),
    "ip": (_IPV4, "[IP]"),
    "inn": (_INN, "[INN]"),
}


@dataclass
class MaskResult:
    text: str
    found: list[str] = field(default_factory=list)


def mask_pii(text: str, enabled_types: list[str] | None = None) -> MaskResult:
    """Заменить ПДн на теги. Возвращает текст и список типов найденных ПДн."""
    if not text:
        return MaskResult(text="", found=[])

    types = enabled_types if enabled_types is not None else list(PATTERNS.keys()) + ["url_with_query_token"]
    found: list[str] = []
    out = text

    # URL с секретами обрабатываем отдельно (сохраняем путь, режем токен)
    if "url_with_query_token" in types:
        new_out, n = _TOKEN_URL.subn(lambda m: f"{m.group(1)}?{m.group(2)}[REDACTED]", out)
        if n:
            found.append("url_with_query_token")
        out = new_out

    for name in types:
        if name not in PATTERNS or name == "url_with_query_token":
            continue
        pattern, repl = PATTERNS[name]
        new_out, n = pattern.subn(repl, out)
        if n:
            found.append(name)
        out = new_out

    # счётчики могут быть 0 у замен -> всегда заменяем
    return MaskResult(text=out, found=sorted(set(found)))


def contains_pii(text: str, enabled_types: list[str] | None = None) -> bool:
    return bool(mask_pii(text, enabled_types).found)


def redact_field(value: str, forbidden_fields: list[str], field_name: str) -> str | None:
    """Политика ``forbidden_fields``: такие поля не собираются вовсе."""
    if field_name in forbidden_fields:
        return None
    return value
