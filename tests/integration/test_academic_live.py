"""Academic integration tests — real OpenAlex network access.

Disabled by default. Run explicitly with:
    WRR_LIVE=1 pytest tests/integration/test_academic_live.py -v
"""
import asyncio
import os

import pytest

from wrr.engines import academic as ac
from wrr.schemas import SearchOptions


@pytest.mark.integration
def test_openalex_live_single_source():
    if os.getenv("WRR_LIVE") != "1":
        pytest.skip("set WRR_LIVE=1 to run live academic test")
    out = asyncio.run(ac._fetch_openalex(SearchOptions("transformer attention", count=3)))
    assert out
    assert all("cited_by_count" in paper for paper in out)
    assert all(isinstance(paper["cited_by_count"], int) and paper["cited_by_count"] >= 0
               for paper in out)
    assert all(paper.get("title") for paper in out)
