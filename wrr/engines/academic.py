"""Native scholarly retrieval with bounded, isolated public API requests.

OpenAlex and Semantic Scholar remain the general sources (arXiv is opt-in).
Biomedical queries add Europe PMC; an empty general search may fall back to
Crossref bibliographic metadata. Exact DOIs use identifier-matched Crossref
records, never title search. Explicit references:/citations: queries use one
verified OpenAlex seed and one relationship page, without graph expansion.

Citation counts and ranking signals are discovery aids, not truth assessments.
Metadata verification confirms a bibliographic identifier, not a paper's claims.
"""
import asyncio
import math
import re
from html import unescape
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from .base import SearchEngine
from ._fusion import recency_decay
from ._academic_query import AcademicQuery, academic_query, biomedical_scope
from .. import config
from ..errors import EngineError
from ..schemas import SearchOptions, SearchResult, EngineCheckResult

# 内部统一论文表示（paper_dict）字段：
#   title cited_by_count influential_citations pub_date(datetime|None) is_preprint
#   venue venue_rank doi arxiv_id openalex_id source source_relevance
#   tldr abstract doi_url landing_url [cite_sources]


# ── 小工具 ───────────────────────────────────────────────────────────
def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _parse_date(val) -> Optional[datetime]:
    """解析 'YYYY-MM-DD' / ISO 8601 → aware UTC datetime。"""
    if not val or not isinstance(val, str):
        return None
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(val[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _year_date(year) -> Optional[datetime]:
    y = _int(year)
    if not 1 <= y <= 9999:
        return None
    return datetime(y, 1, 1, tzinfo=timezone.utc)


def _clean_doi(doi: str) -> str:
    """归一 DOI：剥 https://doi.org/ 前缀、小写。"""
    if not doi:
        return ""
    d = doi.strip().lower()
    return re.sub(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", "", d)


def _rank_relevance(idx: int, total: int) -> float:
    """源返回顺序 → 相关性分位（首位最高，末位最低，恒 >0）。"""
    if total <= 0:
        return 0.0
    return (total - idx) / total


def _reconstruct_abstract(inv) -> str:
    """OpenAlex abstract_inverted_index → 还原正文。"""
    if not isinstance(inv, dict) or not inv:
        return ""
    positions: List = []
    for word, idxs in inv.items():
        for i in idxs or []:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


# ── 纯评分函数（单测可直接调用）──────────────────────────────────────
def citation_velocity(cited_by_count: int, age_years: float) -> float:
    """年均引用对数压缩到 [0,1]。~300/yr → 1.0。

    用年均引用而非绝对引用数：老论文引用高但 age 大，velocity 不会碾压新论文。
    """
    age = max(age_years, 0.25)                    # 下界保护，避免极新论文 velocity 爆炸
    velocity = max(cited_by_count, 0) / age
    return min(math.log10(velocity + 1) / 2.5, 1.0)


def academic_score(paper: Dict[str, Any], now: Optional[datetime] = None) -> float:
    """4 维加权综合分。权重取自 config.ACADEMIC_SCORE_WEIGHTS。"""
    now = now or datetime.now(timezone.utc)
    w_v, w_a, w_r, w_rel = config.ACADEMIC_SCORE_WEIGHTS
    pub = paper.get("pub_date")

    # 1. velocity：年均引用对数压缩
    if pub is not None:
        age_years = max((now - pub).days / 365.0, 0.25)
    else:
        age_years = 5.0                           # 未知日期 → 中性偏老
    velocity = citation_velocity(_int(paper.get("cited_by_count")), age_years)

    # 2. authority：venue 权威 + S2 influential 引用占比
    cited = _int(paper.get("cited_by_count"))
    infl = _int(paper.get("influential_citations"))
    infl_ratio = (infl / cited) if cited > 0 else 0.0
    venue_rank = float(paper.get("venue_rank") or 0.0)
    authority = min(0.6 * venue_rank + 0.4 * min(infl_ratio * 3, 1.0), 1.0)

    # 3. recency：连续衰减（半衰期 365d）+ 预印本 boost
    if pub is not None:
        age_hours = max((now - pub).total_seconds() / 3600.0, 0.0)
        recency = recency_decay(age_hours, config.ACADEMIC_RECENCY_HALFLIFE_DAYS * 24.0)
    else:
        recency = 0.5
    if paper.get("is_preprint"):
        recency = min(recency * 1.2, 1.0)

    # 4. relevance：源相关性分位
    relevance = float(paper.get("source_relevance") or 0.0)

    return w_v * velocity + w_a * authority + w_r * recency + w_rel * relevance


# ── 去重（DOI 优先 → arXiv → openalex → 标题）─────────────────────────
def _dedup_key(p: Dict[str, Any]) -> str:
    doi = _clean_doi(p.get("doi") or "")
    if doi:
        return "doi:" + doi
    ax = (p.get("arxiv_id") or "").strip().lower()
    if ax:
        return "arxiv:" + ax
    oa = (p.get("openalex_id") or "").strip().lower()
    if oa:
        return "openalex:" + oa
    return "title:" + re.sub(r"\W+", "", (p.get("title") or "").lower())


def _merge_into(base: Dict[str, Any], other: Dict[str, Any]) -> None:
    """引用取 max + 标注来源；优先 OpenAlex 字段；保留正式版（有 venue）为代表。"""
    base["cited_by_count"] = max(_int(base.get("cited_by_count")),
                                 _int(other.get("cited_by_count")))
    base["influential_citations"] = max(_int(base.get("influential_citations")),
                                        _int(other.get("influential_citations")))
    cs = base.setdefault("cite_sources", {})
    if other.get("source"):
        cs[other["source"]] = _int(other.get("cited_by_count"))

    for field in ("retraction_evidence", "evidence"):
        values = base.setdefault(field, [])
        for value in other.get(field) or []:
            if value not in values:
                values.append(value)
    if not base.get("pub_date") and other.get("pub_date"):
        base["pub_date"] = other["pub_date"]
        base["pub_date_precision"] = other.get("pub_date_precision", 10)
    # 缺字段补全（任一源有就补）
    for f in ("tldr", "abstract", "venue", "doi", "doi_url", "landing_url",
              "arxiv_id", "openalex_id"):
        if not base.get(f) and other.get(f):
            base[f] = other[f]

    # 正式版优先：有 venue → 提升权威、视为已发表代表
    if other.get("venue") and not base.get("venue"):
        base["venue"] = other["venue"]
    if other.get("venue"):
        base["venue_rank"] = max(float(base.get("venue_rank") or 0.0),
                                 float(other.get("venue_rank") or 0.0))
    if not other.get("is_preprint"):
        base["is_preprint"] = False

    # OpenAlex 字段优先（日更最快、口径最一致）
    if other.get("source") == "openalex":
        for f in ("doi", "doi_url", "venue", "venue_rank",
                  "openalex_id", "landing_url"):
            if other.get(f):
                base[f] = other[f]
        if other.get("pub_date"):
            base["pub_date"] = other["pub_date"]
            base["pub_date_precision"] = other.get("pub_date_precision", 10)
        base["source"] = "openalex"


def dedup_by_doi(papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """DOI 优先合并同一篇（arXiv ID/openalex id/标题兜底），保留首见为代表。"""
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for p in papers:
        key = _dedup_key(p)
        if key in merged:
            _merge_into(merged[key], p)
        else:
            rep = dict(p)
            for field in ("evidence", "retraction_evidence"):
                rep[field] = list(p.get(field) or [])
            rep["cite_sources"] = {p["source"]: _int(p.get("cited_by_count"))} \
                if p.get("source") else {}
            merged[key] = rep
            order.append(key)
    return [merged[k] for k in order]


# ── 各源解析（纯函数）────────────────────────────────────────────────
def _parse_openalex_work(work: Dict[str, Any], idx: int, total: int) -> Optional[Dict[str, Any]]:
    title = (work.get("title") or work.get("display_name") or "").strip()
    if not title:
        return None
    ids = work.get("ids") or {}
    doi_url = ids.get("doi") or work.get("doi") or ""
    openalex_id = ids.get("openalex") or work.get("id") or ""
    ploc = work.get("primary_location") or {}
    venue = ((ploc.get("source") or {}).get("display_name") or "").strip()
    landing = ploc.get("landing_page_url") or openalex_id or ""
    is_preprint = (work.get("type") or "").lower() == "preprint"
    return {
        "title": title,
        "cited_by_count": _int(work.get("cited_by_count")),
        "influential_citations": 0,
        "pub_date": _parse_date(work.get("publication_date")),
        "is_preprint": is_preprint,
        "venue": venue,
        "venue_rank": 0.0 if (is_preprint or not venue) else 0.5,
        "doi": _clean_doi(doi_url),
        "arxiv_id": "",
        "openalex_id": openalex_id,
        "source": "openalex",
        "source_relevance": _rank_relevance(idx, total),
        "tldr": "",
        "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")),
        "doi_url": doi_url,
        "landing_url": landing,
        "retraction_evidence": ["retracted (OpenAlex)"] if work.get("is_retracted") is True else [],
    }


def _parse_s2_paper(p: Dict[str, Any], idx: int, total: int) -> Optional[Dict[str, Any]]:
    title = (p.get("title") or "").strip()
    if not title:
        return None
    ext = p.get("externalIds") or {}
    doi = _clean_doi(ext.get("DOI") or "")
    arxiv_id = (ext.get("ArXiv") or "").strip()
    venue = (p.get("venue") or "").strip()
    tldr_obj = p.get("tldr") or {}
    tldr = (tldr_obj.get("text") if isinstance(tldr_obj, dict) else "") or ""
    supplied_date = _parse_date(p.get("publicationDate"))
    pub_date = supplied_date or _year_date(p.get("year"))
    is_preprint = (not venue) and bool(arxiv_id)
    return {
        "title": title,
        "cited_by_count": _int(p.get("citationCount")),
        "influential_citations": _int(p.get("influentialCitationCount")),
        "pub_date": pub_date,
        "pub_date_precision": 10 if supplied_date else 4,
        "is_preprint": is_preprint,
        "venue": venue,
        "venue_rank": 0.0 if (is_preprint or not venue) else 0.5,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "openalex_id": "",
        "source": "semantic_scholar",
        "source_relevance": _rank_relevance(idx, total),
        "tldr": tldr,
        "abstract": (p.get("abstract") or ""),
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "landing_url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
    }


_ATOM = {"atom": "http://www.w3.org/2005/Atom"}


def _parse_arxiv_atom(text: str, count: int) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    entries = root.findall("atom:entry", _ATOM)
    out: List[Dict[str, Any]] = []
    total = len(entries)
    for idx, e in enumerate(entries):
        title = (e.findtext("atom:title", default="", namespaces=_ATOM) or "").strip()
        if not title:
            continue
        url = (e.findtext("atom:id", default="", namespaces=_ATOM) or "").strip()
        arxiv_id = url.rsplit("/abs/", 1)[-1] if "/abs/" in url else ""
        summary = (e.findtext("atom:summary", default="", namespaces=_ATOM) or "").strip()
        pub_date = _parse_date(e.findtext("atom:published", default="", namespaces=_ATOM))
        out.append({
            "title": title,
            "cited_by_count": 0,
            "influential_citations": 0,
            "pub_date": pub_date,
            "is_preprint": True,
            "venue": "",
            "venue_rank": 0.0,
            "doi": "",
            "arxiv_id": arxiv_id,
            "openalex_id": "",
            "source": "arxiv",
            "source_relevance": _rank_relevance(idx, total),
            "tldr": "",
            "abstract": summary,
            "doi_url": "",
            "landing_url": url,
        })
        if len(out) >= count:
            break
    return out


def _metadata_text(value: Any) -> str:
    """Strip markup from deposited abstract metadata, not fetch paper text."""
    return unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).strip()


def _crossref_publication(work: Dict[str, Any]) -> tuple[Optional[datetime], int]:
    for field in ("published-online", "published-print", "published", "issued"):
        obj = work.get(field) or {}
        parts = obj.get("date-parts") if isinstance(obj, dict) else None
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
            continue
        values = parts[0]
        try:
            date = datetime(int(values[0]), int(values[1]) if len(values) > 1 else 1,
                            int(values[2]) if len(values) > 2 else 1, tzinfo=timezone.utc)
            return date, (10 if len(values) >= 3 else (7 if len(values) == 2 else 4))
        except (ValueError, TypeError, IndexError, OverflowError):
            continue
    return None, 0


def _parse_crossref_work(work: Dict[str, Any], idx: int, total: int) -> Optional[Dict[str, Any]]:
    titles = work.get("title") or []
    title = (titles[0] if isinstance(titles, list) and titles else "").strip()
    doi = _clean_doi(work.get("DOI") or "")
    if not title or not doi:
        return None
    venues = work.get("container-title") or []
    venue = venues[0] if isinstance(venues, list) and venues else ""
    retractions = []
    # update-to describes this notice's target, NOT retraction of this work.
    for update in work.get("update-to") or []:
        if isinstance(update, dict) and update.get("type") == "retraction":
            retractions.append(f"retraction notice for {update.get('DOI', '')} (Crossref)")
    for update in work.get("updated-by") or []:
        if isinstance(update, dict) and update.get("type") == "retraction":
            retractions.append(f"retracted; notice {update.get('DOI', '')} (Crossref)")
    for notice in (work.get("relation") or {}).get("is-retracted-by") or []:
        if isinstance(notice, dict):
            retractions.append(f"retracted; notice {notice.get('id', '')} (Crossref)")
    pub_date, precision = _crossref_publication(work)
    return {
        "title": title, "doi": doi, "doi_url": f"https://doi.org/{doi}",
        "landing_url": work.get("URL") or "", "source": "crossref",
        "source_relevance": _rank_relevance(idx, total),
        "cited_by_count": _int(work.get("is-referenced-by-count")),
        "influential_citations": 0, "pub_date": pub_date, "pub_date_precision": precision,
        "is_preprint": work.get("type") == "posted-content",
        "venue": venue, "venue_rank": 0.0, "arxiv_id": "", "openalex_id": "",
        "abstract": _metadata_text(work.get("abstract")), "tldr": "",
        "evidence": ["bibliographic metadata (Crossref); not claim verification"],
        "retraction_evidence": retractions,
    }


def _parse_europe_pmc_work(work: Dict[str, Any], idx: int, total: int) -> Optional[Dict[str, Any]]:
    title = (work.get("title") or "").strip()
    native_id = str(work.get("id") or work.get("pmid") or work.get("pmcid") or "")
    native_source = str(work.get("source") or "MED")
    if not title or not native_id:
        return None
    journal_info = work.get("journalInfo") or {}
    venue = (journal_info.get("journal") or {}).get("title") or work.get("journalTitle") or ""
    pub_types = (work.get("pubTypeList") or {}).get("pubType") or []
    if isinstance(pub_types, str):
        pub_types = [pub_types]
    retractions = []
    if "Retracted Publication" in pub_types or str(work.get("isRetracted") or "").upper() == "Y":
        retractions.append("retracted (Europe PMC)")
    if "Retraction of Publication" in pub_types:
        retractions.append("retraction notice (Europe PMC)")
    for correction in (work.get("commentCorrectionList") or {}).get("commentCorrection") or []:
        if isinstance(correction, dict) and correction.get("type") == "Retraction in":
            retractions.append(f"retracted; notice {correction.get('id', '')} (Europe PMC)")
    doi = _clean_doi(work.get("doi") or "")
    supplied_date = _parse_date(work.get("firstPublicationDate"))
    return {
        "title": title, "doi": doi, "doi_url": f"https://doi.org/{doi}" if doi else "",
        "landing_url": f"https://europepmc.org/article/{quote(native_source, safe='')}/{quote(native_id, safe='')}",
        "source": "europe_pmc", "source_relevance": _rank_relevance(idx, total),
        "cited_by_count": _int(work.get("citedByCount")), "influential_citations": 0,
        "pub_date": supplied_date or _year_date(work.get("pubYear")),
        "pub_date_precision": 10 if supplied_date else 4,
        "is_preprint": native_source == "PPR" or "Preprint" in pub_types,
        "venue": venue, "venue_rank": 0.5 if venue else 0.0,
        "abstract": _metadata_text(work.get("abstractText")), "tldr": "",
        "arxiv_id": "", "openalex_id": "", "retraction_evidence": retractions,
    }




def _openalex_headers() -> Dict[str, str]:
    key = config.get_env("OPENALEX_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}




# ── HTTP（模块级，便于单测 monkeypatch）──────────────────────────────
def _per_page(count: int, cap: int) -> int:
    return min(max(count, 1), cap)


async def _fetch_openalex(options: SearchOptions, client: httpx.AsyncClient | None = None) -> List[Dict[str, Any]]:
    """OpenAlex works 检索 → paper_dict 列表。主力源。"""
    params = {
        "search": options.query,
        "mailto": config.OPENALEX_MAILTO,
        "per_page": _per_page(options.count, 25),
    }
    if client is None:
        async with httpx.AsyncClient(timeout=config.ACADEMIC_SOURCE_TIMEOUT) as client:
            r = await client.get(config.OPENALEX_API, params=params, headers=_openalex_headers())
    else:
        r = await client.get(config.OPENALEX_API, params=params, headers=_openalex_headers())
    r.raise_for_status()
    data = r.json() or {}
    works = data.get("results") or []
    parsed = [_parse_openalex_work(w, i, len(works)) for i, w in enumerate(works)]
    return [p for p in parsed if p]


async def _fetch_s2(options: SearchOptions, client: httpx.AsyncClient | None = None) -> List[Dict[str, Any]]:
    """Semantic Scholar paper search → paper_dict 列表。引用质量 + tldr 补充。"""
    params = {
        "query": options.query,
        "fields": ("title,abstract,citationCount,influentialCitationCount,"
                   "tldr,externalIds,year,venue,publicationDate"),
        "limit": _per_page(options.count, 25),
    }
    if client is None:
        async with httpx.AsyncClient(timeout=config.ACADEMIC_SOURCE_TIMEOUT) as client:
            r = await client.get(config.SEMANTIC_SCHOLAR_API, params=params)
    else:
        r = await client.get(config.SEMANTIC_SCHOLAR_API, params=params)
    r.raise_for_status()
    data = r.json() or {}
    papers = data.get("data") or []
    parsed = [_parse_s2_paper(p, i, len(papers)) for i, p in enumerate(papers)]
    return [p for p in parsed if p]


async def _fetch_arxiv(options: SearchOptions, client: httpx.AsyncClient | None = None) -> List[Dict[str, Any]]:
    """arXiv 预印本（Atom）→ paper_dict 列表。默认 lazy（P2，3s/req 慢）。"""
    params = {
        "search_query": f"all:{options.query}",
        "start": 0,
        "max_results": _per_page(options.count, 15),
    }
    if client is None:
        async with httpx.AsyncClient(timeout=config.ACADEMIC_SOURCE_TIMEOUT) as client:
            r = await client.get(config.ARXIV_API, params=params)
    else:
        r = await client.get(config.ARXIV_API, params=params)
    r.raise_for_status()
    text = r.text
    return _parse_arxiv_atom(text, options.count)


async def _fetch_europe_pmc(options: SearchOptions, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    response = await client.get(config.EUROPE_PMC_API, params={
        "query": options.query, "format": "json", "resultType": "core",
        "pageSize": _per_page(options.count, 25),
    })
    response.raise_for_status()
    works = response.json().get("resultList", {}).get("result") or []
    return [p for i, work in enumerate(works[:25])
            if isinstance(work, dict) and (p := _parse_europe_pmc_work(work, i, len(works)))]


async def _fetch_crossref(options: SearchOptions, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    parsed = academic_query(options.query)
    exact = parsed is not None and parsed.kind == "doi"
    url = config.CROSSREF_API.rstrip("/")
    params = {"mailto": config.OPENALEX_MAILTO}
    if exact:
        url += "/" + quote(parsed.identifier, safe="")
    else:
        params.update({"query": options.query, "rows": _per_page(options.count, 25)})
    response = await client.get(url, params=params)
    if exact and response.status_code == 404:
        return []
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), dict):
        raise EngineError("academic: Crossref returned malformed bibliographic metadata")
    message = payload["message"]
    if exact:
        if _clean_doi(message.get("DOI") or "") != parsed.identifier:
            return []
        works = [message]
    else:
        works = message.get("items") or []
    out = []
    for i, work in enumerate(works[:25]):
        if not isinstance(work, dict):
            continue
        paper = _parse_crossref_work(work, i, len(works))
        if paper:
            if exact:
                paper["evidence"].append(f"DOI identifier matched: {parsed.identifier} (Crossref)")
            out.append(paper)
    return out


def _openalex_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parsed = academic_query("references:" + value)
    return parsed.identifier if parsed and parsed.identifier_type == "openalex" else ""


async def _fetch_relationship(query: AcademicQuery, options: SearchOptions,
                              client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """One seed lookup + one page (25 maximum); never follows graph edges."""
    base = config.OPENALEX_API.rstrip("/")
    seed_identifier = (f"https://doi.org/{query.identifier}"
                       if query.identifier_type == "doi" else query.identifier)
    headers = _openalex_headers()
    response = await client.get(base + "/" + quote(seed_identifier, safe=""),
                                headers=headers, params={"mailto": config.OPENALEX_MAILTO})
    if response.status_code == 404:
        return []
    response.raise_for_status()
    seed = response.json()
    if not isinstance(seed, dict):
        raise EngineError("academic: OpenAlex returned malformed seed metadata")
    seed_id = _openalex_id(seed.get("id"))
    if not seed_id:
        raise EngineError("academic: OpenAlex seed response has no valid work ID")
    if query.identifier_type == "doi":
        if _clean_doi(seed.get("doi") or (seed.get("ids") or {}).get("doi") or "") != query.identifier:
            return []
    elif seed_id != query.identifier:
        return []
    # cites finds works citing the seed; cited_by finds works cited by it.
    edge = "cites" if query.kind == "citations" else "cited_by"
    response = await client.get(base, headers=headers, params={
        "filter": f"{edge}:{seed_id}", "per_page": _per_page(options.count, 25),
        "mailto": config.OPENALEX_MAILTO,
    })
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise EngineError("academic: OpenAlex returned a malformed relationship page")
    works = payload["results"]
    seed_references = {_openalex_id(v) for v in seed.get("referenced_works") or []}
    out = []
    for i, work in enumerate(works[:25]):
        if not isinstance(work, dict):
            continue
        work_id = _openalex_id(work.get("id"))
        if not work_id or work_id == seed_id:
            continue
        if query.kind == "references" and work_id not in seed_references:
            continue
        if query.kind == "citations" and seed_id not in {
                _openalex_id(v) for v in work.get("referenced_works") or []}:
            continue
        paper = _parse_openalex_work(work, i, len(works))
        if paper:
            paper["evidence"] = [
                f"{'referenced by' if query.kind == 'references' else 'cites'} seed {seed_id}; "
                f"requested {query.identifier}; one-hop OpenAlex relationship"
            ]
            out.append(paper)
    return out


# ── 输出映射 ─────────────────────────────────────────────────────────
def _to_result(p: Dict[str, Any]) -> SearchResult:
    cited = _int(p.get("cited_by_count"))
    sig = f"📚{cited}cit"
    infl = _int(p.get("influential_citations"))
    if infl:
        sig += f"（{infl} influential）"
    if p.get("is_preprint"):
        sig += " ·preprint"
    body = (p.get("tldr") or p.get("abstract") or "").strip()
    snippet = (body[:300] + " · " + sig) if body else sig
    venue = (p.get("venue") or "").strip()
    if venue:
        snippet += " · " + venue
    if p.get("pub_date"):
        snippet += " · published " + p["pub_date"].date().isoformat()[:p.get("pub_date_precision", 10)]
    for evidence in (p.get("evidence") or []) + (p.get("retraction_evidence") or []):
        snippet += " · " + evidence
    sources = sorted(set(p.get("cite_sources") or {}) | {p.get("source", "")})
    snippet += " · sources: " + ", ".join(s for s in sources if s)
    snippet += " · citation counts are not claim verification"
    doi = (p.get("doi") or "").strip()
    url = (p.get("doi_url") or (f"https://doi.org/{doi}" if doi else "")
           or p.get("landing_url") or p.get("openalex_id") or "")
    return SearchResult(
        title=(p.get("title") or "").strip() or "(untitled)",
        url=url,
        snippet=snippet,
        source_tag=f"academic:{p.get('source', '')}",
        source_ts=(p["pub_date"].timestamp()
                   if p.get("pub_date") and p.get("pub_date_precision", 10) == 10 else 0.0),
    )


class AcademicEngine(SearchEngine):
    name = "academic"
    tier = 0  # 无本地配置要求

    async def search(self, options: SearchOptions) -> List[SearchResult]:
        if options.count <= 0:
            return []
        now = datetime.now(timezone.utc)
        query = academic_query(options.query)
        if query is None and re.match(r"^\s*(references|citations|doi)\s*:", options.query, re.I):
            raise EngineError("academic: expected a complete DOI or OpenAlex relationship identifier")
        papers: List[Dict[str, Any]] = []
        async with asyncio.timeout(config.ACADEMIC_SOURCE_TIMEOUT * 2):
            async with httpx.AsyncClient(
                timeout=config.ACADEMIC_SOURCE_TIMEOUT,
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=5),
                follow_redirects=False,
            ) as client:
                if query is not None:
                    try:
                        papers = (await _fetch_crossref(options, client) if query.kind == "doi"
                                  else await _fetch_relationship(query, options, client))
                    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                        raise EngineError(
                            f"academic: {query.kind} metadata lookup failed ({type(exc).__name__})"
                        ) from None
                    if not papers:
                        return []
                else:
                    fetchers = [_fetch_openalex(options, client), _fetch_s2(options, client)]
                    if config.ACADEMIC_INCLUDE_ARXIV:
                        fetchers.append(_fetch_arxiv(options, client))
                    if biomedical_scope(options.query):
                        fetchers.append(_fetch_europe_pmc(options, client))
                    gathered = await asyncio.gather(*fetchers, return_exceptions=True)
                    for res in gathered:
                        if not isinstance(res, Exception) and res:
                            papers.extend(res)
                    if not papers:
                        try:
                            papers = await _fetch_crossref(options, client)
                        except Exception:
                            pass
        if not papers:
            raise EngineError("academic: all sources failed or returned no results")

        merged = dedup_by_doi(papers)                     # DOI 优先合并
        scored = sorted(merged, key=lambda p: academic_score(p, now), reverse=True)
        return [_to_result(p) for p in scored[:options.count]]

    async def health_check(self, *, deep: bool = False) -> EngineCheckResult:
        """Probe native APIs; auth/rate-limit responses are not usable sources."""
        probes = {
            "openalex": (config.OPENALEX_API, {"per_page": 1, "mailto": config.OPENALEX_MAILTO}),
            "semantic_scholar": (config.SEMANTIC_SCHOLAR_API, {"query": "test", "limit": 1}),
            "crossref": (config.CROSSREF_API, {"rows": 1}),
            "europe_pmc": (config.EUROPE_PMC_API, {"query": "cancer", "format": "json", "pageSize": 1}),
        }
        if config.ACADEMIC_INCLUDE_ARXIV:
            probes["arxiv"] = (config.ARXIV_API, {"search_query": "all:test", "max_results": 1})

        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            async def probe(name, url, params):
                try:
                    response = await client.get(
                        url, params=params, headers=_openalex_headers() if name == "openalex" else {},
                    )
                    return name, response.status_code, ""
                except httpx.HTTPError as exc:
                    return name, 0, type(exc).__name__

            checked = await asyncio.gather(*(probe(name, url, params)
                                             for name, (url, params) in probes.items()))
        sources_ok = [name for name, status, _ in checked if 200 <= status < 300]
        sources_fail = [f"{name}({error or status})" for name, status, error in checked
                        if not 200 <= status < 300]
        status = "ok" if not sources_fail else ("warn" if sources_ok else "fail")
        return EngineCheckResult(
            engine=self.name, status=status, tier=self.tier,
            summary=f"Academic native APIs usable ({len(sources_ok)}/{len(probes)})",
            details="; ".join(sources_fail),
            active_backend="+".join(sources_ok) if sources_ok else "none",
            repair=["Check academic API connectivity, API keys and rate limits"] if sources_fail else [],
            evidence={"sources_ok": sources_ok, "sources_fail": sources_fail},
        )
