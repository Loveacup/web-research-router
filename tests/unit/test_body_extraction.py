"""Local article extraction consumes HTML without expanding fetch permissions."""
import asyncio

import httpx
import pytest
from httpx._client import AsyncClient

from wrr.engines.brave import BraveEngine
from wrr.extraction import extraction_error
from wrr.schemas import ExtractOptions


ARTICLE = """<!doctype html><html><head>
<title>Field observations</title>
<link rel="stylesheet" href="http://127.0.0.1/private.css">
<link rel="canonical" href="http://127.0.0.1/private-metadata">
<style>STYLE_SENTINEL { color: red; }</style>
<script src="http://127.0.0.1/private.js">SCRIPT_SENTINEL</script>
</head><body>
<nav><a href="/home">NAVIGATION_SENTINEL</a><a href="/shop">SHOP_SENTINEL</a></nav>
<main><article><h1>Field observations</h1>
<p>Researchers measured the river every morning throughout the winter.
The instruments recorded water temperature, current speed, and dissolved oxygen.
These repeated measurements allowed the team to distinguish daily variation
from the longer seasonal trend without relying on isolated observations.</p>
<p>The final analysis found that colder days were associated with more dissolved
oxygen. Independent teams repeated the measurements at three neighboring sites
and obtained similar results. The report describes the instruments and their
calibration so that other researchers can reproduce the procedure.</p>
<p>Fish &amp; water remained the central focus of the field study.</p>
<img src="http://127.0.0.1/private-image" alt="IMAGE_SENTINEL">
</article></main>
<div id="comments"><p>COMMENT_SENTINEL: Reader discussion is not article text.</p></div>
<footer><a href="/legal">FOOTER_SENTINEL</a></footer>
</body></html>"""


def extract_fixture(monkeypatch, html, *, max_characters=5000):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=html)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: AsyncClient(
        **kwargs, transport=httpx.MockTransport(respond)))
    result = asyncio.run(BraveEngine().extract(ExtractOptions(
        "https://8.8.8.8/article", max_characters=max_characters)))
    # Linked images, scripts, canonical metadata, and styles never get fetched.
    assert len(requests) == 1
    assert str(requests[0].url) == "https://8.8.8.8/article"
    return result


def test_article_body_survives_without_navigation_or_executable_content(monkeypatch):
    result = extract_fixture(monkeypatch, ARTICLE)
    assert "Researchers measured the river every morning" in result.text
    assert "other researchers can reproduce the procedure" in result.text
    assert "Fish & water" in result.text
    assert "SENTINEL" not in result.text
    assert "<p>" not in result.text
    assert result.content_kind == "body"
    assert result.content_type == "text/plain"
    assert result.completeness == "unknown"
    assert result.partial is True
    assert extraction_error(result) is None


@pytest.mark.parametrize("limit", [31, 0, -5])
def test_character_limit_applies_to_extracted_body(monkeypatch, limit):
    full = extract_fixture(monkeypatch, ARTICLE)
    clipped = extract_fixture(monkeypatch, ARTICLE, max_characters=limit)
    assert clipped.text == full.text[:max(0, limit)]
    assert clipped.completeness == "truncated"
    assert clipped.partial is True


@pytest.mark.parametrize("html", [
    "",
    "<html><body><script>window.app = 1;</script><style>body {color:red}</style></body></html>",
    '<html><body><nav><a href="/">Home</a><a href="/login">Login</a></nav></body></html>',
])
def test_empty_or_boilerplate_page_is_not_a_complete_body(monkeypatch, html):
    result = extract_fixture(monkeypatch, html)
    assert result.text == ""
    assert result.content_kind == "empty"
    assert result.completeness == "unknown"
    assert result.partial is True
    assert extraction_error(result) == "empty_body"


