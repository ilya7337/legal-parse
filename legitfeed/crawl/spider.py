"""Опциональный Scrapy-паук для многостраничного сбора списков статей.

Scrapy — **опциональная** зависимость: `pip install scrapy`.

Настройки принципиально соблюдают правовую политику проекта:
  * ROBOTSTXT_OBEY = True   (RFC 9309)
  * AUTO THROTTLE           (вежливый rate limiting)
  * один запрос на домен    (не «бомбим» сервер)
  * честный User-Agent

Запуск: `python -m legitfeed.cli crawl <start_url> ...`
"""

from __future__ import annotations

from config.settings import get_settings


class ScrapyUnavailableError(RuntimeError):
    """Scrapy не установлен."""


def run_crawl(
    start_urls: list[str],
    *,
    article_css: str = "article a::attr(href)",
    title_css: str = "h1::text",
    body_css: str = "article p::text",
    next_css: str = "",
    max_pages: int = 5,
    allowed_domains: list[str] | None = None,
) -> list[dict]:
    """Рекурсивный обход: список статей -> статья -> следующая страница списка.

    Возвращает список dict с ключами ``url``/``title``/``text``.
    """
    try:
        import scrapy
        from scrapy.crawler import CrawlerProcess
    except ImportError as exc:
        raise ScrapyUnavailableError(
            "Scrapy не установлен. Установите: `pip install scrapy`"
        ) from exc

    settings = get_settings()
    items: list[dict] = []

    class ArticleSpider(scrapy.Spider):
        name = "legitfeed"
        pages_seen = 0

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.start_urls = list(start_urls)
            if allowed_domains:
                self.allowed_domains = list(allowed_domains)

        def parse(self, response):
            self.pages_seen += 1
            for href in response.css(article_css).getall():
                if href:
                    yield response.follow(href, callback=self.parse_article)
            if next_css and self.pages_seen < max_pages:
                nxt = response.css(next_css).get()
                if nxt:
                    yield response.follow(nxt, callback=self.parse)

        def parse_article(self, response):
            title = " ".join(response.css(title_css).getall()).strip()
            body = " ".join(response.css(body_css).getall()) if body_css else response.text
            items.append({"url": response.url, "title": title, "text": body})

    process = CrawlerProcess(
        settings={
            "ROBOTSTXT_OBEY": True,
            "AUTOTHROTTLE_ENABLED": True,
            "AUTOTHROTTLE_START_DELAY": 2.0,
            "AUTOTHROTTLE_MAX_DELAY": 30.0,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "CONCURRENT_REQUESTS": 1,
            "USER_AGENT": settings.user_agent,
            "LOG_LEVEL": "WARNING",
        }
    )
    process.crawl(ArticleSpider)
    process.start()
    return items
