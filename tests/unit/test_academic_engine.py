"""AcademicEngine 单测：纯评分/去重函数 + search 聚合（monkeypatch 模块级 _fetch_*）。

零网络：通过替换模块级 `_fetch_openalex` / `_fetch_s2` 注入 canned paper_dict 列表，
骨架对齐 test_community.py（asyncio.run + 手动 swap 模块级函数）。
"""
import asyncio
import math
from datetime import datetime, timezone
import httpx
import pytest

from wrr.engines._academic_query import academic_query, biomedical_query, biomedical_scope

from wrr.engines import academic as ac
from wrr.engines._fusion import recency_decay
from wrr.schemas import SearchOptions
from wrr.errors import EngineError
from wrr import config


def run(coro):
    return asyncio.run(coro)


# ── canned paper_dict 工厂 ──────────────────────────────────────────
def _paper(**kw):
    base = {
        "title": "T", "cited_by_count": 0, "influential_citations": 0,
        "pub_date": None, "is_preprint": False, "venue": "", "venue_rank": 0.0,
        "doi": "", "arxiv_id": "", "openalex_id": "", "source": "openalex",
        "source_relevance": 1.0, "tldr": "", "abstract": "",
        "doi_url": "", "landing_url": "",
    }
    base.update(kw)
    return base


# ── 纯函数：citation_velocity ────────────────────────────────────────
def test_citation_velocity_old_does_not_dominate_new():
    # 老论文：引用高（200）但年龄大（20y）→ 年均 10/yr
    old = ac.citation_velocity(200, 20.0)
    # 新论文：引用中（50）但年轻（1y）→ 年均 50/yr
    new = ac.citation_velocity(50, 1.0)
    assert new > old                                  # 年均引用高者胜，非绝对引用数
    assert 0.0 < old < 1.0 and 0.0 < new <= 1.0


def test_citation_velocity_bounds_and_monotonic():
    assert ac.citation_velocity(0, 1.0) == 0.0
    assert ac.citation_velocity(10**6, 1.0) == 1.0    # 钳位到 1.0
    # 同龄下引用越多 velocity 越大
    assert ac.citation_velocity(100, 2.0) > ac.citation_velocity(10, 2.0)


# ── 纯函数：academic_score 手算一致 ─────────────────────────────────
def test_academic_score_matches_manual():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pub = datetime(2024, 1, 1, tzinfo=timezone.utc)
    p = _paper(cited_by_count=100, influential_citations=20, pub_date=pub,
               is_preprint=False, venue="NeurIPS", venue_rank=0.5,
               source_relevance=0.8, source="semantic_scholar")

    w_v, w_a, w_r, w_rel = config.ACADEMIC_SCORE_WEIGHTS
    age_years = max((now - pub).days / 365.0, 0.25)
    velocity = ac.citation_velocity(100, age_years)
    infl_ratio = 20 / 100
    authority = min(0.6 * 0.5 + 0.4 * min(infl_ratio * 3, 1.0), 1.0)   # 0.54
    age_hours = (now - pub).total_seconds() / 3600.0
    recency = recency_decay(age_hours, config.ACADEMIC_RECENCY_HALFLIFE_DAYS * 24.0)
    expected = w_v * velocity + w_a * authority + w_r * recency + w_rel * 0.8

    assert abs(ac.academic_score(p, now) - expected) < 1e-9


def test_academic_score_preprint_recency_boost():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pub = datetime(2025, 10, 1, tzinfo=timezone.utc)
    pre = _paper(pub_date=pub, is_preprint=True, source="arxiv")
    pub_paper = _paper(pub_date=pub, is_preprint=False)
    # 同日期、同引用，预印本 recency ×1.2 → 总分更高（其余维度相同/更低）
    assert ac.academic_score(pre, now) > ac.academic_score(pub_paper, now) - 1e-9
    # 显式核对 recency boost 进了分（预印本 recency 维度更高）
    age_hours = (now - pub).total_seconds() / 3600.0
    base_rec = recency_decay(age_hours, config.ACADEMIC_RECENCY_HALFLIFE_DAYS * 24.0)
    assert min(base_rec * 1.2, 1.0) > base_rec


