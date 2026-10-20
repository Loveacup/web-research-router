"""Independent upstream families must not manufacture duplicate fusion votes."""

import pytest

from wrr.engines._fusion import rrf_fuse
from wrr.schemas import SearchResult


def test_github_and_skill_do_not_manufacture_independent_votes():
    shared = SearchResult("same repository", "https://github.com/example/repo")
    independent = SearchResult("other evidence", "https://example.org/evidence")
    fused = rrf_fuse({"github": [shared, shared], "skill": [shared],
                      "exa": [independent], "deepseek": [independent]})
    assert fused[0]["doc"].url == independent.url
    same = next(row for row in fused if row["doc"].url == shared.url)
    assert same["rrf"] == pytest.approx(1 / 61)
    assert same["doc"].fusion_sources == ["github", "skill"]


def test_community_wrapper_does_not_double_vote_same_upstream():
    item = SearchResult("same repository", "https://github.com/example/repo",
                        source_tag="github")
    fused = rrf_fuse({"github": [item], "community": [item], "last30days_en": [item]})
    assert fused[0]["rrf"] == pytest.approx(1 / 61)


