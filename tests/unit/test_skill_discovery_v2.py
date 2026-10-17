"""SkillDiscoveryEngine behaviour (P2b M1-M8, P2c N1-N10), zero network.

Each test guards one design rule; the mutation that must make it fail is named
in its docstring (acceptance p2b B01 / p2c C01).
"""
import asyncio
import base64

from wrr import config
from wrr.engines import skill_discovery as sd
from wrr.errors import EngineError, RateLimitError
from wrr.schemas import SearchOptions

TRUSTED_Q = " ".join(f"repo:{s}" for s in config.SKILL_TRUSTED_SOURCES)


def _md(name, desc="does things"):
    text = f"---\nname: {name}\ndescription: {desc}\n---\nbody"
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


def _item(repo, path, sha="", score=1.0, fork=False):
    return {"repository": {"full_name": repo, "fork": fork}, "path": path, "sha": sha, "score": score}


class Fake:
    """code_search answers from a per-call list; repo_search answers likewise; records every call."""

    def __init__(self, answers, stars=None, names=None, repo_answers=None, content_sha=None, repo_error=None):
        self.answers = list(answers)
        self.repo_answers = list(repo_answers or [])
        self.stars = stars or {}
        self.names = names or {}
        self.content_sha = content_sha or {}
        self.repo_error = repo_error
        self.queries = []
        self.repo_queries = []
        self.content_calls = []
        self.repo_calls = []
        self.commit_calls = []

    async def code_search(self, query, per_page=30):
        self.queries.append(query)
        items = self.answers.pop(0) if self.answers else []
        return {"items": items, "total_count": len(items), "incomplete_results": False}

    async def repo_search(self, query, per_page=10, sort="stars"):
        self.repo_queries.append(query)
        if self.repo_error:
            raise self.repo_error
        items = self.repo_answers.pop(0) if self.repo_answers else []
        return {"items": items, "total_count": len(items)}

    async def get_contents(self, repo, path):
        self.content_calls.append((repo, path))
        md = _md(self.names.get((repo, path), path.rsplit("/", 2)[-2] if "/" in path else repo.split("/")[1]))
        if (repo, path) in self.content_sha:
            md["sha"] = self.content_sha[(repo, path)]
        return md

    async def get_repo(self, repo):
        self.repo_calls.append(repo)
        return {"stargazers_count": self.stars.get(repo, 0)}

    async def subdir_commit_count(self, repo, path, since):
        self.commit_calls.append((repo, path))
        return 0


def search(fake, query, count=5):
    try:
        return asyncio.run(sd.SkillDiscoveryEngine(client=fake).search(SearchOptions(query=query, count=count)))
    except EngineError:
        return []


def test_changed_contents_sha_cannot_borrow_old_group_provenance():
    fake = Fake([[_item("m/mirror", "skills/pdf/SKILL.md", sha="old", score=9),
                  _item("c/upstream", "skills/pdf/SKILL.md", sha="old", score=1)], []],
                stars={"m/mirror": 1, "c/upstream": 5000},
                names={("m/mirror", "skills/pdf/SKILL.md"): "changed-mirror"},
                content_sha={("m/mirror", "skills/pdf/SKILL.md"): "new"})
    out = search(fake, "pdf")
    assert len(out) == 1
    assert out[0].title == "changed-mirror"
    assert out[0].url == "https://github.com/m/mirror/tree/HEAD/skills/pdf"
    assert "copies=1" in out[0].snippet
    assert "stars=1 " in out[0].snippet
    assert fake.content_calls == [("m/mirror", "skills/pdf/SKILL.md")]
    assert len(fake.repo_calls) <= config.SKILL_REPO_META_CAP


def test_query_shaping():
    """M1: shaping returns [original]."""
    assert sd.shape_query("去除中文文本 AI 写作痕迹的 skill") == ["去除中文文本", "ai", "写作痕迹"]
    assert sd.shape_query("skill for filling PDF forms and extracting text from PDFs") == [
        "filling", "pdf", "forms", "extracting", "text", "from", "pdfs"]
    assert sd.shape_query("推荐一个做 PPT 的 skill") == ["做", "ppt"]
    fake = Fake([[_item("o/r", "skills/pdf-forms/SKILL.md")], []])
    search(fake, "skill for filling PDF forms")
    assert fake.queries[0] == "filling pdf forms filename:SKILL.md"


