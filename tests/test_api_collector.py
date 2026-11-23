"""Тесты API-коллектора: полный URL, извлечение записей, маппинг полей, offline-сбор."""

from __future__ import annotations

import json
from pathlib import Path

from legitfeed.collect.api_collector import APICollector, extract_records, full_url, to_raw_item
from legitfeed.collect.specs import APISpec, AuthSpec, FieldMap, load_api_specs
from legitfeed.legal.auditor import RobotsFetcher
from legitfeed.legal.registry import Source


def _spec(**kw) -> APISpec:
    base = dict(source_id="x", endpoint="http://e.test/api", items_path="", limit=10)
    base.update(kw)
    return APISpec.model_validate(base)


def _src() -> Source:
    return Source(
        id="x", name="X", site_url="https://e.org", license="CC BY 4.0",
        approved_by="b", approved_at="2025-01-01", tos_allow_feed=True,
    )


def test_full_url_appends_params():
    spec = _spec(endpoint="http://e.test/api", params={"q": "py", "tags": "story"})
    assert full_url(spec) == "http://e.test/api?q=py&tags=story"


def test_full_url_no_params():
    assert full_url(_spec()) == "http://e.test/api"


def test_extract_records_top_level_array():
    assert len(extract_records([{"t": 1}, {"t": 2}], _spec())) == 2


def test_extract_records_nested_path():
    payload = {"data": {"items": [{"t": 1}]}}
    assert len(extract_records(payload, _spec(items_path="data.items"))) == 1


def test_extract_records_dict_of_items():
    payload = {"r1": {"t": 1}, "r2": {"t": 2}}
    assert len(extract_records(payload, _spec(items_path=""))) == 2


def test_to_raw_item_mapping():
    rec = {
        "title": "Title here",
        "html_url": "https://g/x/1",
        "body": "A long enough body text describing the issue for the dataset.",
        "created_at": "2025-11-10T09:30:00Z",
        "user": {"login": "octocat"},
    }
    f = FieldMap(title="title", url="html_url", text="body",
                 published_at="created_at", author="user.login")
    item = to_raw_item(rec, _src(), _spec(fields=f))
    assert item.title == "Title here"
    assert item.link == "https://g/x/1"
    assert item.author == "octocat"
    assert "body text" in item.summary_html
    assert item.published_at is not None


def test_to_raw_item_skip_empty():
    assert to_raw_item({}, _src(), _spec()) is None


def _allow_fetcher(tmp_path, site: str) -> RobotsFetcher:
    f = RobotsFetcher(cache_dir=tmp_path / "robots", offline=True)
    f.cache_dir.mkdir(parents=True, exist_ok=True)
    url = f"{str(site).rstrip('/')}/robots.txt"
    f._cache_path(url).write_text(
        json.dumps({"text": "User-agent: *\nAllow: /", "status": 200}), encoding="utf-8"
    )
    return f


def test_collect_one_offline(tmp_settings, policy, registry):
    spec = load_api_specs()[0]  # github_psf_issues
    source = registry.get(spec.source_id)
    collector = APICollector(
        settings=tmp_settings, policy=policy,
        robots_fetcher=_allow_fetcher(tmp_path=tmp_settings.data_dir, site=str(source.site_url)),
    )
    items, audit, stats = collector.collect_one(spec, source)
    assert audit.allowed is True
    assert len(items) == 3
    assert stats.from_fixture is True
    assert all(i.source_id == "github_psf_issues" for i in items)


def test_collect_one_missing_secret_denied(tmp_settings, policy, registry, monkeypatch):
    monkeypatch.delenv("LEGITFEED_MISSING_TOKEN", raising=False)
    spec = APISpec(
        source_id="github_psf_issues",
        endpoint="https://api.github.com/repos/psf/requests/issues",
        items_path="",
        fields=FieldMap(title="title", url="html_url", text="body"),
        auth=AuthSpec(type="header", env="LEGITFEED_MISSING_TOKEN", header_name="X-Token"),
        fixture="tests/fixtures/api_github.json",
    )
    source = registry.get("github_psf_issues")
    collector = APICollector(
        settings=tmp_settings, policy=policy,
        robots_fetcher=_allow_fetcher(tmp_path=tmp_settings.data_dir, site=str(source.site_url)),
    )
    items, audit, stats = collector.collect_one(spec, source)
    assert items == []
    assert stats.status == "denied"
    assert "LEGITFEED_MISSING_TOKEN" in stats.error
