"""Импорт готовых датасетов: локальные файлы, файлы по URL, HuggingFace.

Политика: у датасета обязана быть лицензия (проверяется аудитом), robots.txt
не применяется (это не краулинг страницы). Записи приводятся к RawItem и дальше
проходят общий конвейер очистки/аномалий/разметки.
"""

from __future__ import annotations

import csv
import io
import json as _json
from pathlib import Path

from config.settings import PROJECT_ROOT, Settings, get_settings
from legitfeed.collect.feed_collector import CollectionResult, CollectionStats, load_fixture
from legitfeed.collect.http_client import PoliteHttpClient
from legitfeed.collect.specs import DatasetSpec, parse_datetime, resolve_path
from legitfeed.legal.auditor import AuditResult, RobotsFetcher, audit_source
from legitfeed.legal.policy import LegalPolicy, load_policy
from legitfeed.legal.registry import Source
from legitfeed.models import RawItem, text_hash


def _detect_fmt(spec: DatasetSpec, text: str | None = None) -> str:
    if spec.fmt != "auto":
        return spec.fmt
    suffix = (spec.path or "").lower().split("?")[0]
    for fmt in ("jsonl", "json", "csv"):
        if suffix.endswith(fmt):
            return fmt
    if text:
        if text.lstrip()[:1] in "[{":
            return "json"
    return "jsonl"


def parse_rows(text: str, fmt: str) -> list[dict]:
    if fmt == "jsonl":
        return [_json.loads(line) for line in text.splitlines() if line.strip()]
    if fmt == "json":
        data = _json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for val in data.values():
                if isinstance(val, list):
                    return val
            return [data]
        return []
    if fmt == "csv":
        reader = csv.DictReader(io.StringIO(text))
        return [dict(row) for row in reader]
    raise ValueError(f"неподдерживаемый формат: {fmt}")


def _s(value) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()


def to_raw_item(row: dict, source: Source, spec: DatasetSpec) -> RawItem | None:
    f = spec.fields
    title = _s(resolve_path(row, f.title))
    text = _s(resolve_path(row, f.text))
    url = _s(resolve_path(row, f.url))
    author = _s(resolve_path(row, f.author)) or None
    published = parse_datetime(resolve_path(row, f.published_at))

    if not text and not title:
        return None
    if not text and title:
        text = title
    if not url:
        url = f"https://{source.id}.invalid/item/{text_hash(title or text)[:16]}"

    return RawItem(
        source_id=source.id,
        title=title[:1024],
        link=url,
        summary_html=text,
        published_at=published,
        author=author,
        tags=[],
        license=source.license,
        license_url=source.license_url,
    ).ensure_hash()


class DatasetImporter:
    """Загрузка готовых датасетов без краулинга (robots пропущен, лицензия обязательна)."""

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

    def _fetch_rows(self, spec: DatasetSpec) -> list[dict]:
        if spec.source_type == "local":
            p = Path(spec.path)
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            if not p.exists():
                raise FileNotFoundError(f"dataset file not found: {p}")
            text = p.read_text(encoding="utf-8-sig")
            return parse_rows(text, _detect_fmt(spec, text))

        if spec.source_type == "url":
            text: str | None = None
            if not self.settings.offline:
                result = self.client.get(spec.path)
                if result.ok:
                    text = result.text
            if text is None and spec.fixture:
                text = load_fixture(spec.fixture)
            if text is None:
                raise RuntimeError("URL-датасет недоступен (offline без fixture)")
            return parse_rows(text, _detect_fmt(spec, text))

        if spec.source_type == "huggingface":
            try:
                from datasets import load_dataset
            except ImportError as exc:
                raise ImportError(
                    "Для импорта из HuggingFace установите `datasets` (`pip install datasets`)"
                ) from exc
            ds = load_dataset(spec.name, split=spec.split)
            return [dict(row) for row in ds]

        raise ValueError(f"unknown source_type: {spec.source_type}")

    def collect_one(
        self, spec: DatasetSpec, source: Source
    ) -> tuple[list[RawItem], AuditResult, CollectionStats]:
        audit = audit_source(
            source, paths=[], check_robots=False, policy=self.policy,
            robots_fetcher=self.robots_fetcher, strict=True,
        )
        stats = CollectionStats(source_id=source.id)
        if not audit.allowed:
            stats.status = "denied"
            stats.error = "; ".join(audit.reasons) or "denied by legal audit"
            return [], audit, stats

        try:
            rows = self._fetch_rows(spec)
        except Exception as exc:  # noqa: BLE001 - любая ошибка загрузки в журнал
            stats.status = "error"
            stats.error = f"{type(exc).__name__}: {exc}"
            return [], audit, stats

        if spec.limit > 0:
            rows = rows[: spec.limit]

        items: list[RawItem] = []
        for row in rows:
            item = to_raw_item(row, source, spec)
            if item is not None:
                items.append(item)
        stats.items = len(items)
        return items, audit, stats

    def collect(self, pairs: list[tuple[DatasetSpec, Source]]) -> CollectionResult:
        result = CollectionResult()
        for spec, source in pairs:
            items, audit, stats = self.collect_one(spec, source)
            result.items.extend(items)
            result.audits.append(audit)
            result.stats.append(stats)
        result.client_stats = dict(self.client.stats)
        return result