# ── 纯函数：dedup_by_doi 跨源合并 ───────────────────────────────────
def test_dedup_by_doi_merges_cross_source_max_citation():
    oa = _paper(title="Attention Is All You Need", doi="10.1/abc",
                cited_by_count=1000, source="openalex", venue="NeurIPS",
                venue_rank=0.5, doi_url="https://doi.org/10.1/abc")
    s2 = _paper(title="Attention is all you need", doi="10.1/ABC",   # 大小写不同同 DOI
                cited_by_count=1200, influential_citations=300,
                source="semantic_scholar", tldr="seminal transformer paper")
    out = ac.dedup_by_doi([oa, s2])
    assert len(out) == 1                              # 同 DOI 合并为 1 条
    m = out[0]
    assert m["cited_by_count"] == 1200               # 引用取 max
    assert m["influential_citations"] == 300         # 从 s2 保留
    assert m["tldr"] == "seminal transformer paper"  # 补全缺字段
    assert m["venue"] == "NeurIPS"                    # 正式版权威保留
    assert set(m["cite_sources"]) == {"openalex", "semantic_scholar"}  # 标注双源
    assert m["cite_sources"]["semantic_scholar"] == 1200


def test_dedup_by_doi_arxiv_fallback_and_distinct():
    a = _paper(title="P1", arxiv_id="2401.001", source="arxiv", cited_by_count=5)
    b = _paper(title="P1 published", arxiv_id="2401.001", source="openalex",
               cited_by_count=8, venue="ICML", venue_rank=0.5)
    c = _paper(title="totally other", doi="10.9/zzz", source="openalex")
    out = ac.dedup_by_doi([a, b, c])
    assert len(out) == 2                              # a/b 同 arXiv ID 合并，c 独立
    merged = next(p for p in out if p.get("arxiv_id") == "2401.001")
    assert merged["cited_by_count"] == 8             # max
    assert merged["is_preprint"] is False            # 有正式版 → 不再算预印本
    assert merged["venue"] == "ICML"


# ── search 聚合（monkeypatch 模块级 _fetch_*）───────────────────────
def _fake_async(payload):
    async def _f(options, client=None):
        return list(payload)
    return _f


def _fake_raise(exc):
    async def _f(options, client=None):
        raise exc
    return _f


def test_search_aggregates_and_dedups(monkeypatch):
    monkeypatch.setattr(config, "ACADEMIC_INCLUDE_ARXIV", False)
    oa = [
        _paper(title="Paper A", doi="10.1/a", cited_by_count=500,
               source="openalex", pub_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
               venue="NeurIPS", venue_rank=0.5, source_relevance=1.0),
        _paper(title="Paper B", doi="10.1/b", cited_by_count=10, source="openalex",
               pub_date=datetime(2018, 1, 1, tzinfo=timezone.utc), source_relevance=0.5),
    ]
    s2 = [
        _paper(title="Paper A dup", doi="10.1/A", cited_by_count=600,    # 与 oa[0] 同 DOI
               influential_citations=120, source="semantic_scholar",
               tldr="great paper", source_relevance=1.0),
        _paper(title="Paper C", doi="10.1/c", cited_by_count=80,
               source="semantic_scholar",
               pub_date=datetime(2025, 6, 1, tzinfo=timezone.utc), source_relevance=0.7),
    ]
    monkeypatch.setattr(ac, "_fetch_openalex", _fake_async(oa))
    monkeypatch.setattr(ac, "_fetch_s2", _fake_async(s2))

    out = run(ac.AcademicEngine().search(SearchOptions("transformer", count=10)))
    titles = [r.title for r in out]
    assert "Paper A" in titles                        # 代表取首见（openalex 标题）
    assert "Paper A dup" not in titles                # DOI 去重生效
    assert len([t for t in titles if t.startswith("Paper A")]) == 1
    assert {"Paper A", "Paper B", "Paper C"} == set(titles)  # 3 篇唯一
    # 合并条 snippet 含引用数 + 来源标签前缀正确
    pa = next(r for r in out if r.title == "Paper A")
    assert "cit" in pa.snippet
    assert pa.source_tag.startswith("academic:")
    assert pa.url == "https://doi.org/10.1/a" or pa.url.startswith("http")


def test_search_isolates_failing_source(monkeypatch):
    monkeypatch.setattr(config, "ACADEMIC_INCLUDE_ARXIV", False)
    oa = [_paper(title="Survived", doi="10.1/ok", cited_by_count=42, source="openalex")]
    monkeypatch.setattr(ac, "_fetch_openalex", _fake_async(oa))
    monkeypatch.setattr(ac, "_fetch_s2", _fake_raise(RuntimeError("s2 down")))

    out = run(ac.AcademicEngine().search(SearchOptions("x", count=5)))
    assert [r.title for r in out] == ["Survived"]     # 单源失败被隔离，其余正常


