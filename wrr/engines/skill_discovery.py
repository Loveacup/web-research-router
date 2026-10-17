"""Skill 发现引擎（v3，P2c）。复用共享 GitHubClient，零自造认证/httpx。

检索（每查询至多 3 次 code search + 1 次 repository search）：
  1. 查询整形：去掉中文填充词与英文意图/停用词，保留内容词（CJK 连续段整体保留）；
  2. 第一阶段并发：通用 ``<terms> filename:SKILL.md`` 与可信来源 ``... repo:<s1> repo:<s2>``（OR）；
  3. 伪相关反馈：从第一阶段候选目录名中选出反馈词 t*（多仓出现且/或与查询词同词干）；
  4. 第二阶段并发：``t* in:path filename:SKILL.md``（按目录名召回）与仓库搜索 ``t* skill``（按 star
     排序，召回根目录 SKILL.md 的独立 skill 仓库）。第一阶段为空且词数 ≥2 时改为去掉最长词的通用检索，
     再按其结果做反馈与仓库搜索。
评分：通道分按名次 1-(rank-1)/per_page（GitHub code search 的 score 恒为 1.0，不含信息）；
  overlap 以词干子串与首字母缩写匹配目录名；relevance = 0.5*通道分 + 0.5*overlap；
  目录名无词面证据（overlap=0）时出处先验只计 0.3 倍，避免可信仓库中无关 skill 占位；
  可信检索只命中 ≤3 条时视为主题证据，豁免该门；prerank = 0.70*relevance + 0.30*门*出处先验；
  同仓同名目录留最浅路径，相同 blob sha 合组。反馈词取最具体（出现仓库数最少、≥2）的查询同词干 token。
补全（有界）：前 K=6 组 + 至多 2 个预留组（先选择性可信候选，再仓库通道 overlap>0 候选）各取 1 次
  SKILL.md（无有效 frontmatter 即丢弃；contents 返回的 sha 回填后再按 sha 合组），并为缺 star 的
  非可信仓库取至多 10 次元数据；不列目录、不数提交。
终排：final = 0.60*组内最高 relevance + 0.25*组头证据门*出处 + 0.15*frontmatter 完整度。
任一通道或补全调用的 RateLimitError 立即向上抛出；其他单通道错误按空结果处理。

输出 SearchResult：title=name；url 指向代表副本目录 ``tree/HEAD/<dir>``；snippet 含
description 截断、score、copies、source、stars 与 Hermes 兼容标记。
"""
from __future__ import annotations

import asyncio
import base64
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .base import SearchEngine
from .. import config
from ..errors import EngineError, RateLimitError
from ..schemas import SearchOptions, SearchResult, EngineCheckResult
from ._github_client import GitHubClient

_Scored = Tuple[Tuple[float, str, str], SearchResult]
_EN_STOPWORDS = frozenset((
    "skill", "skills", "agent", "find", "recommend", "a", "an", "the", "for", "to", "that",
    "with", "and", "of", "in", "on", "helps", "help", "using", "use",
))
# 中文填充词按长度降序替换，避免短词先切断长词。
_ZH_FILLERS = tuple(sorted(("找一个", "推荐一个", "有没有", "一个", "的", "找", "推荐", "技能"),
                           key=len, reverse=True))
_TOKEN_SPLIT_RE = re.compile(r"[\s!-/:-@\[-`{-~]+")   # 空白与 ASCII 标点；CJK 连续段保留
_NAME_SPLIT_RE = re.compile(r"[-_. /]+")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
# 最长后缀优先；只在能留下 ≥4 个字符时剥离。
_STEM_SUFFIXES = tuple(sorted(("ations", "ation", "ings", "ing", "ions", "ion", "ers", "er", "ors", "or",
                               "als", "al", "ies", "es", "ed", "s"), key=len, reverse=True))
# 反馈词排除：目录名里常见但不表达主题的 token。
_FEEDBACK_STOP = frozenset(("skill", "skills", "agent", "agents", "claude", "codex", "plugin", "plugins",
                            "core", "main", "cli", "tool", "tools", "the", "and", "for"))


