"""Тесты robots.txt: RFC 9309-семантика (группы, wildcard, приоритет, fail-closed)."""

from __future__ import annotations

import pytest

from legitfeed.legal.robots import RobotsTxt, canonical_url

UA = "LegitFeedBot/0.1 (+https://github.com/example/legitfeed)"

RFC_SAMPLE = """
# Пример из RFC 9309 §2.2.2
User-agent: *
Disallow: /private/
Allow: /private/public/
Disallow: /*.pdf$
"""


def test_empty_robots_allows_everything():
    robots = RobotsTxt.parse("")
    assert robots.is_allowed("/anything", UA) is True


def test_disallow_root_blocks_all():
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /\n")
    assert robots.is_allowed("/", UA) is False
    assert robots.is_allowed("/news", UA) is False


def test_empty_disallow_allows_all():
    robots = RobotsTxt.parse("User-agent: *\nDisallow:\n")
    assert robots.is_allowed("/news", UA) is True


def test_rfc_sample_rules():
    robots = RobotsTxt.parse(RFC_SAMPLE)
    assert robots.is_allowed("/private/data", UA) is False
    assert robots.is_allowed("/private/public/x", UA) is True  # более длинное Allow
    assert robots.is_allowed("/docs/report.pdf", UA) is False  # wildcard + $


def test_wildcard_in_middle():
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /*/admin/\n")
    assert robots.is_allowed("/ru/admin/x", UA) is False
    assert robots.is_allowed("/ru/user/x", UA) is True


def test_most_specific_user_agent_group_wins():
    text = (
        "User-agent: *\n"
        "Disallow: /\n"
        "\n"
        "User-agent: LegitFeedBot\n"
        "Allow: /\n"
    )
    robots = RobotsTxt.parse(text)
    # Для нас выбрана группа с самым специфичным токеном
    assert robots.is_allowed("/feed", UA) is True
    # Для другого бота действует *
    assert robots.is_allowed("/feed", "OtherBot/1.0") is False


def test_longest_match_wins_regardless_of_order():
    text = "User-agent: *\nAllow: /a\nDisallow: /a/b\n"
    robots = RobotsTxt.parse(text)
    assert robots.is_allowed("/a", UA) is True
    assert robots.is_allowed("/a/b", UA) is False
    assert robots.is_allowed("/a/bc", UA) is False


def test_tie_prefers_allow():
    text = "User-agent: *\nDisallow: /same\nAllow: /same\n"
    robots = RobotsTxt.parse(text)
    assert robots.is_allowed("/same", UA) is True


def test_query_string_is_part_of_path():
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /*?sort=\n")
    assert robots.is_allowed("/list?sort=date", UA) is False
    assert robots.is_allowed("/list?page=2", UA) is True


def test_crawl_delay_parsed_and_max_used():
    robots = RobotsTxt.parse(
        "User-agent: *\nCrawl-delay: 1\n\nUser-agent: LegitFeedBot\nCrawl-delay: 7\n"
    )
    assert robots.crawl_delay(UA) == 7.0


def test_request_rate_parsed():
    robots = RobotsTxt.parse("User-agent: *\nRequest-rate: 1/10\n")
    assert robots.request_rate(UA) == (1.0, 10.0)
    assert robots.min_delay_from_rate(UA) == 10.0


def test_unavailable_is_fail_closed():
    robots = RobotsTxt.unavailable_response(status=503)
    assert robots.unavailable is True
    assert robots.is_allowed("/", UA) is False


@pytest.mark.parametrize("status", [401, 403])
def test_auth_required_is_full_block(status):
    robots = RobotsTxt.unavailable_response(status=status)
    assert robots.forbidden_all is True
    assert robots.is_allowed("/feed", UA) is False


def test_no_fallback_to_wildcard_when_specific_group_exists():
    """RFC 9309 §2.2.1: если есть группа для нашего бота — * игнорируется."""
    text = (
        "User-agent: *\n"
        "Disallow: /feed\n"
        "\n"
        "User-agent: LegitFeedBot\n"
        "Allow: /feed\n"
    )
    robots = RobotsTxt.parse(text)
    assert robots.is_allowed("/feed", UA) is True


def test_sitemap_collected():
    robots = RobotsTxt.parse(RFC_SAMPLE + "Sitemap: https://x.org/sitemap.xml\n")
    assert robots.sitemaps == ["https://x.org/sitemap.xml"]


def test_comments_ignored():
    robots = RobotsTxt.parse("User-agent: * # bot\nDisallow: /x # secret\n")
    assert robots.is_allowed("/x", UA) is False
    assert robots.is_allowed("/y", UA) is True


def test_canonical_url_normalization():
    assert canonical_url("https://WWW.Example.com/path/") == "https://example.com/path"
    assert canonical_url("https://example.com/path#frag") == "https://example.com/path"
    assert canonical_url("https://example.com") == "https://example.com/"
