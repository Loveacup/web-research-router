"""RRF with canonical URL identity and one vote per upstream family."""
from __future__ import annotations

from typing import Sequence

from wrr.engines._fusion import canonical_url

from . import rubric

from .contracts import Candidate, Contribution, Kind, PrivacyClass, SourceDescriptor, SourceRun

_PRIVACY_ORDER = {PrivacyClass.PUBLIC: 0, PrivacyClass.AUTHENTICATED: 1, PrivacyClass.LOCAL: 2}
# Local/authenticated provenance must win over a public web wrapper. For equally
# public kinds retain the first source's kind (no invented semantic hierarchy).
_KIND_ORDER = {kind: int(kind == Kind.LOCAL) for kind in Kind}


def fuse(runs: Sequence[SourceRun], inventory: Sequence[SourceDescriptor], *,
         k: int = 60) -> list[Candidate]:
    descriptors = {s.id: s for s in inventory}
    buckets: dict[str, tuple[Candidate, dict[str, float]]] = {}
    for run in runs:
        source = descriptors[run.source_id]
        for rank, result in enumerate(run.results, 1):
            key = canonical_url(result.url)
            if key not in buckets:
                buckets[key] = (Candidate(
                    cid="", title=result.title[:rubric.MAX_TITLE_CHARS], url=result.url,
                    snippet=result.snippet[:rubric.MAX_SNIPPET_CHARS],
                    contributions=[], privacy_class=source.privacy_class,
                    source_kind=source.kind, source_ts=result.source_ts,
                    clipped=(len(result.title) > rubric.MAX_TITLE_CHARS
                             or len(result.snippet) > rubric.MAX_SNIPPET_CHARS),
                ), {})
            candidate, families = buckets[key]
            candidate.contributions.append(Contribution(run.source_id, rank))
            if _PRIVACY_ORDER[source.privacy_class] > _PRIVACY_ORDER[candidate.privacy_class]:
                candidate.privacy_class = source.privacy_class
                candidate.source_kind = source.kind
            elif _KIND_ORDER[source.kind] > _KIND_ORDER[candidate.source_kind]:
                candidate.source_kind = source.kind
            families[source.upstream_family] = max(
                families.get(source.upstream_family, 0), 1 / (k + rank))
    candidates = []
    for candidate, families in buckets.values():
        candidate.rrf_score = sum(families.values())
        candidates.append(candidate)
    candidates.sort(key=lambda c: -c.rrf_score)
    for index, candidate in enumerate(candidates):
        candidate.cid = f"c{index}"
    return candidates
