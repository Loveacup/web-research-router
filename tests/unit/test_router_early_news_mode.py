"""Tests for P3-3 early-news routing mode promotion."""
import pytest

from wrr import config
from wrr.router import build_chain
from wrr.engines.community import CommunityEngine


class _FakeRegistry:
    def __init__(self):
        self.engines = {}

    def register(self, name, engine):
        self.engines[name] = engine

    def get(self, name):
        return self.engines.get(name)


def test_build_chain_promotes_community_for_early_news():
    """early-news trigger should put community at the front of the chain."""
    for q in ("今天 AI 热点", "ai 早报", "latest ai", "AI 行业动态"):
        chain = build_chain("search", None, q)
        assert chain[0] == "community", f"failed for {q}: {chain}"


def test_build_chain_github_still_first_for_site_github():
    """site:github.com should still win over early-news community promotion."""
    chain = build_chain("search", None, "site:github.com AI 早报")
    assert chain[0] == "github"
    assert "community" in chain[:2]


def test_build_chain_extract_untouched():
    chain = build_chain("extract", None, "ai 早报")
    assert chain == list(config.EXTRACT_FALLBACK_ORDER)


def test_detect_sources_adds_rss_for_early_news():
    eng = CommunityEngine.__new__(CommunityEngine)
    sources = eng._detect_sources("今天 ai 有什么热点")
    assert "aihot_rss" in sources


def test_detect_sources_wechat_rss_requires_config_for_early_news(monkeypatch):
    import wrr.engines.community as community_mod

    eng = CommunityEngine.__new__(CommunityEngine)

    # Without feed configured, early-news still adds aihot_rss but not wechat_rss
    monkeypatch.setattr(community_mod.config, "WECHAT_RSS_FEEDS", ())
    sources = eng._detect_sources("AI 早报")
    assert "aihot_rss" in sources
    assert "wechat_rss" not in sources

    # With feed configured, early-news adds both
    monkeypatch.setattr(community_mod.config, "WECHAT_RSS_FEEDS",
                        ("https://rss.example.com/feed.xml",))
    sources = eng._detect_sources("AI 早报")
    assert "aihot_rss" in sources
    assert "wechat_rss" in sources