def test_relaxed_search_drops_longest_term():
    """M8: relaxation removed."""
    fake = Fake([[], [], [_item("o/humanizer", "SKILL.md")]])
    out = search(fake, "去除中文文本 AI 写作痕迹的 skill")
    assert fake.queries[2:] == ["ai 写作痕迹 filename:SKILL.md"]
    assert [r.url for r in out] == ["https://github.com/o/humanizer/tree/HEAD"]


def test_trusted_search_issued():
    """M6: trusted search removed."""
    fake = Fake([[_item("x/agg", "skills/pdf/SKILL.md", score=9)],
                 [_item("anthropics/skills", "skills/pdf/SKILL.md", score=8)]])
    out = search(fake, "pdf")
    assert fake.queries[:2] == ["pdf filename:SKILL.md", f"pdf filename:SKILL.md {TRUSTED_Q}"]
    assert out[0].url == "https://github.com/anthropics/skills/tree/HEAD/skills/pdf"
    assert "source=trusted" in out[0].snippet


def test_sha_duplicates_collapse():
    """M2: sha collapse skipped (copies would each spend a K slot on enrichment)."""
    fake = Fake([[_item("a/one", "skills/pdf/SKILL.md", sha="s1", score=3),
                  _item("b/two", "x/pdf/SKILL.md", sha="s1", score=2),
                  _item("c/three", "pdf/SKILL.md", sha="s1", score=1)], []])
    out = search(fake, "pdf")
    assert len(out) == 1
    assert "copies=3" in out[0].snippet
    assert len(fake.content_calls) == 1


def test_representative_prefers_trusted_then_stars():
    """M7: representative ignores trusted/stars (takes the best-prerank member)."""
    fake = Fake([[_item("m/mirror", "skills/pdf/SKILL.md", sha="s1", score=9),
                  _item("f/forked", "skills/pdf/SKILL.md", sha="s1", score=8, fork=True),
                  _item("c/canon", "skills/pdf/SKILL.md", sha="s1", score=1)], []],
                stars={"m/mirror": 5, "f/forked": 99999, "c/canon": 5000})
    out = search(fake, "pdf")
    assert out[0].url == "https://github.com/c/canon/tree/HEAD/skills/pdf"
    fake = Fake([[_item("m/mirror", "skills/pdf/SKILL.md", sha="s2", score=9)],
                 [_item("anthropics/skills", "skills/pdf/SKILL.md", sha="s2", score=1)]],
                stars={"m/mirror": 90000, "anthropics/skills": 10})
    out = search(fake, "pdf")
    assert out[0].url == "https://github.com/anthropics/skills/tree/HEAD/skills/pdf"


def test_enrichment_bounded():
    """M3: K raised to 30."""
    items = [_item(f"o{i}/r{i}", f"skills/s{i}/SKILL.md", sha=f"h{i}", score=30 - i) for i in range(30)]
    fake = Fake([items, []])
    search(fake, "things")
    assert len(fake.content_calls) == config.SKILL_ENRICH_TOP_K == 6
    assert len(fake.repo_calls) <= config.SKILL_REPO_META_CAP


def test_no_listing_or_commit_calls():
    """M4: directory listing or subdir_commit_count re-added to the search path."""
    fake = Fake([[_item("o/r", "skills/pdf/SKILL.md"), _item("p/q", "a/b/pdf/SKILL.md")], []])
    search(fake, "pdf")
    assert fake.content_calls and all(path.endswith("SKILL.md") for _, path in fake.content_calls)
    assert fake.commit_calls == []


def test_relevance_dominates_order():
    """M5: relevance weight 0 in the final score."""
    fake = Fake([[_item("a/small", "skills/pdf-forms/SKILL.md", sha="a", score=10),
                  _item("b/famous", "skills/pdf-tools/SKILL.md", sha="b", score=1)], []],
                stars={"a/small": 0, "b/famous": 100000})
    out = search(fake, "pdf forms")
    assert [r.url.rsplit("/", 1)[-1] for r in out] == ["pdf-forms", "pdf-tools"]


def test_stars_break_ties_between_equal_candidates():
    fake = Fake([[_item("org/junk", "skills/x/SKILL.md", sha="j", score=1),
                  _item("org/good", "skills/y/SKILL.md", sha="g", score=1)], []],
                stars={"org/good": 80000, "org/junk": 1})
    out = search(fake, "zzz")
    assert out[0].url == "https://github.com/org/good/tree/HEAD/skills/y"
    assert "stars=80000" in out[0].snippet