# ── 纯函数：frontmatter 解析 / 真伪判定（单测可直接调用）────────────────
def parse_frontmatter(md_text: str) -> Optional[Dict[str, Any]]:
    """解析 Markdown 顶部 ``---...---`` YAML frontmatter。

    首行须为 ``---``，需有闭合 ``---``/``...``；yaml 解析失败或结果非 dict → None。
    """
    if not md_text:
        return None
    text = md_text.lstrip("﻿")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return None
    block = "\n".join(lines[1:end])
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def is_true_skill(fm: Optional[Dict[str, Any]]) -> bool:
    """真 skill = frontmatter 含非空 name + description（否则只是恰好有 SKILL.md）。"""
    if not isinstance(fm, dict):
        return False
    name = str(fm.get("name") or "").strip()
    desc = str(fm.get("description") or "").strip()
    return bool(name and desc)


# ── 纯函数：评分组件 ─────────────────────────────────────────────────
def _frontmatter_completeness(fm: Optional[Dict[str, Any]]) -> float:
    """完整度：name+description 必备(0.6) + version/license 各 0.15 + 触发词 0.10。"""
    if not isinstance(fm, dict):
        return 0.0
    s = 0.0
    if str(fm.get("name") or "").strip() and str(fm.get("description") or "").strip():
        s += 0.6
    if fm.get("version"):
        s += 0.15
    if fm.get("license"):
        s += 0.15
    if fm.get("triggers") or fm.get("trigger") or fm.get("keywords"):
        s += 0.10
    return min(1.0, s)


def stars_norm(stars: Optional[int]) -> float:
    """star 对数压缩（~100k stars → 1.0）；缺失/非正 → 0。"""
    s = stars or 0
    if s <= 0:
        return 0.0
    return min(1.0, math.log10(s + 1) / 5.0)


def shape_query(query: str) -> List[str]:
    """查询整形：小写 → 替换中文填充词 → 按空白/ASCII 标点切分 → 去英文停用词 → 保序去重。"""
    text = (query or "").lower()
    for filler in _ZH_FILLERS:
        text = text.replace(filler, " ")
    terms: List[str] = []
    for token in _TOKEN_SPLIT_RE.split(text):
        if token and token not in _EN_STOPWORDS and token not in terms:
            terms.append(token)
    return terms or [(query or "").strip()]


def relaxed_terms(terms: List[str]) -> List[str]:
    """去掉最长词（同长取最早）。"""
    drop = max(range(len(terms)), key=lambda i: (len(terms[i]), -i))
    return terms[:drop] + terms[drop + 1:]


def _basename(repo: str, subdir: str) -> str:
    return (subdir.rsplit("/", 1)[-1] if subdir else repo.rsplit("/", 1)[-1]).lower()


def _is_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _long_enough(text: str) -> bool:
    return len(text) >= (2 if _is_cjk(text) else 3)


def stem(term: str) -> str:
    """轻量词干：去掉能留下 ≥4 个字符的最长后缀；CJK 原样返回。"""
    t = (term or "").lower()
    if _is_cjk(t):
        return t
    for suffix in _STEM_SUFFIXES:
        if t.endswith(suffix) and len(t) - len(suffix) >= 4:
            return t[:-len(suffix)]
    return t


def _name_tokens(basename: str) -> List[str]:
    return [t for t in _NAME_SPLIT_RE.split(basename.lower()) if t]


def expand_terms(terms: List[str], feedback: Optional[str]) -> List[str]:
    """E = 整形词；反馈词与任一整形词词干不同时追加。"""
    if feedback and all(stem(t) != stem(feedback) for t in terms):
        return [*terms, feedback]
    return list(terms)


def _acronym_members(terms: List[str], tokens: List[str]) -> set:
    """整形词中连续 2~3 个 ASCII 词的首字母缩写等于目录名某个完整 token → 这些词算命中。"""
    token_set = set(tokens)
    hit = set()
    for n in (2, 3):
        for i in range(len(terms) - n + 1):
            run = terms[i:i + n]
            if all(t.isascii() and t for t in run) and "".join(t[0] for t in run) in token_set:
                hit.update(range(i, i + n))
    return hit


