"""Тесты ToS-эвристик и аудита источника (fail-closed, запрет сбора)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legitfeed.legal.auditor import RobotsFetcher, audit_source
from legitfeed.legal.registry import Source
from legitfeed.legal.tos import analyze_tos, verdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _source(**overrides) -> Source:
    base = dict(
        id="test_src",
        name="Test",
        feed_url="https://example.org/feed.xml",
        site_url="https://example.org",
        license="CC BY 4.0",
        license_url="https://example.org/license",
        tos_allow_feed=True,
        contains_personal_data=False,
        rate_limit_rps=0.2,
        min_delay_seconds=5.0,
        categories=["test"],
        approved_by="legal-review",
        approved_at="2025-11-01",
    )
    base.update(overrides)
    return Source(**base)


def _fetcher(tmp_path, text: str, status: int = 200) -> RobotsFetcher:
    f = RobotsFetcher(cache_dir=tmp_path / "robots", offline=True)
    url = "https://example.org/robots.txt"
    f.cache_dir.mkdir(parents=True, exist_ok=True)
    f._cache_path(url).write_text(
        json.dumps({"text": text, "status": status}, ensure_ascii=False), encoding="utf-8"
    )
    return f


def test_audit_allows_compliant_source(tmp_path, policy):
    fetcher = _fetcher(tmp_path, "User-agent: *\nAllow: /\nCrawl-delay: 4\n")
    result = audit_source(_source(), policy=policy, robots_fetcher=fetcher)
    assert result.allowed is True
    assert result.crawl_delay == 4.0
    assert result.reasons == []


def test_audit_denies_when_tos_not_allow(tmp_path, policy):
    fetcher = _fetcher(tmp_path, "User-agent: *\nAllow: /\n")
    result = audit_source(_source(tos_allow_feed=False), policy=policy, robots_fetcher=fetcher)
    assert result.allowed is False
    assert any("ToS" in r for r in result.reasons)


def test_audit_denies_unaudited_source_in_strict_mode(tmp_path, policy):
    fetcher = _fetcher(tmp_path, "User-agent: *\nAllow: /\n")
    src = _source(approved_by="", approved_at="")
    assert audit_source(src, policy=policy, robots_fetcher=fetcher, strict=True).allowed is False
    assert audit_source(src, policy=policy, robots_fetcher=fetcher, strict=False).allowed is True


def test_audit_denies_robots_disallow(tmp_path, policy):
    fetcher = _fetcher(tmp_path, "User-agent: *\nDisallow: /feed.xml\n")
    result = audit_source(_source(), policy=policy, robots_fetcher=fetcher)
    assert result.allowed is False
    assert any("robots.txt запрещает" in r for r in result.reasons)


def test_audit_fail_closed_on_server_error(tmp_path, policy):
    fetcher = _fetcher(tmp_path, "", status=503)
    result = audit_source(_source(), policy=policy, robots_fetcher=fetcher)
    assert result.allowed is False
    assert any("fail-closed" in r for r in result.reasons)


def test_audit_denies_without_license(tmp_path, policy):
    fetcher = _fetcher(tmp_path, "User-agent: *\nAllow: /\n")
    result = audit_source(_source(license="  "), policy=policy, robots_fetcher=fetcher)
    assert result.allowed is False
    assert any("лицензия" in r for r in result.reasons)


def test_audit_notes_personal_data_source(tmp_path, policy):
    fetcher = _fetcher(tmp_path, "User-agent: *\nAllow: /\n")
    result = audit_source(
        _source(contains_personal_data=True), policy=policy, robots_fetcher=fetcher
    )
    assert any("152-ФЗ" in r for r in result.reasons)


def test_audit_result_serializable(tmp_path, policy):
    fetcher = _fetcher(tmp_path, "User-agent: *\nRequest-rate: 1/5\n")
    d = audit_source(_source(), policy=policy, robots_fetcher=fetcher).as_dict()
    assert d["request_rate"] == [1.0, 5.0]
    assert isinstance(d["robots_allowed_paths"], dict)


# ---------- ToS-эвристики ----------

def test_tos_detects_russian_prohibition():
    text = (
        "Пользователь обязуется не осуществлять автоматизированный сбор информации "
        "с сайта без письменного согласия правообладателя."
    )
    signals = analyze_tos(text)
    assert verdict(signals) == "block"
    assert signals[0].quote


def test_tos_detects_english_prohibition():
    text = (
        "You may not use any robot, spider or other automated means to access the site "
        "without our prior written permission."
    )
    assert verdict(analyze_tos(text)) == "block"


def test_tos_detects_public_feed_allowance():
    text = "Our RSS feeds are publicly available and free to consume with attribution."
    assert verdict(analyze_tos(text)) == "allow"


def test_tos_unknown_for_neutral_text():
    assert verdict(analyze_tos("Добро пожаловать на наш сайт. Здесь публикуются статьи.")) == (
        "unknown"
    )


def test_tos_empty_text():
    assert analyze_tos("") == []


def test_tos_block_wins_over_allow():
    text = (
        "Our RSS feeds are publicly available. However, you may not crawl or scrape "
        "the site without prior written permission."
    )
    assert verdict(analyze_tos(text)) == "block"


@pytest.mark.parametrize("action", ["bypass_paywall", "bypass_captcha", "parameter_fuzzing"])
def test_policy_forbids_illegal_actions(policy, action):
    with pytest.raises(PermissionError):
        policy.assert_action_allowed(action)


def test_policy_allows_normal_action(policy):
    policy.assert_action_allowed("respect_robots_txt")  # не должно бросать


def test_policy_config_contains_legal_requirements(policy):
    assert policy.robots.fail_closed_on_server_error is True
    assert policy.terms_of_service.require_tos_allow_feed is True
    assert policy.personal_data.mask_pii is True
    assert "bypass_authorization" in policy.forbidden_actions


def test_registry_sources_have_license_and_feed_urls(registry):
    assert len(registry) >= 3
    for source in registry.sources.values():
        assert source.license.strip()
        assert str(source.feed_url).startswith("http")


def test_registry_rejects_unknown_source(registry):
    with pytest.raises(KeyError):
        registry.get("does_not_exist")
