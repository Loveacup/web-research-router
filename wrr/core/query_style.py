"""Deterministic keyword queries for keyword-only sources; no text generation."""
from __future__ import annotations

import re

_STOPWORDS = frozenset("what how why is are the a an for of to in on with vs versus please github 的 了 是 在 和 与 关于 如何 怎么 什么 哪个 请".split())
_INTENT_PREFIX = frozenset("best latest open source repos repositories projects github find search show me about 帮我 找 搜索 最新 最好".split())
# Chinese runs carry no spaces, so intent phrases are peeled from the run's
# edges (longest first) instead of being matched as whole tokens.
_CJK_LEADING = tuple(sorted(
    "请帮我找一下 请帮我找 帮我找一下 帮我找找 帮我找 帮我搜一下 帮我搜 帮我查一下 帮我查 请找一下 找一下 查一下 搜一下 请问 请 关于 有关 有没有 有哪些 什么是 如何 怎么 怎样 哪些 哪个 最新的 最新 最好的 最好 一些 一下".split(),
    key=len, reverse=True))
_CJK_TRAILING = tuple(sorted(
    "的项目 的仓库 的例子 的库 项目 仓库 怎么样 如何 吗 呢 的".split(),
    key=len, reverse=True))
_CJK_RUN = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+$")


def _trim_cjk(run: str) -> str:
    changed = True
    while changed and run:
        if run in _CJK_LEADING or run in _CJK_TRAILING:
            return ""
        changed = False
        for phrase in _CJK_LEADING:
            if run.startswith(phrase) and len(run) > len(phrase):
                run = run[len(phrase):]
                changed = True
                break
        for phrase in _CJK_TRAILING:
            if run.endswith(phrase) and len(run) > len(phrase):
                run = run[:-len(phrase)]
                changed = True
                break
    return run


# Keep CJK runs intact and preserve technical names, including .net and c++.
# Latin tokens must stop at CJK so "Rust异步运行时" splits into two tokens.
_LATIN = r"[^\W_\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+"
_TOKENS = re.compile(r'"[^"\n]*"|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|' + _LATIN + r'(?:[-._+#]+' + _LATIN + r')*[+#]*|[.]' + _LATIN, re.UNICODE)


def to_keywords(query: str, *, max_terms: int = 6) -> str:
    """Keep quoted phrases, remove standalone filler and leading intent words.

    Intent words remain after the first topic token (``web search`` stays intact).
    Chinese runs are not segmented, so proper names are never substring-filtered.
    Empty extraction falls back to the original query.
    """
    terms: list[str] = []
    seen: set[str] = set()
    prefix = True
    for match in _TOKENS.finditer(query):
        raw = match.group()
        quoted = raw.startswith('"')
        token = raw if quoted else raw.lower()
        if not quoted and (token in _STOPWORDS or (prefix and token in _INTENT_PREFIX)):
            continue
        if not quoted and _CJK_RUN.match(token):
            token = _trim_cjk(token)
            if not token or token in _STOPWORDS or (prefix and token in _INTENT_PREFIX):
                continue
        prefix = False
        key = token.lower()
        if key not in seen:
            seen.add(key)
            terms.append(token)
    return " ".join(terms[:max(0, max_terms)]) or query