def term_overlap(terms: List[str], basename: str, feedback: Optional[str] = None) -> float:
    """E 中与目录名匹配的比例：词干（ASCII ≥3 / CJK ≥2）为目录名子串，或属于命中的缩写。"""
    expanded = expand_terms(terms, feedback)
    if not expanded:
        return 0.0
    name = basename.lower()
    acronym = _acronym_members(terms, _name_tokens(name))
    matched = 0
    for i, term in enumerate(expanded):
        s = stem(term)
        if (_long_enough(s) and s in name) or i in acronym:
            matched += 1
    return matched / len(expanded)


def _matches_term(token: str, terms: List[str]) -> bool:
    token_stem = stem(token)
    return any((_long_enough(stem(t)) and stem(t) in token) or (_long_enough(token_stem) and token_stem in t)
               for t in terms)


def feedback_token(terms: List[str], names: List[Tuple[str, str]]) -> Optional[str]:
    """伪相关反馈：names=(repo, basename)。n(tok)=出现的不同仓库数。
    有 n≥2 且与整形词同词干的 token → 取 n 最小者（最具体）；否则取 n≥3 的非匹配 token 中 n 最大者；
    同分取更长、再按字典序；都没有则 None。"""
    repos_by_token: Dict[str, set] = {}
    for repo, basename in names:
        for token in set(_name_tokens(basename)):
            if token in _FEEDBACK_STOP or not _long_enough(token):
                continue
            repos_by_token.setdefault(token, set()).add(repo)
    matching = [(tok, len(r)) for tok, r in repos_by_token.items() if len(r) >= 2 and _matches_term(tok, terms)]
    if matching:
        return min(matching, key=lambda x: (x[1], -len(x[0]), x[0]))[0]
    frequent = [(tok, len(r)) for tok, r in repos_by_token.items() if len(r) >= 3]
    if frequent:
        return min(frequent, key=lambda x: (-x[1], -len(x[0]), x[0]))[0]
    return None


def provenance_final(trusted: bool, fork: bool, stars: Optional[int]) -> float:
    if trusted:
        return 1.0
    norm = stars_norm(stars)
    return 0.25 * norm if fork else 0.25 + 0.5 * norm


def _member_key(m: Dict[str, Any]) -> Tuple[float, str, str]:
    """稳定排序键：prerank 降序，再按 (repo, subdir)。"""
    return (-m["prerank"], m["repo"], m["subdir"])


def _settle(outcomes) -> Tuple[List[Any], List[BaseException]]:
    """gather(return_exceptions=True) 结果：RateLimitError 立即抛出；返回 (成功结果的 items, 其他错误)。"""
    ok: List[Any] = []
    errors: List[BaseException] = []
    for outcome in outcomes:
        if isinstance(outcome, RateLimitError):
            raise outcome
        if isinstance(outcome, BaseException):
            errors.append(outcome)
        elif isinstance(outcome, dict):
            ok.append(outcome.get("items", []) or [])
    return ok, errors


def _items(outcome) -> List[Dict[str, Any]]:
    return (outcome.get("items", []) or []) if isinstance(outcome, dict) else []


def _reserved(rest: List[List[Dict[str, Any]]], slots: int) -> List[List[Dict[str, Any]]]:
    """K 截断外的预留组：先取含选择性可信候选的组（按可信检索名次），再取含 overlap>0 的仓库通道候选的组
    （按仓库检索名次）。"""
    picked: List[List[Dict[str, Any]]] = []
    rules = ((lambda m: m["exempt"], "trusted_rank"),
             (lambda m: m["repo_rank"] is not None and m["overlap"] > 0, "repo_rank"))
    for match, rank_key in rules:
        ranked = sorted((g for g in rest if g not in picked and any(match(m) for m in g)),
                        key=lambda g: min(m[rank_key] for m in g if match(m)))
        for g in ranked:
            if len(picked) >= slots:
                return picked
            picked.append(g)
    return picked




