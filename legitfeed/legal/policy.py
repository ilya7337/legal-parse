"""Машиночитаемая юридическая политика (config/legal_policy.yaml).

Политика — единая точка правды: и рантайм (сборщик), и тесты читают одни и
те же правила. Любое действие, попадающее в ``forbidden_actions``, запрещено
на уровне архитектуры: система не содержит кода, который их выполняет.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from config.settings import get_settings


class RobotsPolicy(BaseModel):
    fail_closed_on_server_error: bool = True
    disallow_on_auth_required: bool = True
    honor_crawl_delay: bool = True
    honor_request_rate: bool = True
    check_both_site_and_feed: bool = True


class RatePolicy(BaseModel):
    max_global_rps: float = 2.0
    min_delay_seconds: float = 2.0
    respect_retry_after: bool = True
    backoff_base_seconds: float = 1.5
    max_retries: int = 3


class TosPolicy(BaseModel):
    require_tos_allow_feed: bool = True
    require_source_audit: bool = True


class PersonalDataPolicy(BaseModel):
    mask_pii: bool = True
    mask_types: list[str] = Field(
        default_factory=lambda: ["email", "phone", "snils", "passport", "url_with_query_token", "ip"]
    )
    forbidden_fields: list[str] = Field(default_factory=list)
    skip_comments: bool = True


class StoragePolicy(BaseModel):
    keep_raw_html: bool = False
    raw_retention_days: int = 30
    provenance_required: bool = True


class AttributionPolicy(BaseModel):
    respect_copyright: bool = True
    export_source_license: bool = True
    max_excerpt_chars: int = 1000


class LegalPolicy(BaseModel):
    version: int = 1
    robots: RobotsPolicy = Field(default_factory=RobotsPolicy)
    rate_limiting: RatePolicy = Field(default_factory=RatePolicy)
    terms_of_service: TosPolicy = Field(default_factory=TosPolicy)
    personal_data: PersonalDataPolicy = Field(default_factory=PersonalDataPolicy)
    forbidden_actions: list[str] = Field(default_factory=list)
    storage: StoragePolicy = Field(default_factory=StoragePolicy)
    attribution: AttributionPolicy = Field(default_factory=AttributionPolicy)

    @classmethod
    def from_yaml(cls, path: Path) -> LegalPolicy:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def assert_action_allowed(self, action: str) -> None:
        """Страж: поднимает исключение, если действие запрещено политикой."""
        if action in self.forbidden_actions:
            raise PermissionError(
                f"Действие '{action}' запрещено юридической политикой проекта "
                f"(config/legal_policy.yaml -> forbidden_actions)"
            )


@lru_cache(maxsize=4)
def load_policy(path: str | None = None) -> LegalPolicy:
    policy_path = Path(path) if path else get_settings().legal_policy_path
    if not policy_path.exists():
        return LegalPolicy()
    return LegalPolicy.from_yaml(policy_path)