def test_graceful_without_get_repo():
    class NoRepo(Fake):
        get_repo = None
    fake = NoRepo([[_item("o/r", "skills/pdf/SKILL.md")], []])
    out = search(fake, "pdf")
    assert len(out) == 1 and "stars=?" in out[0].snippet


# ── P2c ────────────────────────────────────────────────────────────────
def test_stem_overlap():
    """N1: stem() returns its input."""
    assert sd.stem("creating") == sd.stem("creator") == "creat"
    assert sd.stem("retrieval") == sd.stem("retriever") == "retriev"
    assert sd.term_overlap(["creating", "new"], "skill-creator", "creator") == 0.5


def test_acronym_overlap():
    """N2: acronym matching removed."""
    assert sd.term_overlap(["knowledge", "base", "retrieval"], "kb-retriever") == 1.0
    assert sd.term_overlap(["knowledge", "base"], "kb") == 1.0


def test_trusted_needs_lexical_evidence():
    """N3: evidence gate fixed at 1.0 (unrelated trusted skills outrank a matching one)."""
    fake = Fake([[_item("u/one", "skills/skill-creator/SKILL.md", sha="c", score=5)],
                 [_item("anthropics/skills", f"skills/{n}/SKILL.md", sha=n)
                  for n in ("algorithmic-art", "canvas-design", "claude-api", "docx", "pdf")]])
    out = search(fake, "skill for creating new agent skills")
    assert out[0].url == "https://github.com/u/one/tree/HEAD/skills/skill-creator"


def test_channel_score_follows_rank():
    """N4: rank-based channel score replaced by a constant 1.0 (GitHub scores are all 1.0)."""
    items = [_item("u/a", "skills/pdf-alpha/SKILL.md", sha="a"), _item("u/b", "skills/pdf-beta/SKILL.md", sha="b")]
    out = search(Fake([items, []]), "pdf")
    assert [r.url.rsplit("/", 1)[-1] for r in out] == ["pdf-alpha", "pdf-beta"]
    out = search(Fake([list(reversed(items)), []]), "pdf")
    assert [r.url.rsplit("/", 1)[-1] for r in out] == ["pdf-beta", "pdf-alpha"]


_HUMANIZER_GENERAL = [_item("a/x", "skills/humanizer-zh-next/SKILL.md", score=3),
                      _item("b/y", "humanizer-cn/SKILL.md", score=2),
                      _item("d/w", "skills/humanizer/SKILL.md", score=2),
                      _item("c/z", "deai-zh/SKILL.md", score=1)]
_ZH_QUERY = "去除中文文本 AI 写作痕迹的 skill"


def test_feedback_token_drives_phase2():
    """N5: feedback token always None."""
    assert sd.feedback_token(sd.shape_query(_ZH_QUERY), sd._names(_HUMANIZER_GENERAL)) == "humanizer"
    fake = Fake([list(_HUMANIZER_GENERAL), []])
    search(fake, _ZH_QUERY)
    assert "humanizer in:path filename:SKILL.md" in fake.queries
    assert fake.repo_queries == ["humanizer skill"]


def test_repo_channel_recalls_root_skill():
    """N6: repository channel removed."""
    fake = Fake([list(_HUMANIZER_GENERAL), []],
                repo_answers=[[{"full_name": "op/Humanizer-zh", "stargazers_count": 18000, "fork": False}]])
    out = search(fake, _ZH_QUERY)
    assert "https://github.com/op/Humanizer-zh/tree/HEAD" in [r.url for r in out]
    assert "op/Humanizer-zh" not in fake.repo_calls          # stars came from the repository search


_COMIC_GENERAL = [_item("a/x", "skills/comic-core/SKILL.md", score=3),
                  _item("b/y", "skills/twitter-to-comic/SKILL.md", score=2),
                  _item("c/z", "skills/ai-image-gen/SKILL.md", score=1)]


def test_path_channel_recalls_named_dir():
    """N7: path channel removed."""
    fake = Fake([list(_COMIC_GENERAL), [], [_item("JimLiu/baoyu-skills", "skills/baoyu-comic/SKILL.md", sha="bc", score=5)]])
    out = search(fake, "skill for creating comic strips with AI images")
    assert fake.queries[2] == "comic in:path filename:SKILL.md"
    assert "https://github.com/JimLiu/baoyu-skills/tree/HEAD/skills/baoyu-comic" in [r.url for r in out]