def _names(items) -> List[Tuple[str, str]]:
    names = []
    for it in items:
        repo = (it.get("repository") or {}).get("full_name")
        subdir = _subdir_from_path(it.get("path") or "")
        if repo and subdir is not None and not _is_excluded(subdir):
            names.append((repo, _basename(repo, subdir)))
    return names



def hermes_compat(fm: Optional[Dict[str, Any]]) -> str:
    """Hermes 兼容标记：name+desc+type+version 齐 → "✓"，缺 type/version → 需补默认。"""
    if not is_true_skill(fm):
        return "⚠needs-default"
    if fm.get("type") and fm.get("version"):
        return "✓"
    return "⚠needs-default"


# ── 反推子目录 / 排除 / 解码 ─────────────────────────────────────────
def _subdir_from_path(path: str) -> Optional[str]:
    """从 ``a/b/SKILL.md`` 反推 skill 子目录 ``a/b``；根目录 SKILL.md → ""。"""
    if not path or not path.endswith("SKILL.md"):
        return None
    return path[: -len("SKILL.md")].strip("/")


def _is_excluded(subdir: str) -> bool:
    """子目录任一路径段命中 config.SKILL_EXCLUDE_DIRS → 排除（模板/示例等非真 skill）。"""
    segs = [s for s in (subdir or "").lower().split("/") if s]
    for ex in config.SKILL_EXCLUDE_DIRS:
        exs = ex.strip("/").lower()
        if exs and exs in segs:
            return True
    return False


def _decode_content(content: Any) -> Optional[str]:
    """从 contents API 文件 dict（base64）解出文本。"""
    if not isinstance(content, dict):
        return None
    raw = content.get("content")
    if raw is None:
        return None
    if (content.get("encoding") or "base64") == "base64":
        try:
            return base64.b64decode(raw).decode("utf-8", errors="ignore")
        except Exception:
            return None
    return str(raw)


