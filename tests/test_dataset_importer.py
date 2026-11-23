"""Тесты импортёра готовых датасетов: CSV/JSONL/JSON и правовой гейт лицензии."""

from __future__ import annotations

from legitfeed.collect.dataset_importer import DatasetImporter, parse_rows, to_raw_item
from legitfeed.collect.specs import DatasetSpec, FieldMap
from legitfeed.legal.registry import Source


def _src(license="CC BY 4.0", approved_by="b", approved_at="2025-01-01") -> Source:
    return Source(
        id="ds", name="D", site_url="https://e.org", license=license,
        approved_by=approved_by, approved_at=approved_at, tos_allow_feed=True,
    )


def test_parse_csv():
    text = "title,text,url\nA,long body text here,https://x/1\n"
    rows = parse_rows(text, "csv")
    assert rows[0]["title"] == "A"


def test_parse_jsonl():
    rows = parse_rows('{"a": 1}\n{"a": 2}\n', "jsonl")
    assert len(rows) == 2


def test_parse_json_list():
    assert len(parse_rows('[{"a": 1}]', "json")) == 1


def test_parse_json_dict_with_list():
    assert len(parse_rows('{"data": [{"a": 1}, {"a": 2}]}', "json")) == 2


def test_to_raw_item_mapping():
    spec = DatasetSpec(
        source_id="ds", source_type="local", path="x.csv", fmt="csv",
        fields=FieldMap(title="title", text="text", url="url", author="author"),
    )
    row = {"title": "T", "text": "A long enough text", "url": "https://e.org/1", "author": "A"}
    item = to_raw_item(row, _src(), spec)
    assert item.title == "T"
    assert item.link == "https://e.org/1"
    assert item.author == "A"


def test_to_raw_item_synthesizes_url():
    spec = DatasetSpec(source_id="ds", source_type="local", path="x.csv",
                       fmt="csv", fields=FieldMap(title="title", text="text"))
    item = to_raw_item({"title": "T", "text": "A long enough text"}, _src(), spec)
    assert item.link.startswith("https://ds.invalid/")
    assert item.content_hash


def test_importer_local_csv_offline(tmp_settings, policy):
    spec = DatasetSpec(source_id="ds", source_type="local",
                       path="tests/fixtures/dataset_articles.csv", fmt="csv",
                       fields=FieldMap(title="title", text="text", url="url", author="author"))
    importer = DatasetImporter(settings=tmp_settings, policy=policy)
    items, audit, stats = importer.collect_one(spec, _src())
    assert audit.allowed is True
    assert len(items) == 3
    assert items[0].license == "CC BY 4.0"


def test_importer_denies_without_license(tmp_settings, policy):
    spec = DatasetSpec(source_id="ds", source_type="local",
                       path="tests/fixtures/dataset_articles.csv", fmt="csv")
    importer = DatasetImporter(settings=tmp_settings, policy=policy)
    items, audit, stats = importer.collect_one(spec, _src(license="", approved_by="", approved_at=""))
    assert audit.allowed is False
    assert items == []
    assert any("лицензия" in r for r in audit.reasons)
