"""Построение URL для многостраничного сбора и извлечение next-ссылок."""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit, urlunsplit

from legitfeed.collect.specs import PaginationSpec, resolve_path


def add_query(url: str, params: dict) -> str:
    """Добавить query-параметры к URL, сохраняя существующие."""
    if not params:
        return url
    parts = urlsplit(url)
    merged = parts.query + ("&" if parts.query else "") + urlencode(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, merged, parts.fragment))


def page_requests(base_url: str, pag: PaginationSpec) -> list[tuple[str, dict]]:
    """Список (url, params) для режимов page/offset (без next_link)."""
    out: list[tuple[str, dict]] = []
    for i in range(pag.start_page, pag.start_page + pag.max_pages):
        params: dict = {}
        if pag.mode == "offset":
            per_page = pag.per_page or 20
            params[pag.limit_param] = per_page
            params[pag.offset_param] = (i - pag.start_page) * per_page
        else:  # page
            params[pag.page_param] = i
            if pag.per_page_param and pag.per_page:
                params[pag.per_page_param] = pag.per_page
        out.append((base_url, params))
    return out


def next_link_from_json(payload, path: str | None) -> str | None:
    """Извлечь URL следующей страницы из JSON (by dot-path, напр. 'links.next')."""
    if not path:
        return None
    value = resolve_path(payload, path)
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None
