"""Тесты опциональных зависимостей: Playwright и Scrapy (fallback при отсутствии)."""

from __future__ import annotations

import pytest

from legitfeed.collect.browser import PlaywrightUnavailableError
from legitfeed.crawl.spider import ScrapyUnavailableError


def test_playwright_raises_when_missing():
    from legitfeed.collect.browser import render_html

    try:
        import playwright  # noqa: F401

        pytest.skip("playwright установлен — проверку отсутствия пропускаем")
    except ImportError:
        with pytest.raises(PlaywrightUnavailableError):
            render_html("https://example.com")


def test_scrapy_raises_when_missing():
    from legitfeed.crawl.spider import run_crawl

    try:
        import scrapy  # noqa: F401

        pytest.skip("scrapy установлен — проверку отсутствия пропускаем")
    except ImportError:
        with pytest.raises(ScrapyUnavailableError):
            run_crawl(["https://example.com"])
