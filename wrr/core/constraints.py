"""Explicit site:hostname[/path] hints with provider-independent hostname gates."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence, TYPE_CHECKING
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from .contracts import SourceRun

_TOKEN = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s]+''')
_SITE = re.compile(r"(?i)(?<![\w./:@])site:")
_HOST = re.compile(r"(?i)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.?\Z")
_PATH = re.compile(r"/(?:[A-Za-z0-9._~!$&'()*+,;=:@/-]|%[0-9A-Fa-f]{2})*\Z")


class SiteConstraintError(ValueError):
    """Safe, query-free syntax errors suitable for public CLI/RPC diagnostics."""


@dataclass(frozen=True)
class SourceConstraints:
    site_hosts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    site_path_hints: tuple[str, ...] = ()

    def matches(self, url: str) -> bool:
        if not self.site_hosts:
            return True
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if (parsed.scheme.lower() not in {"http", "https"} or "\\" in parsed.netloc
                    or not _HOST.fullmatch(host)):
                return False
        except (ValueError, TypeError):
            return False
        host = host.lower().removesuffix(".")
        return all(host == target or host.endswith("." + target) for target in self.site_hosts)

    def filter_runs(self, runs: Sequence[SourceRun]) -> dict[str, int]:
        """Remove domain mismatches before fusion; return counts, never excluded URLs."""
        excluded = {}
        if not self.site_hosts:
            return excluded
        for run in runs:
            kept = [row for row in run.results if self.matches(row.url)]
            dropped = len(run.results) - len(kept)
            if dropped:
                excluded[run.source_id] = dropped
                run.results = kept
        return excluded


def parse_constraints(query: str) -> SourceConstraints:
    """Support positive, unquoted site:hostname[/path]; never infer domains.

    Boolean/negative/grouped site expressions and distinct repeated sites are
    rejected rather than accidentally interpreting their scopes as conjunctions.
    Quoted text and the historical site.hostname spelling are not operators.
    Paths use ASCII URI path syntax (or valid UTF-8 percent encoding), remain
    provider hints in the original query, and are never delivery hard gates.
    """
    tokens = [m.group() for m in _TOKEN.finditer(query)]
    unquoted = [token for token in tokens if token[0] not in {'"', "'"}]
    warnings = (("nonstandard_site_syntax:not_enforced_use_site_colon_hostname",)
                if any(re.match(r"(?i)^site\.[a-z0-9]", token) for token in unquoted) else ())
    sites = [token for token in unquoted if _SITE.search(token)]
    if not sites:
        return SourceConstraints(warnings=warnings)
    if (any(token.upper() in {"OR", "NOT"} or "|" in token or "(" in token or ")" in token
            for token in unquoted)
            or any(not token.lower().startswith("site:") for token in sites)):
        raise SiteConstraintError("unsupported site constraint syntax: only positive site:hostname[/path] is supported; no negation, OR, or grouping")
    hosts = []
    path_hints = []
    for token in sites:
        value = token[5:]
        host, separator, path = value.partition("/")
        if not host or len(host.removesuffix(".")) > 253 or not _HOST.fullmatch(host):
            raise SiteConstraintError("invalid site constraint: expected site:hostname[/path] without URL, userinfo, port, or wildcard")
        if separator:
            path = "/" + path
            try:
                decoded = unquote(path, errors="strict")
            except UnicodeDecodeError:
                raise SiteConstraintError("invalid site constraint: path requires valid UTF-8 encoding") from None
            if (not _PATH.fullmatch(path)
                    or any(ord(char) < 33 or ord(char) == 127 or char == "\\" for char in decoded)):
                raise SiteConstraintError("invalid site constraint: expected URI path without query, fragment, backslash, or invalid encoding")
            path_hints.append(value)
        hosts.append(host.lower().removesuffix("."))
    unique = tuple(dict.fromkeys(hosts))
    if len(unique) > 1:
        raise SiteConstraintError("unsupported site constraint syntax: multiple distinct site hostnames")
    if path_hints:
        warnings += ("site_path_soft_hint:not_hard_enforced",)
    return SourceConstraints(unique, warnings, tuple(dict.fromkeys(path_hints)))