def test_search_all_failed_raises(monkeypatch):
    monkeypatch.setattr(config, "ACADEMIC_INCLUDE_ARXIV", False)
    monkeypatch.setattr(ac, "_fetch_openalex", _fake_raise(RuntimeError("down")))
    monkeypatch.setattr(ac, "_fetch_s2", _fake_async([]))
    monkeypatch.setattr(ac, "_fetch_crossref", _fake_async([]))
    try:
        run(ac.AcademicEngine().search(SearchOptions("x")))
        assert False, "all failed should raise"
    except EngineError as e:
        assert "academic" in str(e).lower()


def test_search_respects_count(monkeypatch):
    monkeypatch.setattr(config, "ACADEMIC_INCLUDE_ARXIV", False)
    oa = [_paper(title=f"P{i}", doi=f"10.1/{i}", cited_by_count=i, source="openalex")
          for i in range(5)]
    monkeypatch.setattr(ac, "_fetch_openalex", _fake_async(oa))
    monkeypatch.setattr(ac, "_fetch_s2", _fake_async([]))
    out = run(ac.AcademicEngine().search(SearchOptions("x", count=2)))
    assert len(out) == 2


# ── 解析器纯函数（OpenAlex / S2 字段映射）───────────────────────────
def test_parse_openalex_work_fields():
    work = {
        "title": "Deep Learning",
        "cited_by_count": 12345,
        "publication_date": "2015-05-28",
        "type": "article",
        "ids": {"doi": "https://doi.org/10.1038/nature14539", "openalex": "W123"},
        "primary_location": {"source": {"display_name": "Nature"},
                             "landing_page_url": "https://nature.com/x"},
    }
    p = ac._parse_openalex_work(work, 0, 1)
    assert p["doi"] == "10.1038/nature14539"          # 剥前缀小写
    assert p["venue"] == "Nature" and p["venue_rank"] == 0.5
    assert p["is_preprint"] is False
    assert p["source"] == "openalex"
    assert p["pub_date"].year == 2015


def test_parse_s2_paper_preprint_and_tldr():
    paper = {
        "title": "GPT", "citationCount": 900, "influentialCitationCount": 100,
        "tldr": {"text": "a language model"}, "venue": "",
        "externalIds": {"ArXiv": "2005.14165"}, "year": 2020,
    }
    p = ac._parse_s2_paper(paper, 0, 1)
    assert p["is_preprint"] is True                   # 无 venue + 有 arXiv
    assert p["tldr"] == "a language model"
    assert p["arxiv_id"] == "2005.14165"
    assert p["landing_url"] == "https://arxiv.org/abs/2005.14165"


@pytest.mark.parametrize(("query", "kind", "identifier", "identifier_type"), [
    ("10.1038/NATURE14539", "doi", "10.1038/nature14539", "doi"),
    ("doi:10.1038/nature14539", "doi", "10.1038/nature14539", "doi"),
    ("https://dx.doi.org/10.1038%2Fnature14539", "doi", "10.1038/nature14539", "doi"),
    ("References: doi:10.1038/nature14539", "references", "10.1038/nature14539", "doi"),
    ("citations:https://openalex.org/w2741809807", "citations", "W2741809807", "openalex"),
    ("references:W123", "references", "W123", "openalex"),
])
def test_academic_query_complete_identifiers(query, kind, identifier, identifier_type):
    parsed = academic_query(query)
    assert (parsed.kind, parsed.identifier, parsed.identifier_type) == (kind, identifier, identifier_type)


@pytest.mark.parametrize("query", [
    "Deep learning paper", "references:Deep Learning", "W123",
    "https://evil.example/10.1038/nature14539",
    "references:https://doi.org/W123", "references:https://openalex.org/10.1038/abc",
    "https://doi.org/10.1038/nature14539?redirect=1",
    "https://doi.org/10.1038/nature14539#section",
    "citations:https://openalex.org@evil.example/W123",
    "10.1038/nature14539 and related papers",
])
def test_academic_query_does_not_guess_titles_or_arbitrary_urls(query):
    assert academic_query(query) is None


def _mock_academic_http(monkeypatch, handler):
    original = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(ac.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=transport, **kwargs))
    monkeypatch.setattr(config, "ACADEMIC_INCLUDE_ARXIV", False)
    return requests


