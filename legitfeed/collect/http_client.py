"""Вежливый HTTP-клиент: rate limiting, backoff, Retry-After, дисковый кэш.

Клиент **не умеет** обходить блокировки: нет ротации прокси, нет смены
User-Agent, нет подбора заголовков. Единственное, что он делает при 429/503 —
уважает ``Retry-After`` и ждёт.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from config.settings import Settings, get_settings
from legitfeed.legal.policy import LegalPolicy, load_policy


class RateLimiter:
    """Минимальная пауза между запросами к одному хосту + глобальный rps-потолок."""

    def __init__(self, min_delay: float, max_rps: float):
        self.min_delay = max(0.0, min_delay)
        self.max_rps = max(0.0, max_rps)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def required_delay(self) -> float:
        by_rps = 1.0 / self.max_rps if self.max_rps > 0 else 0.0
        return max(self.min_delay, by_rps)

    def acquire(self, host: str, *, dry_run: bool = False) -> float:
        """Заблокировать поток на нужное время. Возвращает фактическое ожидание."""
        delay = self.required_delay()
        with self._lock:
            last = self._last.get(host, 0.0)
            now = time.monotonic()
            wait = max(0.0, last + delay - now)
            if not dry_run and wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()
        return wait


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    from_cache: bool = False
    elapsed: float = 0.0
    content_type: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class PoliteHttpClient:
    def __init__(
        self,
        settings: Settings | None = None,
        policy: LegalPolicy | None = None,
        session: requests.Session | None = None,
        cache_dir: Path | None = None,
        sleep=time.sleep,
    ):
        self.settings = settings or get_settings()
        self.policy = policy or load_policy()
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.settings.user_agent,
                "Accept": (
                    "application/rss+xml, application/atom+xml, application/xml, "
                    "text/xml;q=0.9, */*;q=0.5"
                ),
                "Accept-Language": "ru,en;q=0.8",
                "From": "research@example.org",  # честно шлём контакт
            }
        )
        self.cache_dir = cache_dir or self.settings.path("cache") / "http"
        self.limiter = RateLimiter(
            min_delay=self.policy.rate_limiting.min_delay_seconds,
            max_rps=min(self.policy.rate_limiting.max_global_rps, self.settings.max_global_rps),
        )
        self.sleep = sleep
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0, "throttled_seconds": 0.0}

    # ---------- кэш ----------

    @staticmethod
    def _cache_key(url: str, params: dict | None) -> str:
        """Ключ кэша: базовый URL + query-параметры (секреты не попадают в файл)."""
        from urllib.parse import urlencode

        key = url
        if params:
            key += "|" + urlencode(sorted(params.items()))
        return key

    def _cache_file(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, key: str) -> FetchResult | None:
        path = self._cache_file(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        age = time.time() - payload.get("stored_at", 0)
        if age > self.settings.cache_ttl_seconds:
            return None
        return FetchResult(
            url=payload.get("url", ""),
            status=payload.get("status", 200),
            text=payload.get("text", ""),
            from_cache=True,
            content_type=payload.get("content_type", ""),
        )

    def _write_cache(self, key: str, result: FetchResult) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_file(key).write_text(
                json.dumps(
                    {
                        "url": result.url,  # базовый URL без секретов в query
                        "status": result.status,
                        "text": result.text,
                        "content_type": result.content_type,
                        "stored_at": time.time(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ---------- запрос ----------

    def get(
        self,
        url: str,
        *,
        host_key: str | None = None,
        use_cache: bool = True,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> FetchResult:
        key = self._cache_key(url, params)
        if use_cache:
            cached = self._read_cache(key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return cached

        host = host_key or (url.split("/")[2] if "//" in url else url)
        max_retries = min(self.settings.max_retries, self.policy.rate_limiting.max_retries)
        base = self.policy.rate_limiting.backoff_base_seconds
        started = time.monotonic()
        last_error: str | None = None

        for attempt in range(max_retries + 1):
            waited = self.limiter.acquire(host)
            self.stats["throttled_seconds"] += waited
            self.stats["requests"] += 1
            try:
                resp = self.session.get(
                    url, timeout=self.settings.request_timeout,
                    headers=headers or None, params=params or None,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < max_retries:
                    self.stats["retries"] += 1
                    self.sleep(base ** (attempt + 1))
                    continue
                return FetchResult(
                    url=url, status=0, text="", error=last_error,
                    elapsed=time.monotonic() - started,
                )

            if resp.status_code in (429, 503) and attempt < max_retries:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                if retry_after is not None and self.policy.rate_limiting.respect_retry_after:
                    wait = retry_after
                else:
                    wait = base ** (attempt + 1)
                wait = min(wait, 60.0)  # не ждём бесконечно
                self.stats["retries"] += 1
                self.stats["throttled_seconds"] += wait
                self.sleep(wait)
                continue

            result = FetchResult(
                url=url,
                status=resp.status_code,
                text=resp.text if resp.status_code < 400 else "",
                elapsed=time.monotonic() - started,
                content_type=resp.headers.get("Content-Type", ""),
                error=None if resp.status_code < 400 else f"HTTP {resp.status_code}",
            )
            if result.ok:
                self._write_cache(key, result)
            return result

        return FetchResult(
            url=url, status=0, text="", error=last_error, elapsed=time.monotonic() - started
        )
