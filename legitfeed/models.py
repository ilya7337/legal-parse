"""Общие модели данных, проходящие через конвейер.

Один и тот же объект ``RawItem`` живёт от сбора до разметки. Ключевой принцип
происхождения (provenance): у каждой записи есть источник, URL, лицензия и
дата снимка — без этого запись не попадает в датасет.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def text_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").strip().lower().encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class RawItem(BaseModel):
    """Запись, как она пришла из фида (максимально «сырая»)."""

    source_id: str
    title: str = ""
    link: str = ""
    summary_html: str = ""
    published_at: str | None = None
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    license: str = ""
    license_url: str | None = None
    snapshot_at: str = Field(default_factory=now_iso)
    content_hash: str = ""

    def ensure_hash(self) -> RawItem:
        if not self.content_hash:
            self.content_hash = text_hash(self.source_id, self.link, self.title)
        return self


class CleanDoc(BaseModel):
    """Очищенный документ — вход для детектора аномалий и датасета."""

    doc_id: str
    source_id: str
    url: str
    canonical_url: str
    title: str
    text: str
    summary: str = ""
    language: str = "unknown"
    published_at: str | None = None
    snapshot_at: str = Field(default_factory=now_iso)
    license: str = ""
    license_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    word_count: int = 0
    char_count: int = 0
    pii_masked: list[str] = Field(default_factory=list)
    cleaning_flags: list[str] = Field(default_factory=list)
    content_hash: str = ""

    @classmethod
    def make_id(cls, source_id: str, canonical_url: str, title: str) -> str:
        return text_hash(source_id, canonical_url, title)[:16]


class AnomalyRecord(BaseModel):
    """Результат анализа одной записи детектором аномалий."""

    doc_id: str
    is_anomaly: bool = False
    score: float = 0.0
    rule_hits: list[str] = Field(default_factory=list)
    explanations: list[str] = Field(default_factory=list)
    explanation: str = ""  # склейка объяснений для пользователя
    metrics: dict[str, Any] = Field(default_factory=dict)


class DatasetRow(BaseModel):
    """Строка итогового датасета."""

    doc_id: str
    source_id: str
    url: str
    title: str
    text: str
    language: str
    published_at: str | None
    topic_label: str
    label: str
    label_confidence: float
    anomaly_score: float
    anomaly_reasons: str
    split: str
    license: str
    license_url: str | None
    snapshot_at: str


_WS = re.compile(r"\s+")


def collapse_ws(text: str) -> str:
    return _WS.sub(" ", text or "").strip()
