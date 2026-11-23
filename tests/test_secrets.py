"""Тесты секретов API: чтение из env, отсутствие хранения в конфиге, применение."""

from __future__ import annotations

import pytest

from legitfeed.collect.api_collector import APICollector
from legitfeed.collect.secrets import MissingSecretError, resolve_secret
from legitfeed.collect.specs import APISpec, AuthSpec


def test_resolve_secret_from_env(monkeypatch):
    monkeypatch.setenv("LEGITFEED_TEST_TOKEN", "secret123")
    assert resolve_secret("LEGITFEED_TEST_TOKEN") == "secret123"


def test_missing_secret_raises(monkeypatch):
    monkeypatch.delenv("LEGITFEED_NO_SUCH_TOKEN", raising=False)
    with pytest.raises(MissingSecretError):
        resolve_secret("LEGITFEED_NO_SUCH_TOKEN")


def test_blank_secret_raises(monkeypatch):
    monkeypatch.setenv("LEGITFEED_EMPTY_TOKEN", "   ")
    with pytest.raises(MissingSecretError):
        resolve_secret("LEGITFEED_EMPTY_TOKEN")


def test_auth_spec_stores_only_env_name():
    # в модели нет поля для хранения самого секрета — только имя переменной
    spec = AuthSpec(type="header", env="LEGITFEED_TOKEN", header_name="X-Token")
    assert spec.env == "LEGITFEED_TOKEN"
    for forbidden in ("token", "secret", "key", "password"):
        assert not hasattr(spec, forbidden)


@pytest.mark.parametrize(
    "kind,header_name,param_name,expected_key,expected_value",
    [
        ("bearer", None, None, "Authorization", "Bearer tok123"),
        ("header", "X-H", None, "X-H", "tok123"),
        ("query", None, "key", "key", "tok123"),
    ],
)
def test_resolve_auth_builds_request(
    monkeypatch, tmp_settings, policy, kind, header_name, param_name, expected_key, expected_value
):
    monkeypatch.setenv("LEGITFEED_T", "tok123")
    spec = APISpec(
        source_id="x",
        endpoint="http://e.test/api",
        auth=AuthSpec(type=kind, env="LEGITFEED_T",
                      header_name=header_name, param_name=param_name),
    )
    collector = APICollector(settings=tmp_settings, policy=policy)
    headers, params = collector._resolve_auth(spec)
    if kind == "query":
        assert params[expected_key] == expected_value
    else:
        assert headers[expected_key] == expected_value
