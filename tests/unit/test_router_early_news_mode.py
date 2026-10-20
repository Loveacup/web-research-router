"""Community source detection for early-news queries."""
from wrr.engines.community import CommunityEngine


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
