"""Сбор и извлечение основного текста из HTML-страниц (readability-подобно).

Извлекается «полезное содержимое» статьи: заголовок, основной текст из плотных
блоков <p>/<li>, дата из meta-тегов и canonical-ссылка. Навигация/футеры/
скрипты удаляются до извлечения.
"""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from config.settings import Settings, get_settings
from legitfeed.collect.feed_collector import CollectionResult, CollectionStats, load_fixture
from legitfeed.collect.http_client import PoliteHttpClient
from legitfeed.collect.specs import PageSpec, parse_datetime
from legitfeed.legal.auditor import AuditResult, RobotsFetcher, audit_source
from legitfeed.legal.policy import LegalPolicy, load_policy
from legitfeed.legal.registry import Source
from legitfeed.legal.robots import canonical_url
from legitfeed.models import RawItem, collapse_ws

_DROP = [
    "script", "style", "noscript", "iframe", "svg", "form", "template",
    "nav", "footer", "aside", "header", "figure",
]


def _meta_date(soup: BeautifulSoup) -> str | None:
    props = ("article:published_time", "og:article:published_time")
    for prop in props:
        node = soup.find("meta", attrs={"property": prop})
        if node and node.get("content"):
            return node["content"]
    for name in ("date", "pubdate", "publish_date"):
        node = soup.find("meta", attrs={"name": name})
        if node and node.get("content"):
            return node["content"]
    time = soup.find("time")
    if time and time.get("datetime"):
        return time["datetime"]
    return None


def extract_article(html: str, url: str = "") -> dict:
    """html -> {title, text, published_at, url}."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_DROP):
        tag.decompose()

    title = collapse_ws(soup.title.get_text()) if soup.title else ""
    if not title:
        h1 = soup.find("h1")
        title = collapse_ws(h1.get_text()) if h1 else ""

    date = _meta_date(soup)

    canonical = ""
    link = soup.find("link", rel="canonical")
    if link and link.get("href"):
        canonical = canonical_url(urljoin(url, link["href"]))
    elif url:
        canonical = canonical_url(url)

    container = soup.find("article") or soup.find("main") or soup.body or soup
    blocks = []
    for p in container.find_all(["p", "li", "blockquote", "h2", "h3"]):
        t = collapse_ws(p.get_text())
        if len(t) >= 20:
            blocks.append(t)
    text = "\n".join(blocks)
    if len(text) < 200 and soup.body:
        body = collapse_ws(soup.body.get_text(" "))
        if len(body) > len(text):
            text = body

    return {"title": title, "text": text, "published_at": date, "url": canonical or url}


class HTMLCollector:
    """Аудит (robots для URL страницы) -> GET -> извлечение -> RawItem."""

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

    def _fetch_one(
        self,
        page: PageSpec,
        source: Source,
        url: str,
        stats: CollectionStats,
        online: bool,
    ) -> str | None:
        """Получить HTML одной страницы: Playwright (render_js) или обычный GET/фикстура."""
        if online:
            if page.render_js:
                from legitfeed.collect.browser import (
                    PlaywrightUnavailableError,
                    render_html,
                )

                try:
                    return render_html(url, wait_selector=page.wait_selector)
                except PlaywrightUnavailableError as exc:
                    stats.error = stats.error or str(exc)
                    return None
            result = self.client.get(url, host_key=source.host)
            if result.ok:
                return result.text
            stats.error = stats.error or result.error or "empty response"
            return None

        # offline: фикстура только на первую страницу
        if stats.from_fixture:
            return None
        if page.fixture:
            try:
                stats.from_fixture = True
                return load_fixture(page.fixture)
            except FileNotFoundError as exc:
                stats.status = "error"
                stats.error = str(exc)
                return None
        if not stats.error:
            stats.error = "no data (offline without fixture)"
        return None

    def collect_one(
        self, page: PageSpec, source: Source
    ) -> tuple[list[RawItem], AuditResult, CollectionStats]:
        urls = [page.url] + [u for u in (page.urls or []) if u]
        audit = audit_source(
            source, paths=urls, policy=self.policy,
            robots_fetcher=self.robots_fetcher, strict=True,
        )
        stats = CollectionStats(source_id=source.id)
        if not audit.allowed:
            stats.status = "denied"
            stats.error = "; ".join(audit.reasons) or "denied by legal audit"
            return [], audit, stats

        items: list[RawItem] = []
        online = not self.settings.offline
        for url in urls:
            html = self._fetch_one(page, source, url, stats, online)
            if html is None:
                continue
            article = extract_article(html, url)
            if not article["text"].strip():
                continue
            item = RawItem(
                source_id=source.id,
                title=article["title"] or source.name,
                link=article["url"] or url,
                summary_html=article["text"],
                published_at=parse_datetime(article["published_at"]),
                author=None,
                tags=[],
                license=source.license,
                license_url=source.license_url,
            ).ensure_hash()
            items.append(item)

        if not items:
            stats.status = "error"
            stats.error = stats.error or "не удалось извлечь текст страницы"
            return [], audit, stats
        stats.items = len(items)
        return items, audit, stats

    def collect(self, pairs: list[tuple[PageSpec, Source]]) -> CollectionResult:
        result = CollectionResult()
        for page, source in pairs:
            items, audit, stats = self.collect_one(page, source)
            result.items.extend(items)
            result.audits.append(audit)
            result.stats.append(stats)
        result.client_stats = dict(self.client.stats)
        return result
