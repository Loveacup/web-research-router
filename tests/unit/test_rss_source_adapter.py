"""Tests for the generic RSS source adapter used by AI HOT and WeChat RSS sources."""
import asyncio

from wrr.engines.community_sources import RssSourceAdapter


SAMPLE_AIHOT_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>AI HOT — 精选</title>
    <link>https://aihot.virxact.com/</link>
    <item>
      <title><![CDATA[OpenClaw on HuggingFace Local App]]></title>
      <link>https://aihot.virxact.com/items/cmr9jbw7a0085ihe85luucw73</link>
      <description><![CDATA[OpenClaw lands on @huggingface local app 🦞🤝🤗

via AI HOT · https://aihot.virxact.com/items/cmr9jbw7a0085ihe85luucw73]]></description>
      <category>AI 产品</category>
      <pubDate>Mon, 06 Jul 2026 17:45:30 GMT</pubDate>
      <guid>cmr9jbw7a0085ihe85luucw73</guid>
      <author>X：OpenClaw (@openclaw)</author>
    </item>
    <item>
      <title><![CDATA[SGLang DSpark decoding]]></title>
      <link>https://aihot.virxact.com/items/cmr9h98co0470slsmqqc2ilv1</link>
      <description><![CDATA[SGLang integrates DSpark speculative decoding.]]></description>
      <pubDate>Mon, 06 Jul 2026 17:11:47 GMT</pubDate>
      <guid>cmr9h98co0470slsmqqc2ilv1</guid>
    </item>
  </channel>
</rss>
"""

SAMPLE_WECHAT_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>WeChat Feed</title>
    <item>
      <title>微信文章一</title>
      <link>https://mp.weixin.qq.com/s/abc123</link>
      <description>摘要一</description>
      <pubDate>Tue, 07 Jul 2026 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


class _FakeAsyncResponse:
    def __init__(self, text, status=200):
        self._text = text
        self.status_code = status

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, response=None):
        self._response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, headers=None):
        self.calls.append((url, headers))
        if self._response is None:
            raise RuntimeError("no response configured")
        return self._response


class _Options:
    query = "今天 AI 圈"
    count = 10


async def noop_run_cmd(cli, timeout):
    return (0, "", "")


def test_rss_adapter_parses_aihot_items():
    adapter = RssSourceAdapter()
    cfg = {"feed_url": "https://aihot.virxact.com/feed.xml", "client": _FakeAsyncClient(_FakeAsyncResponse(SAMPLE_AIHOT_RSS))}
    items = asyncio.run(adapter.fetch(cfg, _Options(), noop_run_cmd, 5.0))

    assert len(items) == 2
    first = items[0]
    assert first["title"] == "OpenClaw on HuggingFace Local App"
    assert first["url"] == "https://aihot.virxact.com/items/cmr9jbw7a0085ihe85luucw73"
    assert "via AI HOT" not in first["snippet"]
    assert first["category"] == "AI 产品"
    assert first["published_at"] is not None
    assert first["sources"] == ["X：OpenClaw (@openclaw)"]

    second = items[1]
    assert second["title"] == "SGLang DSpark decoding"
    assert second["sources"] is None


def test_rss_adapter_parses_wechat_items():
    adapter = RssSourceAdapter()
    cfg = {"feed_url": "https://example.com/wechat.xml", "client": _FakeAsyncClient(_FakeAsyncResponse(SAMPLE_WECHAT_RSS))}
    items = asyncio.run(adapter.fetch(cfg, _Options(), noop_run_cmd, 5.0))

    assert len(items) == 1
    assert items[0]["title"] == "微信文章一"
    assert items[0]["url"] == "https://mp.weixin.qq.com/s/abc123"
    assert items[0]["snippet"] == "摘要一"


def test_rss_adapter_empty_feed_url():
    adapter = RssSourceAdapter()
    items = asyncio.run(adapter.fetch({}, _Options(), noop_run_cmd, 5.0))
    assert items == []


def test_rss_adapter_http_failure_isolated():
    adapter = RssSourceAdapter()
    cfg = {"feed_url": "https://example.com/404", "client": _FakeAsyncClient(None)}
    items = asyncio.run(adapter.fetch(cfg, _Options(), noop_run_cmd, 5.0))
    assert items == []


def test_rss_adapter_malformed_xml_isolated():
    adapter = RssSourceAdapter()
    cfg = {"feed_url": "https://example.com/bad", "client": _FakeAsyncClient(_FakeAsyncResponse("not xml"))}
    items = asyncio.run(adapter.fetch(cfg, _Options(), noop_run_cmd, 5.0))
    assert items == []


def test_detect_sources_wechat_requires_both_keyword_and_feeds(monkeypatch):
    """OMP P3-1 blocker (2026-07-07): wechat_rss must require both keyword and feeds.

    Without this guard, an empty WECHAT_RSS_FEEDS + a WeChat keyword query would
    add wechat_rss as the only source, fetch returns [], and search() raises
    EngineError("community: all sources failed or returned no results").
    """
    import wrr.engines.community as community_mod
    from wrr.engines.community import CommunityEngine
    eng = CommunityEngine.__new__(CommunityEngine)

    # Case 1: feeds empty + keyword present → wechat_rss must NOT be added
    monkeypatch.setattr(community_mod.config, "WECHAT_RSS_FEEDS", ())
    sources = eng._detect_sources("请帮我搜公众号文章")
    assert "wechat_rss" not in sources

    # Case 2: feeds set + no keyword → wechat_rss must NOT be added
    monkeypatch.setattr(community_mod.config, "WECHAT_RSS_FEEDS",
                        ("https://rss.example.com/feed.xml",))
    sources = eng._detect_sources("just some random query")
    assert "wechat_rss" not in sources

    # Case 3: feeds set + keyword present → wechat_rss IS added
    sources = eng._detect_sources("微信公众号推荐")
    assert "wechat_rss" in sources