def test_code_search_cap():
    """N8: path search also issued after the relaxation."""
    fake = Fake([[], [], [_item("a/x", "skills/humanizer-zh/SKILL.md"), _item("b/y", "humanizer-cn/SKILL.md"),
                          _item("c/z", "humanizer/SKILL.md")]])
    search(fake, _ZH_QUERY)
    assert len(fake.queries) == 3
    assert fake.repo_queries == ["humanizer skill"]


def test_repo_rate_limit_propagates():
    """N9: RateLimitError from the repository channel swallowed."""
    fake = Fake([list(_COMIC_GENERAL), [], [_item("JimLiu/baoyu-skills", "skills/baoyu-comic/SKILL.md", score=5)]],
                repo_error=RateLimitError("GitHub rate limit exceeded"))
    try:
        asyncio.run(sd.SkillDiscoveryEngine(client=fake).search(SearchOptions(query="comic strips", count=5)))
    except RateLimitError:
        return
    raise AssertionError("repository-search rate limit did not propagate")


def test_repo_only_duplicates_merge_by_content_sha():
    """N10: post-enrichment sha merge removed."""
    fake = Fake([[_item("m/one", "skills/thing-a/SKILL.md", sha="x1", score=2),
                  _item("n/two", "skills/thing-b/SKILL.md", sha="x2", score=1)], [], []],
                repo_answers=[[{"full_name": "a/thing-skill", "stargazers_count": 50},
                               {"full_name": "b/thing-mirror", "stargazers_count": 5}]],
                content_sha={("a/thing-skill", "SKILL.md"): "S1", ("b/thing-mirror", "SKILL.md"): "S1"})
    out = search(fake, "thing")
    roots = [r for r in out if r.url in ("https://github.com/a/thing-skill/tree/HEAD",
                                         "https://github.com/b/thing-mirror/tree/HEAD")]
    assert len(roots) == 1
    assert roots[0].url == "https://github.com/a/thing-skill/tree/HEAD" and "copies=2" in roots[0].snippet


def test_feedback_prefers_specific_query_term():
    """N12: feedback picks the most frequent token first."""
    names = [(f"r{i}/x", "misc-tools") for i in range(15)] + [(f"c{i}/y", "comic-maker") for i in range(4)] + \
            [(f"i{i}/z", "image-gen") for i in range(9)]
    assert sd.feedback_token(["creating", "comic", "strips", "ai", "images"], names) == "comic"


def test_selective_trusted_search_exempt_from_gate():
    """N13: selective-trusted exemption removed (lexically unmatched official skill ranks below noise)."""
    general = [_item(f"g{i}/r", f"skills/noise-{i}/SKILL.md", sha=f"n{i}") for i in range(3)]
    out = search(Fake([general, [_item("anthropics/skills", "skills/pptx/SKILL.md", sha="p")]]), "powerpoint")
    assert out[0].url == "https://github.com/anthropics/skills/tree/HEAD/skills/pptx"


def test_selective_trusted_reaches_enrichment():
    """N14: selective-trusted reserved slot removed (6 matching candidates fill K)."""
    general = [_item(f"g{i}/r", f"skills/slide-deck-{i}/SKILL.md", sha=f"s{i}") for i in range(8)]
    fake = Fake([general, [_item("anthropics/skills", "skills/pptx/SKILL.md", sha="p")]])
    search(fake, "powerpoint slide decks")
    assert ("anthropics/skills", "skills/pptx/SKILL.md") in fake.content_calls


def test_repo_slots_survive_k_cut():
    """N11: reserved repository slots removed."""
    general = [_item(f"g{i}/r", f"skills/humanizer-{i}/SKILL.md", sha=f"h{i}") for i in range(8)]
    others = [{"full_name": f"z{i}/unrelated-{i}", "stargazers_count": 1} for i in range(9)]
    fake = Fake([general, [], []], repo_answers=[others + [{"full_name": "op/Humanizer-zh", "stargazers_count": 18000}]])
    search(fake, "humanizer")
    assert ("op/Humanizer-zh", "SKILL.md") in fake.content_calls
