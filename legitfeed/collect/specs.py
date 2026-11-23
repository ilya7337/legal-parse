"""Спецификации ресурсов: API, HTML-страницы, готовые датасеты.

Аналог ``feeds.yaml``: эти файлы описывают **что** собирать, а правовой профиль
источника берётся из ``sources.yaml`` по ``source_id``. Все три типа выдают тот же
``RawItem``, что и RSS-фиды, поэтому дальше данные идут через общий конвейер
(очистка -> аномалии -> датасет) без изменений.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from config.settings import get_settings


def resolve_path(obj, path: str):
    """Точечный путь по JSON-структуре: ``"data.items"``, ``"user.login"``.

    Поддерживает индексы списков. Пустая строка возвращает сам объект.
    """
    if not path:
        return obj
    for part in path.split("."):
        if obj is None:
            return None
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def parse_datetime(value) -> str | None:
    """ISO-8601 / epoch -> строка ISO; None для чего-либо непонятного."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            return None
    s = str(value).strip()
    parsed = None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat(timespec="seconds")


class FieldMap(BaseModel):
    """Сопоставление полей источника -> поля RawItem (точечные пути)."""

    title: str = "title"
    url: str = "url"
    text: str = "text"
    published_at: str = "published_at"
    author: str = "author"


class APISpec(BaseModel):
    source_id: str
    endpoint: str
    method: Literal["GET", "POST"] = "GET"
    items_path: str = ""            # путь к списку записей в JSON-ответе
    fields: FieldMap = Field(default_factory=FieldMap)
    params: dict = Field(default_factory=dict)
    enabled: bool = True
    limit: int = 20
    fixture: str | None = None


class PageSpec(BaseModel):
    source_id: str
    url: str
    enabled: bool = True
    fixture: str | None = None


class DatasetSpec(BaseModel):
    source_id: str
    source_type: Literal["local", "url", "huggingface"] = "local"
    path: str = ""      # local: путь к файлу; url: URL файла
    name: str = ""      # huggingface: имя датасета
    split: str = "train"
    fmt: Literal["auto", "csv", "jsonl", "json"] = "auto"
    fields: FieldMap = Field(default_factory=FieldMap)
    enabled: bool = True
    limit: int = 100
    fixture: str | None = None


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_api_specs(path: str | None = None) -> list[APISpec]:
    settings = get_settings()
    p = Path(path) if path else settings.config_dir / "apis.yaml"
    if not p.exists():
        return []
    data = _load_yaml(p)
    return [APISpec.model_validate(item) for item in data.get("apis", [])]


def load_page_specs(path: str | None = None) -> list[PageSpec]:
    settings = get_settings()
    p = Path(path) if path else settings.config_dir / "pages.yaml"
    if not p.exists():
        return []
    data = _load_yaml(p)
    return [PageSpec.model_validate(item) for item in data.get("pages", [])]


def load_dataset_specs(path: str | None = None) -> list[DatasetSpec]:
    settings = get_settings()
    p = Path(path) if path else settings.config_dir / "datasets.yaml"
    if not p.exists():
        return []
    data = _load_yaml(p)
    return [DatasetSpec.model_validate(item) for item in data.get("datasets", [])]
