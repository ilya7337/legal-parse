"""Сбор RSS/Atom-фидов с обязательной проверкой правового аудита."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import feedparser

from config.settings import PROJECT_ROOT, Settings, get_settings
from legitfeed.collect.http_client import PoliteHttpClient
from legitfeed.legal.auditor import AuditResult, RobotsFetcher, audit_source
from legitfeed.legal.policy import LegalPolicy, load_policy
from legitfeed.legal.registry import FeedEntry, Source
from legitfeed.models import RawItem, collapse_ws, now_iso, text_hash


@dataclass
class CollectionStats:
    source_id: str
    items: int = 0
    skipped: int = 0
    from_fixture: bool = False
    status: str = "ok"
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "items": self.items,
            "skipped": self.skipped,
            "from_fixture": self.from_fixture,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class CollectionResult:
    items: list[RawItem] = field(default_factory=list)
    audits: list[AuditResult] = field(default_factory=list)
    stats: list[CollectionStats] = field(default_factory=list)
    client_stats: dict = field(default_factory=dict)

    def stats_dict(self) -> dict:
        return {
            "sources": [s.as_dict() for s in self.stats],
            "client": self.client_stats,
            "total_items": len(self.items),
        }


def _entry_datetime(entry) -> str | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=UTC).isoformat(
                    timespec="seconds"
                )
            except (TypeError, ValueError, OverflowError):
                continue
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw:
            try:
                return feedparser.datetimes._parse_date(raw).isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001 - feedparser может бросить что угодно
                continue
    return None


def _entry_summary(entry) -> str:
    content = entry.get("content")
    if content:
        try:
            return content[0].get("value", "")
        except (IndexError, AttributeError):
            pass
    return entry.get("summary", "") or ""


def parse_feed(
    payload: str,
    source: Source,
    *,
    limit: int = 20,
    snapshot_at: str | None = None,
) -> tuple[list[RawItem], int]:
    """Разобрать XML фида в RawItem. Возвращает (items, skipped)."""
    parsed = feedparser.parse(payload)
    items: list[RawItem] = []
    skipped = 0
    snapshot = snapshot_at or now_iso()

    entries = list(parsed.entries)
    if limit > 0:
        entries = entries[:limit]

    for entry in entries:
        title = collapse_ws(entry.get("title", ""))
        link = (entry.get("link") or "").strip()
        summary = _entry_summary(entry)
        if not title or not link:
            skipped += 1
            continue
        # Автор: собираем только отображаемое имя, e-mail/телефоны вычищаются
        # на этапе очистки (минимизация ПДн, 152-ФЗ).
        author = collapse_ws(entry.get("author", "")) or None
        tags = [collapse_ws(t.get("term", "")) for t in entry.get("tags", []) if t.get("term")]
        item = RawItem(
            source_id=source.id,
            title=title,
            link=link,
            summary_html=summary or "",
            published_at=_entry_datetime(entry),
            author=author,
            tags=tags[:10],
            license=source.license,
            license_url=source.license_url,
            snapshot_at=snapshot,
        ).ensure_hash()
        items.append(item)
    return items, skipped


def load_fixture(path: str | Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        raise FileNotFoundError(f"fixture not found: {p}")
    return p.read_text(encoding="utf-8")


class FeedCollector:
    """Оркестрирует: аудит -> вежливый запрос -> разбор -> RawItem."""

    def __init__(
        self,
        settings: Settings | None = None,
        policy: LegalPolicy | None = None,
        client: PoliteHttpClient | None = None,
        robots_fetcher: RobotsFetcher | None = None,
    ):
        self.settings = settings or get_settings()
        self.policy = policy or load_policy()
        self.client = client or PoliteHttpClient(self.settings, self.policy)
        self.robots_fetcher = robots_fetcher

    def collect_one(
        self, source: Source, feed: FeedEntry
    ) -> tuple[list[RawItem], AuditResult, CollectionStats]:
        audit = audit_source(
            source, policy=self.policy, robots_fetcher=self.robots_fetcher, strict=True
        )
        stats = CollectionStats(source_id=source.id)
        if not audit.allowed:
            stats.status = "denied"
            stats.error = "; ".join(audit.reasons) or "denied by legal audit"
            return [], audit, stats

        payload: str | None = None
        if not self.settings.offline:
            self._apply_robots_pacing(audit)
            response = self.client.get(str(source.feed_url), host_key=source.host)
            if response.ok and response.text.strip():
                payload = response.text
            else:
                stats.error = response.error or "empty response"

        if payload is None and feed.fixture:
            try:
                payload = load_fixture(feed.fixture)
                stats.from_fixture = True
            except FileNotFoundError as exc:
                stats.status = "error"
                stats.error = f"{stats.error}; {exc}" if stats.error else str(exc)
                return [], audit, stats

        if payload is None:
            stats.status = "error"
            stats.error = stats.error or "no data (offline mode without fixture)"
            return [], audit, stats

        items, skipped = parse_feed(payload, source, limit=feed.limit)
        stats.items = len(items)
        stats.skipped += skipped
        if skipped:
            stats.status = "partial"
        return items, audit, stats

    def _apply_robots_pacing(self, audit: AuditResult) -> None:
        """Поднять минимальную паузу до значений из robots.txt (Crawl-delay/Request-rate)."""
        if audit.crawl_delay:
            self.client.limiter.min_delay = max(self.client.limiter.min_delay, audit.crawl_delay)
        if audit.request_rate:
            reqs, period = audit.request_rate
            if reqs > 0:
                self.client.limiter.min_delay = max(
                    self.client.limiter.min_delay, period / reqs
                )

    def collect(self, pairs: list[tuple[Source, FeedEntry]]) -> CollectionResult:
        result = CollectionResult()
        for source, feed in pairs:
            items, audit, stats = self.collect_one(source, feed)
            result.items.extend(items)
            result.audits.append(audit)
            result.stats.append(stats)
        result.client_stats = dict(self.client.stats)
        return result


def dedupe_raw(items: list[RawItem]) -> list[RawItem]:
    """Удалить дубли внутри прогона (по content_hash и по каноническому URL)."""
    from legitfeed.legal.robots import canonical_url

    seen_hash: set[str] = set()
    seen_url: set[str] = set()
    out: list[RawItem] = []
    for item in items:
        item.ensure_hash()
        cur_url = canonical_url(item.link)
        if item.content_hash in seen_hash or cur_url in seen_url:
            continue
        seen_hash.add(item.content_hash)
        seen_url.add(cur_url)
        out.append(item)
    return out


def hash_raw(item: RawItem) -> str:
    return text_hash(item.source_id, item.link, item.title)