class SkillDiscoveryEngine(SearchEngine):
    name = "skill"
    tier = 1

    def __init__(self, client: Optional[GitHubClient] = None):
        self._client = client or GitHubClient()

    async def search(self, options: SearchOptions) -> List[SearchResult]:
        client = self._client
        terms = shape_query(options.query)
        trusted = tuple(config.SKILL_TRUSTED_SOURCES)
        trusted_lower = {s.lower() for s in trusted}
        repo_search = getattr(client, "repo_search", None)

        def general_q(ts: List[str]) -> str:
            return f"{' '.join(ts)} {config.SKILL_CODE_SEARCH_PATH}".strip()

        # 第一阶段：通用 + 可信来源。GitHub code search 的 score 恒为 1.0，通道分按名次计。
        jobs = [client.code_search(general_q(terms), per_page=config.SKILL_GENERAL_PER_PAGE)]
        if trusted:
            repos = " ".join(f"repo:{s}" for s in trusted)
            jobs.append(client.code_search(f"{general_q(terms)} {repos}", per_page=config.SKILL_TRUSTED_PER_PAGE))
        outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        _, errors = _settle(outcomes)
        if len(errors) == len(jobs):
            raise errors[0]
        general = _items(outcomes[0])
        trusted_items = _items(outcomes[1]) if len(outcomes) > 1 else []
        sources = [("general", general, config.SKILL_GENERAL_PER_PAGE),
                   ("trusted", trusted_items, config.SKILL_TRUSTED_PER_PAGE)]

        feedback: Optional[str] = None
        stars: Dict[str, int] = {}
        repo_items: List[Dict[str, Any]] = []
        if general or trusted_items:
            feedback = feedback_token(terms, _names(general + trusted_items))
            if feedback:
                path_items, repo_items = await self._phase2(client, repo_search, feedback, with_path=True)
                sources.append(("path", path_items, config.SKILL_PATH_PER_PAGE))
        elif not errors and len(terms) >= 2:
            relaxed = await asyncio.gather(
                client.code_search(general_q(relaxed_terms(terms)), per_page=config.SKILL_GENERAL_PER_PAGE),
                return_exceptions=True)
            _settle(relaxed)
            relaxed_items = _items(relaxed[0])
            sources = [("relaxed", relaxed_items, config.SKILL_GENERAL_PER_PAGE)]
            feedback = feedback_token(terms, _names(relaxed_items)) if relaxed_items else None
            if feedback:
                _, repo_items = await self._phase2(client, repo_search, feedback, with_path=False)
        sources.append(("repo", self._repo_source(repo_items, stars), config.SKILL_REPO_PER_PAGE))
        selective = 0 < len(trusted_items) <= config.SKILL_SELECTIVE_TRUSTED_MAX

        groups = self._groups(self._candidates(sources, terms, feedback, trusted_lower, selective))
        top = groups[:config.SKILL_ENRICH_TOP_K]
        top = top + _reserved(groups[config.SKILL_ENRICH_TOP_K:], config.SKILL_RESERVED_SLOTS)
        if not top:
            raise EngineError("skill: no valid SKILL.md entities found")

        heads = [min(g, key=_member_key) for g in top]
        meta_repos: List[str] = []
        for m in sorted((m for g in top for m in g), key=_member_key):
            if m["repo"] not in meta_repos and m["repo"] not in stars and not m["trusted"]:
                meta_repos.append(m["repo"])
        meta_repos = meta_repos[:config.SKILL_REPO_META_CAP]
        get_repo = getattr(client, "get_repo", None)
        meta_jobs = [get_repo(r) for r in meta_repos] if callable(get_repo) else []
        fetched = await asyncio.gather(*[client.get_contents(h["repo"], h["path"]) for h in heads], *meta_jobs,
                                       return_exceptions=True)
        _settle(fetched)                       # 补全中的 RateLimitError 同样立即抛出
        contents = fetched[:len(heads)]
        for repo, meta in zip(meta_repos, fetched[len(heads):]):
            if isinstance(meta, dict) and isinstance(meta.get("stargazers_count"), int):
                stars[repo] = meta["stargazers_count"]

        # 有效组头回填 contents 的 blob sha，再按组头 sha 合组（保留组头 _member_key 更小的一组）。
        merged: List[Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]] = []
        by_sha: Dict[str, int] = {}
        for group, head, content in zip(top, heads, contents):
            if isinstance(content, BaseException) or not isinstance(content, dict):
                continue
            fm = parse_frontmatter(_decode_content(content) or "")
            if not is_true_skill(fm):
                continue
            actual_sha = str(content.get("sha") or "")
            if actual_sha and head["sha"] and actual_sha != head["sha"]:
                # Search indexed an older blob. Only this fetched member is
                # verified for the new content; do not borrow an old copy's URL.
                group = [head]
            if actual_sha:
                head["sha"] = actual_sha
            if head["sha"] and head["sha"] in by_sha:
                merged[by_sha[head["sha"]]][0].extend(group)
                continue
            if head["sha"]:
                by_sha[head["sha"]] = len(merged)
            merged.append((list(group), head, fm))

        scored: List[_Scored] = []
        for group, head, fm in merged:
            rep = min(group, key=lambda m: (0 if m["trusted"] else 1, 1 if m["fork"] else 0,
                                            -stars.get(m["repo"], -1), -m["prerank"], m["repo"], m["subdir"]))
            rep_stars = stars.get(rep["repo"])
            final = (0.60 * max(m["relevance"] for m in group)
                     + 0.25 * head["gate"] * provenance_final(rep["trusted"], rep["fork"], rep_stars)
                     + 0.15 * _frontmatter_completeness(fm))
            source = "trusted" if rep["trusted"] else ("fork" if rep["fork"] else "other")
            desc = str(fm.get("description") or "")
            snippet = (f"{desc[:160]} · score={final:.3f} · copies={len(group)} · source={source} · "
                       f"stars={rep_stars if rep_stars is not None else '?'} · hermes={hermes_compat(fm)}")
            url = f"https://github.com/{rep['repo']}/tree/HEAD/{rep['subdir']}".rstrip("/")
            scored.append(((-final, rep["repo"], rep["subdir"]),
                           SearchResult(title=str(fm.get("name") or "")[:200], url=url,
                                        snippet=snippet[:500], source_tag="skill")))
        if not scored:
            raise EngineError("skill: no valid SKILL.md entities found")
        scored.sort(key=lambda t: t[0])
        return [sr for _, sr in scored][:options.count]

    @staticmethod
    async def _phase2(client, repo_search, feedback: str, *, with_path: bool):
        """按反馈词并发：目录名 code search（可选）+ 仓库搜索（按 star）。单通道非限流错误按空处理。"""
        jobs = []
        if with_path:
            jobs.append(client.code_search(f"{feedback} in:path {config.SKILL_CODE_SEARCH_PATH}",
                                           per_page=config.SKILL_PATH_PER_PAGE))
        if callable(repo_search):
            jobs.append(repo_search(f"{feedback} skill", per_page=config.SKILL_REPO_PER_PAGE))
        outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        _settle(outcomes)
        path_items: List[Dict[str, Any]] = []
        repo_items: List[Dict[str, Any]] = []
        if with_path and not isinstance(outcomes[0], BaseException):
            path_items = (outcomes[0] or {}).get("items", []) or []
        if callable(repo_search) and not isinstance(outcomes[-1], BaseException):
            repo_items = (outcomes[-1] or {}).get("items", []) or []
        return path_items, repo_items

    @staticmethod
    def _repo_source(repo_items, stars: Dict[str, int]):
        """仓库搜索结果 → 根目录 SKILL.md 候选（按原顺序）；顺带记录 stars。"""
        items = []
        for repo in repo_items:
            name = repo.get("full_name")
            if not name:
                continue
            if isinstance(repo.get("stargazers_count"), int):
                stars[name] = repo["stargazers_count"]
            items.append({"repository": {"full_name": name, "fork": bool(repo.get("fork"))}, "path": "SKILL.md", "sha": ""})
        return items

    @staticmethod
    def _candidates(sources, terms: List[str], feedback: Optional[str], trusted_lower,
                    selective: bool = False) -> List[Dict[str, Any]]:
        """(通道, items, per_page) → 候选；通道分 = 1-(名次-1)/per_page，多通道取最大。"""
        cands: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for channel, items, per_page in sources:
            for rank, it in enumerate(items, 1):
                repository = it.get("repository") or {}
                repo = repository.get("full_name")
                path = it.get("path") or ""
                subdir = _subdir_from_path(path)
                if not repo or subdir is None or _is_excluded(subdir):
                    continue
                s_ch = max(0.0, 1 - (rank - 1) / per_page)
                cand = cands.setdefault((repo, subdir), {
                    "repo": repo, "subdir": subdir, "path": path, "sha": str(it.get("sha") or ""),
                    "fork": bool(repository.get("fork")), "trusted": repo.lower() in trusted_lower, "s_ch": 0.0,
                    "trusted_rank": None, "repo_rank": None,
                })
                cand["s_ch"] = max(cand["s_ch"], s_ch)
                if channel == "trusted" and cand["trusted_rank"] is None:
                    cand["trusted_rank"] = rank
                if channel == "repo" and cand["repo_rank"] is None:
                    cand["repo_rank"] = rank
                if not cand["sha"] and it.get("sha"):
                    cand["sha"] = str(it["sha"])
        for c in cands.values():
            overlap = term_overlap(terms, _basename(c["repo"], c["subdir"]), feedback)
            c["overlap"] = overlap
            c["exempt"] = bool(selective and c["trusted"] and c["trusted_rank"] is not None)
            c["relevance"] = 0.5 * c["s_ch"] + 0.5 * overlap
            c["gate"] = 1.0 if (overlap > 0 or c["exempt"]) else 0.3
            pre = 1.0 if c["trusted"] else (0.0 if c["fork"] else 0.5)
            c["prerank"] = 0.70 * c["relevance"] + 0.30 * c["gate"] * pre
        return list(cands.values())

    @staticmethod
    def _groups(cands: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """同仓同名目录留最浅路径；相同非空 sha 合并；组按最佳成员稳定键排序。"""
        def depth(c):
            return (len([s for s in c["subdir"].split("/") if s]), c["subdir"])

        shallowest: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for c in cands:
            key = (c["repo"], _basename(c["repo"], c["subdir"]))
            if key not in shallowest or depth(c) < depth(shallowest[key]):
                shallowest[key] = c
        by_sha: Dict[str, List[Dict[str, Any]]] = {}
        groups: List[List[Dict[str, Any]]] = []
        for c in shallowest.values():
            if not c["sha"]:
                groups.append([c])
            elif c["sha"] in by_sha:
                by_sha[c["sha"]].append(c)
            else:
                by_sha[c["sha"]] = [c]
                groups.append(by_sha[c["sha"]])
        groups.sort(key=lambda g: _member_key(min(g, key=_member_key)))
        return groups

    async def health_check(self, *, deep: bool = False) -> EngineCheckResult:
        """检查 GITHUB_TOKEN + 验证 code search 实际可用；标注 skill 信源状态。"""
        import httpx
        token = config.get_env("GITHUB_TOKEN")
        if not token:
            return EngineCheckResult(
                engine=self.name,
                status="fail",
                tier=self.tier,
                summary="GITHUB_TOKEN not configured",
                details="Skill Discovery uses GitHub code search which requires authentication",
                requirements=["env:GITHUB_TOKEN"],
                repair=[
                    "Set GITHUB_TOKEN in your shell or ~/.hermes/.env:",
                    "  export GITHUB_TOKEN=your_token_here",
                    "Rerun: wrr-cli.py doctor --engine skill",
                ],
                evidence={"env.GITHUB_TOKEN": "missing"},
            )

        # 验证 GitHub code search 实际可用（轻量：只取 1 条 SKILL.md）
        code_search_ok = False
        code_search_detail = ""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    "https://api.github.com/search/code",
                    params={"q": "filename:SKILL.md", "per_page": 1},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                )
            if resp.status_code == 200:
                data = resp.json()
                count = data.get("total_count", 0)
                code_search_ok = True
                code_search_detail = f"{count} SKILL.md files found"
            elif resp.status_code == 403:
                code_search_detail = f"rate limited ({resp.status_code})"
            else:
                code_search_detail = f"search failed ({resp.status_code})"
        except Exception as e:
            code_search_detail = f"search error: {type(e).__name__}"

        # Vercel skills 信源检查（可选，不影响状态）
        vercel_ok = False
        vercel_detail = ""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    "https://api.github.com/repos/vercel-labs/skills",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                )
            if resp.status_code == 200:
                vercel_ok = True
                vercel_detail = "vercel-labs/skills accessible"
            elif resp.status_code == 404:
                vercel_detail = "vercel-labs/skills not found"
            else:
                vercel_detail = f"vercel-labs/skills status {resp.status_code}"
        except Exception:
            vercel_detail = "vercel-labs/skills unreachable"

        if code_search_ok:
            return EngineCheckResult(
                engine=self.name,
                status="ok",
                tier=self.tier,
                summary=f"GitHub code search OK ({code_search_detail})",
                active_backend="github-code-search",
                evidence={
                    "env.GITHUB_TOKEN": "present",
                    "code_search": code_search_detail,
                    "vercel_labs_skills": "accessible" if vercel_ok else vercel_detail,
                },
            )
        else:
            return EngineCheckResult(
                engine=self.name,
                status="fail",
                tier=self.tier,
                summary=f"GitHub code search failed: {code_search_detail}",
                details="Token is set but code search is unavailable",
                repair=[
                    "Check GitHub token permissions (requires public_repo scope)",
                    f"GitHub API returned: {code_search_detail}",
                    "Regenerate token at https://github.com/settings/tokens",
                    "Verify network can reach api.github.com",
                ],
                evidence={
                    "env.GITHUB_TOKEN": "present",
                    "code_search": code_search_detail,
                },
            )