@pytest.mark.parametrize(("status", "message"), [
    (404, {}),
    (200, {"DOI": "10.1234/unrelated", "title": ["Topical but wrong"]}),
    (200, {"title": ["No identifier"]}),
])
def test_exact_doi_unknown_or_mismatch_never_becomes_topical_result(monkeypatch, status, message):
    requests = _mock_academic_http(
        monkeypatch, lambda request: httpx.Response(status, json={"message": message}),
    )
    out = run(ac.AcademicEngine().search(SearchOptions("https://doi.org/10.1234/requested")))
    assert out == []
    assert len(requests) == 1
    assert requests[0].url.host == "api.crossref.org"
    assert "query" not in requests[0].url.params
    assert "/works/10.1234/requested" == requests[0].url.path


def test_exact_doi_matched_metadata_is_not_claim_verification(monkeypatch):
    _mock_academic_http(monkeypatch, lambda request: httpx.Response(200, json={"message": {
        "DOI": "10.1234/Requested", "title": ["A registered work"],
        "published-online": {"date-parts": [[2024, 2, 3]]},
        "created": {"date-parts": [[2020, 1, 1]]}, "is-referenced-by-count": 700,
    }}))
    result, = run(ac.AcademicEngine().search(SearchOptions("doi:10.1234/requested")))
    assert result.url == "https://doi.org/10.1234/requested"
    assert result.source_tag == "academic:crossref"
    assert "DOI identifier matched: 10.1234/requested (Crossref)" in result.snippet
    assert "not claim verification" in result.snippet
    assert "published 2024-02-03" in result.snippet
    assert "2020" not in result.snippet


