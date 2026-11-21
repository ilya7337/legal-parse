"""Реестр источников (config/sources.yaml) и фидов (config/feeds.yaml)."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator

from config.settings import get_settings


class Source(BaseModel):
    """Источник данных с подтверждённым правовым статусом."""

    id: str
    name: str
    feed_url: HttpUrl
    site_url: HttpUrl
    license: str
    license_url: str | None = None
    tos_allow_feed: bool = False
    contains_personal_data: bool = False
    rate_limit_rps: float = 0.2
    min_delay_seconds: float = 5.0
    categories: list[str] = Field(default_factory=list)
    approved_by: str = ""
    approved_at: str = ""

    @field_validator("id")
    @classmethod
    def _id_is_slug(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "_-" for c in v):
            raise ValueError("source id must be a slug (a-z0-9_-)")
        return v

    @property
    def is_audited(self) -> bool:
        """Проведён ли правовой аудит (есть ответственный и дата)."""
        return bool(self.approved_by.strip()) and self._valid_date()

    def _valid_date(self) -> bool:
        try:
            date.fromisoformat(self.approved_at)
            return True
        except (ValueError, TypeError):
            return False

    @property
    def effective_delay(self) -> float:
        """Минимальная пауза: учитываем и rps, и явный min_delay."""
        by_rps = 1.0 / self.rate_limit_rps if self.rate_limit_rps > 0 else 0.0
        return max(by_rps, self.min_delay_seconds)

    @property
    def host(self) -> str:
        parts = urlsplit(str(self.feed_url))
        return parts.netloc.lower()


class FeedEntry(BaseModel):
    """Запись конфигурации фида."""

    source_id: str
    enabled: bool = True
    limit: int = 20
    fixture: str | None = None


class SourceRegistry:
    def __init__(self, sources: dict[str, Source], feeds: list[FeedEntry]):
        self._sources = sources
        self.feeds = feeds

    def get(self, source_id: str) -> Source:
        try:
            return self._sources[source_id]
        except KeyError as exc:
            raise KeyError(f"unknown source_id={source_id!r}") from exc

    @property
    def sources(self) -> dict[str, Source]:
        return dict(self._sources)

    def enabled(self) -> list[tuple[Source, FeedEntry]]:
        out = []
        for feed in self.feeds:
            if not feed.enabled:
                continue
            out.append((self.get(feed.source_id), feed))
        return out

    def __len__(self) -> int:
        return len(self._sources)


def _load_sources(path: Path) -> dict[str, Source]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result: dict[str, Source] = {}
    for item in data.get("sources", []):
        src = Source.model_validate(item)
        if src.id in result:
            raise ValueError(f"duplicate source id: {src.id}")
        result[src.id] = src
    return result


def _load_feeds(path: Path) -> list[FeedEntry]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [FeedEntry.model_validate(item) for item in data.get("feeds", [])]


@lru_cache(maxsize=4)
def _registry_from_paths(sources_path: str, feeds_path: str) -> SourceRegistry:
    feeds = _load_feeds(Path(feeds_path))
    sources = _load_sources(Path(sources_path))
    for feed in feeds:
        if feed.source_id not in sources:
            raise KeyError(f"feeds.yaml references unknown source_id={feed.source_id!r}")
    return SourceRegistry(sources, feeds)


def load_registry(
    sources_path: Path | None = None, feeds_path: Path | None = None
) -> SourceRegistry:
    settings = get_settings()
    sp = sources_path or settings.sources_path
    fp = feeds_path or settings.feeds_path
    return _registry_from_paths(str(sp), str(fp))