@pytest.mark.parametrize("message", [
    "Just a moment... Enable JavaScript and cookies to continue.",
    "Verify you are human. Cloudflare security captcha verification required.",
])
@pytest.mark.parametrize("limit", [5000, 5])
@pytest.mark.parametrize("tag", ["p", "div"])
def test_challenge_page_is_rejected_even_when_parser_discards_it(monkeypatch, message, limit, tag):
    result = extract_fixture(
        monkeypatch, f"<html><body><{tag}>{message}</{tag}></body></html>",
        max_characters=limit,
    )
    if result.text:
        # A retained interstitial must be classified before character clipping.
        assert result.content_kind == "challenge"
        assert result.completeness == ("truncated" if limit == 5 else "unknown")
        assert extraction_error(result) == "challenge_page"
    else:
        # Precision extraction may discard the interstitial altogether.
        assert result.content_kind == "empty"
        assert result.completeness == "unknown"
        assert extraction_error(result) == "empty_body"
    assert result.partial is True


def test_long_article_discussing_challenges_remains_body(monkeypatch):
    discussion = ("Just a moment: enable JavaScript and cookies is a challenge-page phrase. "
                  "This article explains how websites display that message to readers. ") * 25
    result = extract_fixture(monkeypatch, f"<article><p>{discussion}</p></article>")
    assert len(result.text) > 2000
    assert result.content_kind == "body"
    assert extraction_error(result) is None


@pytest.mark.parametrize("wrapper", ["span", "b", "strong", "i", "em", "q"])
@pytest.mark.parametrize("block", ["pre", "blockquote"])
def test_empty_inline_marker_keeps_following_main_text(monkeypatch, wrapper, block):
    # Pygments emits an empty span before the entire PEP 20 text. Its tail is
    # content, not disposable whitespace, including inside preformatted blocks.
    aphorisms = (
        "Beautiful is better than ugly.\n"
        "Explicit is better than implicit.\n"
        "Simple is better than complex.\n"
        "Readability counts.\n"
    )
    html = (
        '<html><body><nav><a href="/">NAVIGATION_SENTINEL</a></nav>'
        '<main><article><h1>Guiding principles</h1>'
        '<p>The following principles guide the design of this programming language.</p>'
        f'<div class="highlight"><{block}><{wrapper}></{wrapper}>{aphorisms}</{block}></div>'
        '<p>These principles explain how to evaluate competing implementation choices.</p>'
        '</article></main></body></html>'
    )
    result = extract_fixture(monkeypatch, html)
    for aphorism in aphorisms.splitlines():
        assert aphorism in result.text
    assert result.text.index("Beautiful is better") < result.text.index("Readability counts")
    assert "NAVIGATION_SENTINEL" not in result.text
    assert result.content_kind == "body"
    assert result.completeness == "unknown"


def test_quoted_line_breaks_retain_each_line(monkeypatch):
    html = (
        '<html><body><article><h1>Design notes</h1>'
        '<p>The author supplied these principles as a quotation.</p>'
        '<blockquote><p><span></span>Beautiful is better than ugly.<br>'
        'Explicit is better than implicit.<br>Readability counts.</p></blockquote>'
        '<p>These principles remain useful when reviewing a proposed design.</p>'
        '</article></body></html>'
    )
    result = extract_fixture(monkeypatch, html)
    assert "Beautiful is better than ugly." in result.text
    assert "Explicit is better than implicit." in result.text
    assert "Readability counts." in result.text


@pytest.mark.parametrize("fragment", [
    "<p>Authorized fixture body</p>",
    "<article><p>Authorized fixture body</p></article>",
    "<article><h1>Fixture heading</h1><p>Authorized fixture body</p></article>",
])
def test_valid_html_fragments_keep_their_body(monkeypatch, fragment):
    result = extract_fixture(monkeypatch, fragment)
    assert "Authorized fixture body" in result.text
    assert result.content_kind == "body"
    assert extraction_error(result) is None


@pytest.mark.parametrize("html", [
    '<nav><a href="/">Home</a><a href="/login">Login</a></nav>',
    '<html><body><div role="navigation"><a href="/">Home</a>'
    '<a href="/login">Login</a></div></body></html>',
])
def test_navigation_cannot_be_rescued_as_article_text(monkeypatch, html):
    result = extract_fixture(monkeypatch, html)
    assert result.text == ""
    assert result.content_kind == "empty"
    assert extraction_error(result) == "empty_body"