@pytest.mark.parametrize("status", [401, 429, 500])
def test_exact_doi_source_failures_do_not_return_other_sources(monkeypatch, status):
    requests = _mock_academic_http(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(EngineError, match="metadata lookup failed"):
        run(ac.AcademicEngine().search(SearchOptions("10.1234/requested")))
    assert len(requests) == 1


def _work(identifier, **fields):
    return {"id": f"https://openalex.org/{identifier}", "title": identifier,
            "referenced_works": [], **fields}


@pytest.mark.parametrize(("kind", "edge", "relationship"), [
    ("references", "cited_by", "referenced by"),
    ("citations", "cites", "cites"),
])
def test_relationship_direction_seed_and_edge_evidence_bounded(monkeypatch, kind, edge, relationship):
    seed = _work("W1", doi="https://doi.org/10.1234/seed",
                 referenced_works=["https://openalex.org/W2"])
    related = (_work("W2") if kind == "references"
               else _work("W2", referenced_works=["https://openalex.org/W1"]))
    unrelated = _work("W3")  # not a reference and does not cite W1

    def respond(request):
        if request.url.path != "/works":
            return httpx.Response(200, json=seed)
        assert request.url.params["filter"] == f"{edge}:W1"
        assert request.url.params["per_page"] == "25"
        return httpx.Response(200, json={"results": [related, related, unrelated, seed]})

    monkeypatch.setattr(config, "get_env", lambda key: "test-key" if key == "OPENALEX_API_KEY" else "")
    requests = _mock_academic_http(monkeypatch, respond)
    result, = run(ac.AcademicEngine().search(SearchOptions(f"{kind}:10.1234/seed", count=100)))
    assert result.title == "W2"
    assert f"{relationship} seed W1" in result.snippet
    assert "requested 10.1234/seed; one-hop OpenAlex" in result.snippet
    assert len(requests) == 2
    assert all(r.headers["Authorization"] == "Bearer test-key" for r in requests)
    assert all("test-key" not in str(r.url) for r in requests)


@pytest.mark.parametrize("query", ["references:W1", "citations:10.1234/seed"])
def test_relationship_seed_mismatch_never_queries_graph(monkeypatch, query):
    requests = _mock_academic_http(
        monkeypatch, lambda request: httpx.Response(200, json=_work("W999", doi="10.1234/wrong")),
    )
    assert run(ac.AcademicEngine().search(SearchOptions(query))) == []
    assert len(requests) == 1


@pytest.mark.parametrize("kind", ["references", "citations"])
def test_unknown_relationship_seed_returns_no_related_evidence(monkeypatch, kind):
    requests = _mock_academic_http(monkeypatch, lambda request: httpx.Response(404))
    assert run(ac.AcademicEngine().search(SearchOptions(f"{kind}:W1"))) == []
    assert len(requests) == 1


def test_relationship_failure_never_uses_topical_search(monkeypatch):
    def respond(request):
        if request.url.path == "/works":
            return httpx.Response(429)
        return httpx.Response(200, json=_work("W1"))
    requests = _mock_academic_http(monkeypatch, respond)
    with pytest.raises(EngineError, match="metadata lookup failed"):
        run(ac.AcademicEngine().search(SearchOptions("citations:W1")))
    assert len(requests) == 2


@pytest.mark.parametrize("query", ["references:a paper title", "citations:", "doi:random"])
def test_invalid_explicit_identifier_rejected_without_network(monkeypatch, query):
    requests = _mock_academic_http(monkeypatch, lambda request: httpx.Response(500))
    with pytest.raises(EngineError, match="complete DOI"):
        run(ac.AcademicEngine().search(SearchOptions(query)))
    assert requests == []


@pytest.mark.parametrize("query", ["clinical cancer therapy", "基因治疗 综述", "CRISPR RNA vaccines"])
def test_biomedical_search_adds_europe_pmc_and_merges_evidence(monkeypatch, query):
    monkeypatch.setattr(ac, "_fetch_openalex", _fake_async([
        _paper(title="Clinical study", doi="10.1234/study", cited_by_count=50),
    ]))
    monkeypatch.setattr(ac, "_fetch_s2", _fake_raise(RuntimeError("s2 unavailable")))

    def respond(request):
        assert request.url.host == "www.ebi.ac.uk"
        assert request.url.params["query"] == query
        assert request.url.params["resultType"] == "core"
        return httpx.Response(200, json={"resultList": {"result": [{
            "id": "12345", "source": "MED", "title": "Clinical study duplicate",
            "doi": "10.1234/STUDY", "citedByCount": 60,
            "firstPublicationDate": "2024-03-14",
            "abstractText": "<p>Deposited abstract.</p>",
            "pubTypeList": {"pubType": ["Retracted Publication"]},
            "journalInfo": {"journal": {"title": "Medical Journal"}},
        }]}})

    requests = _mock_academic_http(monkeypatch, respond)
    result, = run(ac.AcademicEngine().search(SearchOptions(query)))
    assert result.title == "Clinical study"
    assert "Deposited abstract." in result.snippet
    assert "60cit" in result.snippet
    assert "retracted (Europe PMC)" in result.snippet
    assert "published 2024-03-14" in result.snippet
    assert "europe_pmc, openalex" in result.snippet
    assert len(requests) == 1


def test_non_biomedical_search_does_not_add_europe_pmc_or_unneeded_fallback(monkeypatch):
    monkeypatch.setattr(ac, "_fetch_openalex", _fake_async([_paper(doi="10.1234/algo")]))
    monkeypatch.setattr(ac, "_fetch_s2", _fake_async([]))
    requests = _mock_academic_http(monkeypatch, lambda request: httpx.Response(500))
    assert len(run(ac.AcademicEngine().search(SearchOptions("graph algorithms paper")))) == 1
    assert requests == []


def test_biomedical_failure_does_not_erase_surviving_primary_source(monkeypatch):
    monkeypatch.setattr(ac, "_fetch_openalex", _fake_async([_paper(title="Survived")]))
    monkeypatch.setattr(ac, "_fetch_s2", _fake_async([]))
    requests = _mock_academic_http(monkeypatch, lambda request: httpx.Response(503))
    result, = run(ac.AcademicEngine().search(SearchOptions("clinical therapy")))
    assert result.title == "Survived"
    assert len(requests) == 1


def test_empty_primary_search_uses_bounded_crossref_metadata_fallback(monkeypatch):
    monkeypatch.setattr(ac, "_fetch_openalex", _fake_raise(RuntimeError("down")))
    monkeypatch.setattr(ac, "_fetch_s2", _fake_async([]))

    def respond(request):
        assert request.url.host == "api.crossref.org"
        assert request.url.params["query"] == "graph algorithms"
        assert request.url.params["rows"] == "25"
        return httpx.Response(200, json={"message": {"items": [
            {"title": ["Metadata only"], "DOI": "10.1234/meta"},
            {"title": ["Metadata duplicate"], "DOI": "10.1234/META"},
        ]}})

    requests = _mock_academic_http(monkeypatch, respond)
    result, = run(ac.AcademicEngine().search(SearchOptions("graph algorithms", count=100)))
    assert result.title == "Metadata only"
    assert "bibliographic metadata (Crossref)" in result.snippet
    assert "DOI identifier matched" not in result.snippet
    assert len(requests) == 1


def test_openalex_retraction_evidence_survives_dedup():
    p = ac._parse_openalex_work(_work("W1", doi="10.1234/a", is_retracted=True), 0, 1)
    other = _paper(title="Other source", doi="https://doi.org/10.1234/A", source="semantic_scholar")
    merged, = ac.dedup_by_doi([other, p])
    assert "retracted (OpenAlex)" in ac._to_result(merged).snippet


def test_crossref_dates_and_retraction_notice_direction():
    notice = ac._parse_crossref_work({
        "title": ["Retraction notice"], "DOI": "10.1234/notice",
        "published-online": {"date-parts": [[2024, 2, 31]]},
        "published-print": {"date-parts": [[2024, 3]]},
        "update-to": [{"DOI": "10.1234/original", "type": "retraction"}],
    }, 0, 1)
    assert notice["pub_date"] == datetime(2024, 3, 1, tzinfo=timezone.utc)
    assert notice["retraction_evidence"] == ["retraction notice for 10.1234/original (Crossref)"]
    original = ac._parse_crossref_work({
        "title": ["Original"], "DOI": "10.1234/original",
        "updated-by": [{"DOI": "10.1234/notice", "type": "retraction"}],
    }, 0, 1)
    assert original["retraction_evidence"] == ["retracted; notice 10.1234/notice (Crossref)"]
    assert ac._year_date(99999) is None


def test_health_does_not_label_401_or_429_usable(monkeypatch):
    def respond(request):
        status = {"api.openalex.org": 401, "api.semanticscholar.org": 429}.get(request.url.host, 200)
        return httpx.Response(status, json={})
    requests = _mock_academic_http(monkeypatch, respond)
    result = run(ac.AcademicEngine().health_check())
    assert result.status == "warn"
    assert result.evidence["sources_ok"] == ["crossref", "europe_pmc"]
    assert result.evidence["sources_fail"] == ["openalex(401)", "semantic_scholar(429)"]
    assert "openalex" not in result.active_backend
    assert len(requests) == 4


@pytest.mark.parametrize("query", [
    "clinical randomized trial", "PubMed cancer", "Europe PMC immunotherapy",
    "生医论文", "临床试验综述", "CRISPR RNA delivery", "cancer treatment evidence",
])
def test_biomedical_academic_routing_has_research_intent(query):
    assert biomedical_query(query)
    assert biomedical_scope(query)


@pytest.mark.parametrize("query", [
    "how should I treat a cold?", "癌症怎么治", "vaccine side effects",
    "what are clinical symptoms of flu?", "randomized graph algorithms",
    "systematic review of database indexes",
])
def test_biomedical_routing_does_not_capture_general_health_or_unrelated_research(query):
    assert not biomedical_query(query)


def test_broad_biomedical_topic_still_adds_scope_after_explicit_academic_route():
    assert biomedical_scope("cancer treatment")
    assert not biomedical_query("cancer treatment")


def test_publication_precision_does_not_invent_dates_or_exact_timestamps():
    paper = ac._parse_s2_paper({"title": "Year only", "year": 2022}, 0, 1)
    result = ac._to_result(paper)
    assert "published 2022" in result.snippet
    assert "2022-01-01" not in result.snippet
    assert result.source_ts == 0
    paper = ac._parse_crossref_work({
        "title": ["Month only"], "DOI": "10.1234/month",
        "published-online": {"date-parts": [[2023, 7]]},
    }, 0, 1)
    result = ac._to_result(paper)
    assert "published 2023-07" in result.snippet
    assert "2023-07-01" not in result.snippet
    assert result.source_ts == 0
    paper = ac._parse_crossref_work({
        "title": ["Day supplied"], "DOI": "10.1234/day",
        "published-online": {"date-parts": [[2023, 7, 9]]},
    }, 0, 1)
    assert ac._to_result(paper).source_ts == datetime(2023, 7, 9, tzinfo=timezone.utc).timestamp()


def test_dedup_does_not_mutate_source_evidence_lists():
    original = _paper(doi="10.1234/a", evidence=["first"])
    other = _paper(doi="10.1234/a", source="europe_pmc", evidence=["second"])
    merged, = ac.dedup_by_doi([original, other])
    assert merged["evidence"] == ["first", "second"]
    assert original["evidence"] == ["first"]


@pytest.mark.parametrize("payload", [[], {"message": []}, {"message": None}])
def test_exact_doi_malformed_metadata_never_becomes_evidence(monkeypatch, payload):
    requests = _mock_academic_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(EngineError, match="malformed bibliographic metadata"):
        run(ac.AcademicEngine().search(SearchOptions("10.1234/a")))
    assert len(requests) == 1
