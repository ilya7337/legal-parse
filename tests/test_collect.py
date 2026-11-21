"""Тесты сбора: парсинг фидов, дедупликация, rate limiter, вежливый HTTP-клиент."""

from __future__ import annotations

from pathlib import Path

from legitfeed.collect.feed_collector import dedupe_raw, parse_feed
from legitfeed.collect.http_client import PoliteHttpClient, RateLimiter
from legitfeed.models import RawItem

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_parse_feed_extracts_items(registry, habr_feed):
    source = registry.get("opendata_habr")
    items, skipped = parse_feed(habr_feed, source, limit=3)
    assert len(items) == 3
    assert skipped == 0
    for item in items:
        assert item.title
        assert item.link.startswith("http")
        assert item.source_id == "opendata_habr"


def test_parse_feed_respects_limit(registry, habr_feed):
    source = registry.get("opendata_habr")
    items, _ = parse_feed(habr_feed, source, limit=1)
    assert len(items) == 1


def test_parse_feed_atom(registry):
    xml = (FIXTURES / "python_insider.xml").read_text(encoding="utf-8")
    source = registry.get("python_insider")
    items, skipped = parse_feed(xml, source)
    assert len(items) >= 3
    assert items[0].source_id == "python_insider"


def test_dedupe_raw_removes_same_link():
    a = RawItem(source_id="s", title="T", link="https://example.org/a").ensure_hash()
    b = RawItem(source_id="s", title="T", link="https://example.org/a").ensure_hash()
    c = RawItem(source_id="s", title="T2", link="https://example.org/b").ensure_hash()
    out = dedupe_raw([a, b, c])
    assert len(out) == 2


# ---------- RateLimiter ----------

def test_rate_limiter_required_delay_min_delay_wins():
    r = RateLimiter(min_delay=5.0, max_rps=10.0)
    assert r.required_delay() == 5.0


def test_rate_limiter_required_delay_rps_wins():
    r = RateLimiter(min_delay=0.5, max_rps=0.2)  # 0.2 rps -> 5 c
    assert r.required_delay() == 5.0


def test_rate_limiter_acquire_is_throttled():
    r = RateLimiter(min_delay=10.0, max_rps=100.0)
    first_wait = r.acquire("host", dry_run=True)
    second_wait = r.acquire("host", dry_run=True)
    assert first_wait == 0.0   # первый запрос без ожидания
    assert second_wait >= 9.0  # второй ждёт ~10 c


# ---------- PoliteHttpClient ----------

class _Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class _Session:
    headers: dict = {}

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        return self.responses.pop(0)


def _client(tmp_path, policy, session, settings=None):
    from config.settings import Settings

    # отключаем ожидания в rate limiter, чтобы тест был быстрым
    policy.rate_limiting.min_delay_seconds = 0.0
    policy.rate_limiting.max_global_rps = 1000.0
    policy.rate_limiting.backoff_base_seconds = 0.0
    if settings is None:
        settings = Settings(database_url="sqlite+pysqlite:///:memory:", data_dir=tmp_path / "data")
    return PoliteHttpClient(
        settings=settings, policy=policy, session=session, cache_dir=tmp_path / "cache",
        sleep=lambda s: None,
    )


def test_http_client_retries_429_then_succeeds(tmp_path, policy):
    session = _Session(
        [_Resp(429, headers={"Retry-After": "0"}), _Resp(200, text="<ok/>")]
    )
    client = _client(tmp_path, policy, session)
    result = client.get("https://example.org/feed")
    assert result.ok is True
    assert result.text == "<ok/>"
    assert client.stats["retries"] == 1
    assert session.calls == 2


def test_http_client_uses_cache(tmp_path, policy):
    session = _Session([_Resp(200, text="body")])
    client = _client(tmp_path, policy, session)
    first = client.get("https://example.org/feed")
    second = client.get("https://example.org/feed")
    assert first.ok and second.ok
    assert second.from_cache is True
    assert session.calls == 1  # второй раз не ходили в сеть
    assert client.stats["cache_hits"] == 1


def test_http_client_reports_http_error(tmp_path, policy):
    session = _Session([_Resp(404)])
    client = _client(tmp_path, policy, session)
    result = client.get("https://example.org/missing")
    assert result.ok is False
    assert result.status == 404
