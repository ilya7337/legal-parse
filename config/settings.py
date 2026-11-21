"""Настройки проекта (pydantic-settings, переменные окружения с префиксом LEGITFEED_)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


class Settings(BaseSettings):
    """Конфигурация приложения.

    Все значения можно переопределить переменной окружения ``LEGITFEED_<ИМЯ>``,
    например ``LEGITFEED_DATABASE_URL=postgresql+psycopg2://...``.
    """

    model_config = SettingsConfigDict(
        env_prefix="LEGITFEED_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- хранилище ---
    database_url: str = Field(
        default=f"sqlite+pysqlite:///{(PROJECT_ROOT / 'legitfeed.db').as_posix()}"
    )

    # --- файловая система ---
    data_dir: Path = PROJECT_ROOT / "data"
    config_dir: Path = CONFIG_DIR

    # --- сеть / вежливый краулинг ---
    user_agent: str = (
        "LegitFeedBot/0.1 (+https://github.com/example/legitfeed; "
        "educational research crawler; contact: research@example.org)"
    )
    request_timeout: float = 20.0
    max_retries: int = 3
    backoff_base_seconds: float = 1.5
    # глобальный «потолок» вежливости: даже если источник разрешает чаще —
    # не быстрее этого значения
    max_global_rps: float = 2.0
    respect_robots: bool = True
    cache_ttl_seconds: int = 900
    offline: bool = False  # только локальные фикстуры, без выхода в сеть

    # --- очистка ---
    min_text_chars: int = 120
    mask_pii: bool = True
    language: Literal["auto", "ru", "en"] = "auto"

    # --- аномалии ---
    zscore_threshold: float = 3.0
    duplicate_similarity_threshold: float = 0.92
    anomaly_min_history: int = 5

    # --- датасет ---
    train_ratio: float = 0.8
    val_ratio: float = 0.1
    test_ratio: float = 0.1
    random_seed: int = 42

    # --- юридическая политика ---
    legal_policy_file: Path | None = None
    sources_file: Path | None = None
    feeds_file: Path | None = None

    def path(self, name: str) -> Path:
        """Путь внутри ``data/``; каталог создаётся при обращении."""
        p = (self.data_dir / name).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def legal_policy_path(self) -> Path:
        return self.legal_policy_file or (self.config_dir / "legal_policy.yaml")

    @property
    def sources_path(self) -> Path:
        return self.sources_file or (self.config_dir / "sources.yaml")

    @property
    def feeds_path(self) -> Path:
        return self.feeds_file or (self.config_dir / "feeds.yaml")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Для тестов: сбросить кэш настроек после смены env."""
    get_settings.cache_clear()
