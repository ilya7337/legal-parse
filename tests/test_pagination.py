"""Тесты многостраничной пагинации (чистые функции)."""

from __future__ import annotations

from legitfeed.collect.pagination import add_query, next_link_from_json, page_requests
from legitfeed.collect.specs import PaginationSpec


def test_page_requests_page_mode():
    pag = PaginationSpec(mode="page", start_page=1, max_pages=3)
    reqs = page_requests("http://e.test/api", pag)
    assert len(reqs) == 3
    assert reqs[0][1] == {"page": 1}
    assert reqs[2][1] == {"page": 3}


def test_page_requests_offset_mode():
    pag = PaginationSpec(mode="offset", max_pages=2, per_page=50)
    reqs = page_requests("http://e.test/api", pag)
    assert reqs[0][1] == {"limit": 50, "offset": 0}
    assert reqs[1][1] == {"limit": 50, "offset": 50}


def test_next_link_from_json():
    payload = {"links": {"next": "http://e.test/api?page=2"}}
    assert next_link_from_json(payload, "links.next") == "http://e.test/api?page=2"
    assert next_link_from_json(payload, "missing") is None


def test_add_query_preserves_existing():
    assert add_query("http://e.test/api?x=1", {"y": 2}) == "http://e.test/api?x=1&y=2"
