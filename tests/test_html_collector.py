"""Тесты HTML-коллектора: извлечение основного текста и offline-сбор."""

from __future__ import annotations

import json
from pathlib import Path

from legitfeed.collect.html_collector import HTMLCollector, extract_article
from legitfeed.collect.specs import load_page_specs
from legitfeed.legal.auditor import RobotsFetcher

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_extract_article_pep():
    html = (FIXTURES / "page_article.html").read_text(encoding="utf-8")
    art = extract_article(html, "https://peps.python.org/pep-0008/")
    assert "PEP 8" in art["title"]
    assert "indentation" in art["text"].lower()
    assert art["url"] == "https://peps.python.org/pep-0008"
    assert art["published_at"]
    # футер/навигация исключены из основного текста
    assert "Python Software Foundation" not in art["text"]


def test_extract_article_wiki():
    html = (FIXTURES / "page_wiki.html").read_text(encoding="utf-8")
    art = extract_article(html, "https://en.wikipedia.org/wiki/Machine_learning")
    assert "Machine learning" in art["title"]
    assert "supervised" in art["text"].lower()


def _allow_fetcher(tmp_path, site: str) -> RobotsFetcher:
    f = RobotsFetcher(cache_dir=tmp_path / "robots", offline=True)
    f.cache_dir.mkdir(parents=True, exist_ok=True)
    url = f"{str(site).rstrip('/')}/robots.txt"
    f._cache_path(url).write_text(
        json.dumps({"text": "User-agent: *\nAllow: /", "status": 200}), encoding="utf-8"
    )
    return f


def test_collect_one_offline(tmp_settings, policy, registry):
    spec = load_page_specs()[0]  # peps_python
    source = registry.get(spec.source_id)
    collector = HTMLCollector(
        settings=tmp_settings, policy=policy,
        robots_fetcher=_allow_fetcher(tmp_path=tmp_settings.data_dir, site=str(source.site_url)),
    )
    items, audit, stats = collector.collect_one(spec, source)
    assert audit.allowed is True
    assert len(items) == 1
    assert stats.from_fixture is True
    assert "PEP 8" in items[0].title
    assert len(items[0].summary_html) > 200
