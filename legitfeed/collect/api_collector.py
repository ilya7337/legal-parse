"""Сбор данных из публичных JSON API.

Важно: собираются только **публичные** API (без авторизации и подбора секретов)
и только после правового аудита источника. Используется тот же вежливый
HTTP-клиент (rate limit, backoff, кэш, честные заголовки).
"""

from __future__ import annotations

import json as _json
from urllib.parse import urlencode

from config.settings import Settings, get_settings
from legitfeed.collect.feed_collector import CollectionResult, CollectionStats, load_fixture
from legitfeed.collect.http_client import PoliteHttpClient
from legitfeed.collect.specs import APISpec, parse_datetime, resolve_path
from legitfeed.collect.pagination import next_link_from_json, page_requests
from legitfeed.collect.secrets import MissingSecretError, resolve_secret
from legitfeed.legal.auditor import AuditResult, RobotsFetcher, audit_source
from legitfeed.legal.policy import LegalPolicy, load_policy
from legitfeed.legal.registry import Source
from legitfeed.models import RawItem, text_hash


def full_url(spec: APISpec) -> str:
    if spec.params:
        sep = "&" if "?" in spec.endpoint else "?"
        return spec.endpoint + sep + urlencode(spec.params)
    return spec.endpoint


def extract_records(payload, spec: APISpec) -> list:
    """Достать список записей из JSON-ответа по ``items_path``."""
    raw = resolve_path(payload, spec.items_path)
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("items", "data", "results", "hits"):
            if isinstance(raw.get(key), list):
                return raw[key]
        # словарь вида {id: {...}}
        return list(raw.values())
    return []


def _str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return _json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return ""
    return str(value).strip()


def to_raw_item(record: dict, source: Source, spec: APISpec) -> RawItem | None:
    f = spec.fields
    title = _str(resolve_path(record, f.title))
    url = _str(resolve_path(record, f.url))
    text = _str(resolve_path(record, f.text))
    author = _str(resolve_path(record, f.author)) or None
    published = parse_datetime(resolve_path(record, f.published_at))

    # запасные варианты полей
    if not title:
        title = _str(record.get("title")) or _str(record.get("name"))
    if not url:
        url = _str(record.get("url")) or _str(record.get("html_url")) or _str(record.get("link"))
    if not text:
        text = (
            _str(record.get("text")) or _str(record.get("body"))
            or _str(record.get("description")) or _str(record.get("summary"))
            or _str(record.get("story_text"))
        )
    if not title and not text:
        return None
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


class APICollector:
    """Аудит -> вежливый GET -> извлечение записей -> RawItem."""

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

    def _resolve_auth(self, spec: APISpec) -> tuple[dict, dict]:
        """Заголовки/query-параметры авторизации. Секрет берётся из окружения."""
        headers: dict = {}
        params: dict = {}
        if spec.auth:
            token = resolve_secret(spec.auth.env)
            if spec.auth.type == "bearer":
                headers[spec.auth.header_name or "Authorization"] = f"Bearer {token}"
            elif spec.auth.type == "header":
                headers[spec.auth.header_name or "Authorization"] = token
            elif spec.auth.type == "query":
                params[spec.auth.param_name or "api_key"] = token
        return headers, params

    def _fetch_text(
        self,
        spec: APISpec,
        source: Source,
        url: str,
        params: dict | None,
        headers: dict | None,
        stats: CollectionStats,
    ) -> str | None:
        """Получить тело ответа (сеть или фикстура/offline)."""
        if not self.settings.offline:
            result = self.client.get(
                url, host_key=source.host, headers=headers or None, params=params or None
            )
            if result.ok and result.text.strip():
                return result.text
            stats.error = stats.error or result.error or "empty response"
            return None

        # offline: фикстура только на «первую страницу»
        if stats.from_fixture:
            return None
        if spec.fixture:
            try:
                stats.from_fixture = True
                return load_fixture(spec.fixture)
            except FileNotFoundError as exc:
                stats.status = "error"
                stats.error = str(exc)
                return None
        if not stats.error:
            stats.error = "no data (offline without fixture)"
        return None

    def collect_one(
        self, spec: APISpec, source: Source
    ) -> tuple[list[RawItem], AuditResult, CollectionStats]:
        audit = audit_source(
            source, paths=[spec.endpoint], policy=self.policy,
            robots_fetcher=self.robots_fetcher, strict=True,
        )
        stats = CollectionStats(source_id=source.id)
        if not audit.allowed:
            stats.status = "denied"
            stats.error = "; ".join(audit.reasons) or "denied by legal audit"
            return [], audit, stats

        try:
            auth_headers, auth_params = self._resolve_auth(spec)
        except MissingSecretError as exc:
            stats.status = "denied"
            stats.error = str(exc)
            return [], audit, stats

        pag = spec.pagination
        if pag and pag.mode == "next_link":
            queue: list[tuple[str, dict]] = [(spec.endpoint, dict(spec.params))]
        elif pag and pag.max_pages > 1:
            queue = page_requests(spec.endpoint, pag)
        else:
            queue = [(spec.endpoint, dict(spec.params))]

        max_pages = pag.max_pages if pag else 1
        all_records: list = []
        page_count = 0
        while queue and page_count < max_pages:
            url, params = queue.pop(0)
            merged_params = dict(params)
            merged_params.update(auth_params)
            text = self._fetch_text(
                spec, source, url, merged_params or None, auth_headers or None, stats
            )
            if text is None:
                break

            payload = _json.loads(text)
            all_records.extend(extract_records(payload, spec))
            page_count += 1

            if pag and pag.mode == "next_link":
                nxt = next_link_from_json(payload, pag.next_path)
                if nxt:
                    queue.append((nxt, {}))

        items: list[RawItem] = []
        for record in all_records:
            item = to_raw_item(record, source, spec)
            if item is not None:
                items.append(item)
            if spec.limit > 0 and len(items) >= spec.limit:
                break
        stats.items = len(items)
        return items, audit, stats

    def collect(self, pairs: list[tuple[APISpec, Source]]) -> CollectionResult:
        result = CollectionResult()
        for spec, source in pairs:
            items, audit, stats = self.collect_one(spec, source)
            result.items.extend(items)
            result.audits.append(audit)
            result.stats.append(stats)
        result.client_stats = dict(self.client.stats)
        return result
