"""Общие pytest-фикстуры: изоляция БД/каталогов, offline-настройки."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def tmp_settings(tmp_path, monkeypatch):
    """Settings с изолированными каталогами и SQLite в tmp_path, offline."""
    from config.settings import Settings

    monkeypatch.setenv("LEGITFEED_OFFLINE", "true")
    db_url = f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}"
    settings = Settings(
        database_url=db_url,
        data_dir=tmp_path / "data",
        config_dir=PROJECT_ROOT / "config",
        offline=True,
        cache_ttl_seconds=3600,
    )
    return settings


@pytest.fixture()
def repo(tmp_settings):
    from legitfeed.storage.repository import Repository

    r = Repository(db_url := tmp_settings.database_url)
    yield r
    r.dispose()


@pytest.fixture()
def policy():
    from legitfeed.legal.policy import load_policy

    return load_policy(str(PROJECT_ROOT / "config" / "legal_policy.yaml"))


@pytest.fixture()
def registry():
    from legitfeed.legal.registry import load_registry

    return load_registry(
        PROJECT_ROOT / "config" / "sources.yaml", PROJECT_ROOT / "config" / "feeds.yaml"
    )


@pytest.fixture()
def robots_text():
    return (PROJECT_ROOT / "tests" / "fixtures" / "robots" / "habr_robots.txt").read_text(
        encoding="utf-8"
    )


@pytest.fixture()
def habr_feed():
    return (PROJECT_ROOT / "tests" / "fixtures" / "habr_develop.xml").read_text(encoding="utf-8")


@pytest.fixture()
def arxiv_feed():
    return (PROJECT_ROOT / "tests" / "fixtures" / "arxiv_cs_lg.xml").read_text(encoding="utf-8")


@pytest.fixture()
def sample_docs():
    """Набор CleanDoc для тестов детектора/датасета."""
    from legitfeed.models import CleanDoc

    base = (
        "В этой статье мы разбираем построение ETL-пайплайна на Python и Airflow. "
        "Показываем, как настроить DAG, очистку данных и валидацию на уровне загрузки. "
        "Обсуждаем мониторинг и метрики выполнения задач в продакшене."
    )
    docs = []
    for i in range(6):
        text = f"{base} Выпуск номер {i}."
        docs.append(
            CleanDoc(
                doc_id=f"doc{i}",
                source_id="src",
                url=f"https://example.org/p/{i}",
                canonical_url=f"https://example.org/p/{i}",
                title=f"ETL пайплайн, выпуск {i}",
                text=text,
                summary=text[:200],
                language="ru",
                snapshot_at="2025-11-20T00:00:00+00:00",
                license="CC BY 4.0",
                license_url="https://example.org/license",
                word_count=len(text.split()),
                char_count=len(text),
            )
        )
    return docs


@pytest.fixture(autouse=True)
def _clean_env():
    """Не даём переменным окружения «протечь» между тестами."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("LEGITFEED_")}
    yield
    for k in [k for k in os.environ if k.startswith("LEGITFEED_")]:
        del os.environ[k]
    os.environ.update(saved)
