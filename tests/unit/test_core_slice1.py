"""Slice 1 contracts: evidence invariants, judge wire/failure semantics, rubric policies."""
import asyncio
import json

import httpx
import pytest

from wrr.core import rubric
from wrr.core.contracts import (
    Candidate, Contribution, ContractViolation, Evidence, Health, JudgeDiagnostics, Kind, Plan,
    PrivacyClass, Provision, Quality, SourceDescriptor, SourceProfile, SourceRun, RunStatus,
    Sufficiency, Transport, validate_evidence,
)
from wrr.core.judge import (
    Answer, JudgeError, NoJudge, ReplayJudge, TypeSafeJudge, choice, noul, resolve_judge, score,
)


def _run(coro):
    return asyncio.run(coro)


def _src(id, *, primary=False, transport=Transport.API, health=Health.HEALTHY,
         provision=Provision.AUTHORIZED, probe=None):
    return SourceDescriptor(id=id, kind=Kind.WEB, transport=transport, upstream_family=id,
                            privacy_class=PrivacyClass.PUBLIC, profile=SourceProfile(covers=("web",)),
                            provision=provision, health=health, primary=primary, light_probe_ok=probe)


def _cand(cid, url, privacy=PrivacyClass.PUBLIC):
    return Candidate(cid=cid, title=cid, url=url, snippet="s", contributions=[Contribution("exa", 1)],
                     privacy_class=privacy, source_kind=Kind.WEB)


class _R:
    def __init__(self, url):
        self.url = url


# ── contracts ─────────────────────────────────────────────────────────
def test_routable_requires_authorized_and_health():
    assert _src("a").routable
    assert _src("a", health=Health.DEGRADED).routable
    assert not _src("a", health=Health.UNKNOWN).routable
    assert _src("a", health=Health.UNKNOWN, probe=True).routable
    assert not _src("a", health=Health.COOLDOWN).routable
    assert not _src("a", provision=Provision.CONFIGURED).routable


def _evidence(results, runs, *, executed="ok", relevance="judged", posts=1, calls=1):
    return Evidence(query="q", plan=Plan(["exa"], {"exa": "forced:primary"}, "typesafe"), runs=runs,
                    results=results,
                    quality=Quality(executed, relevance, Sufficiency(0.7, "accept", 0.8, "delivered"), 1),
                    deadline_ms=1000, elapsed_ms=10, judge=JudgeDiagnostics(calls=calls, posts=posts))


def test_evidence_invariants():
    runs = [SourceRun("exa", RunStatus.OK, 5, [_R("https://a.test/1")])]
    ok = _cand("c0", "https://a.test/1")
    validate_evidence(_evidence([ok], runs))
    json.dumps(_evidence([ok], runs).to_dict())

    with pytest.raises(ContractViolation):
        validate_evidence(_evidence([_cand("c1", "https://not-from-run.test/")], runs))
    private = _cand("c2", "https://a.test/1", PrivacyClass.LOCAL)
    private.judgment = {"rel": 0.9}
    with pytest.raises(ContractViolation):
        validate_evidence(_evidence([private], runs))
    with pytest.raises(ContractViolation):
        validate_evidence(_evidence([ok], runs, posts=2, calls=1))
    with pytest.raises(ContractViolation):
        validate_evidence(_evidence([ok], runs, executed="cancelled", relevance="judged"))


# ── judge ─────────────────────────────────────────────────────────────
def _transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


def test_typesafe_wire_shape_and_parse():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={
            "model": "jev-1.13.0",
            "answers": {
                "a": {"type": "noul", "noul": 0.9},
                "b": {"type": "choice", "choice": "x", "probabilities": {"x": 0.7, "y": 0.3}, "confidence": 0.6},
                "c": {"type": "score", "score": 1.2, "probabilities": {"0": 0.2, "1": 0.4, "2": 0.4}, "confidence": 0.5},
            },
            "usage": {"input_tokens": 10, "output_tokens": 0},
        }, headers={"x-request-id": "req1"})

    questions = {"a": noul("q?", true="t", false="f"), "b": choice("pick", {"x": "X", "y": None}),
                 "c": score("rate", ["low", "mid", "high"])}
    judge = TypeSafeJudge("k", client=_transport(handler))
    response = _run(judge.judge({"request": {"query": "q"}}, questions))

    assert seen["auth"] == "Bearer k"
    assert set(seen["body"]) == {"model", "state", "questions"}
    assert seen["body"]["questions"]["a"] == {"type": "noul", "instructions": "q?", "criteria": {"true": "t", "false": "f"}}
    assert seen["body"]["questions"]["b"]["criteria"] == {"x": "X", "y": None}
    assert response.answers["a"].p == 0.9
    assert response.answers["b"].choice == "x" and response.answers["b"].confidence == 0.6
    assert response.answers["c"].score == 1.2
    assert response.model == "jev-1.13.0" and response.usage == {"input_tokens": 10, "output_tokens": 0}
    assert response.request_id == "req1" and response.posts == 1


@pytest.mark.parametrize("status,reason", [(401, "authentication"), (403, "authentication"), (500, "http_error"), (429, "http_error")])
def test_typesafe_http_failures_raise(status, reason):
    judge = TypeSafeJudge("k", client=_transport(lambda r: httpx.Response(status)))
    with pytest.raises(JudgeError) as info:
        _run(judge.judge("s", {"a": noul("q")}))
    assert info.value.reason == reason


def test_typesafe_malformed_answers_raise():
    cases = [
        {"answers": {"a": {"type": "noul", "noul": 1.5}}},
        {"answers": {"a": {"type": "choice", "choice": "x"}}},
        {"answers": {"b": {"type": "noul", "noul": 0.5}}},
        {"answers": {"a": {"type": "noul", "noul": 0.5}, "extra": {"type": "noul", "noul": 0.1}}},
        "not json",
    ]
    for payload in cases:
        content = payload if isinstance(payload, str) else json.dumps(payload)
        judge = TypeSafeJudge("k", client=_transport(lambda r, c=content: httpx.Response(200, content=c)))
        with pytest.raises(JudgeError) as info:
            _run(judge.judge("s", {"a": noul("q")}))
        assert info.value.reason == "malformed_response"


def test_typesafe_single_post_and_deadline():
    calls = []

    async def slow(request):
        calls.append(1)
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"answers": {"a": {"type": "noul", "noul": 0.5}}})

    async def main():
        judge = TypeSafeJudge("k", client=_transport(slow), soft_budget=0.05)
        with pytest.raises(JudgeError) as info:
            await judge.judge("s", {"a": noul("q")})
        assert info.value.reason == "timeout"
        # deadline already past → no request is made at all
        judge2 = TypeSafeJudge("k", client=_transport(slow))
        with pytest.raises(JudgeError):
            await judge2.judge("s", {"a": noul("q")}, deadline=asyncio.get_running_loop().time() - 1)

    _run(main())
    assert calls == [1]


def test_resolve_judge_order():
    host = NoJudge()
    host.backend = "host"
    assert resolve_judge(host_judge=host, env={"TYPESAFE_API_KEY": "k"}) is host
    assert resolve_judge(env={"TYPESAFE_API_KEY": "k"}).backend == "typesafe"
    assert resolve_judge(env={}).backend == "none"
    with pytest.raises(JudgeError):
        _run(NoJudge().judge("s", {"a": noul("q")}))


def test_replay_judge_records_calls_and_validates():
    replay = ReplayJudge([{"a": {"type": "noul", "noul": 0.2}}])
    response = _run(replay.judge({"x": 1}, {"a": noul("q")}))
    assert response.answers["a"].p == 0.2 and replay.calls[0][0] == {"x": 1}
    with pytest.raises(JudgeError):
        _run(replay.judge({"x": 1}, {"a": noul("q")}))  # script exhausted


# ── rubric: plan policy ───────────────────────────────────────────────
def _answers(fit: dict, shape="overview", conf=0.8, fresh=0.1):
    out = {"evidence_shape": Answer("choice", choice=shape, probabilities={shape: conf}, confidence=conf),
           "freshness_required": Answer("noul", noul=fresh)}
    for sid, p in fit.items():
        out[f"fit.{sid}"] = Answer("noul", noul=p)
    return out


def test_plan_policy_forced_primary_and_fit_zones():
    sources = [_src("exa", primary=True), _src("deepseek", primary=True), _src("openalex"),
               _src("reddit"), _src("github"), _src("dead", health=Health.UNHEALTHY)]
    plan = rubric.plan_policy(sources, _answers({"openalex": 0.9, "reddit": 0.5, "github": 0.1, "dead": 0.99}), "typesafe")
    assert plan.sources == ["exa", "deepseek", "openalex"]
    assert plan.reasons["exa"] == "forced:primary"
    assert plan.reasons["openalex"] == "fit:0.90"
    assert plan.reasons["reddit"] == "not_selected:0.50"
    assert plan.reasons["github"] == "rejected:0.10"
    assert "dead" not in plan.reasons
    assert plan.evidence_shape.choice == "overview"


def test_plan_policy_uncertain_fills_to_minimum_and_cap():
    sources = [_src("a"), _src("b"), _src("c")]
    plan = rubric.plan_policy(sources, _answers({"a": 0.5, "b": 0.2, "c": 0.4}), "typesafe")
    assert plan.sources == ["a", "c"]
    assert plan.reasons["a"] == "uncertain:0.50"
    many = [_src(f"s{i}", primary=i < 2) for i in range(8)]
    plan = rubric.plan_policy(many, _answers({f"s{i}": 0.9 for i in range(8)}), "typesafe")
    assert len(plan.sources) == rubric.MAX_SOURCES and plan.sources[:2] == ["s0", "s1"]


def test_plan_policy_default_without_judge():
    sources = [_src("exa", primary=True), _src("host:web", transport=Transport.HOST), _src("openalex")]
    plan = rubric.plan_policy(sources, None, "none")
    assert plan.sources == ["exa", "host:web"]
    assert plan.reasons == {"exa": "forced:primary", "host:web": "default"}
    assert plan.judge == "none" and plan.evidence_shape is None


def test_plan_questions_reference_sources_by_index():
    sources = [_src("exa"), _src("openalex")]
    questions = rubric.plan_questions(sources)
    state = rubric.plan_state("q", "2026-10-19", sources)
    assert set(questions) == {"evidence_shape", "freshness_required", "fit.exa", "fit.openalex"}
    assert questions["fit.openalex"].instructions["source"] == "`sources[1]`"
    assert state["sources"][1]["id"] == "openalex"
    assert set(questions["evidence_shape"].criteria) == set(rubric.EVIDENCE_SHAPES)


# ── rubric: select policy ─────────────────────────────────────────────
def _cand_answers(scores: dict, answerable=0.9, raw=0.7):
    out = {"answerable": Answer("noul", noul=answerable), "raw_candidate_sufficiency": Answer("noul", noul=raw)}
    for cid, (rel, inj) in scores.items():
        out[f"rel.{cid}"] = Answer("noul", noul=rel)
        out[f"inj.{cid}"] = Answer("noul", noul=inj)
    return out


def test_select_policy_thresholds_order_and_flags():
    cands = [_cand("c0", "https://a/0"), _cand("c1", "https://a/1"), _cand("c2", "https://a/2"),
             _cand("c3", "https://a/3"), _cand("c4", "https://a/4", PrivacyClass.AUTHENTICATED),
             _cand("c5", "https://a/5")]
    answers = _cand_answers({"c0": (0.6, 0.0), "c1": (0.9, 0.4), "c2": (0.9, 0.7), "c3": (0.2, 0.0)})
    out = rubric.select_policy(cands, answers, {"c0", "c1", "c2", "c3"})
    assert [c.cid for c in out.kept] == ["c1", "c0", "c4", "c5"]
    assert out.dropped_injection == ["c2"] and out.dropped_relevance == ["c3"]
    assert "inj:suspect" in cands[1].flags
    assert cands[4].flags == ["unjudged:private"] and cands[4].judgment == {}
    assert cands[5].flags == ["unjudged:overflow"]
    assert out.answerable == 0.9 and out.raw_sufficiency == 0.7


def test_select_policy_fail_open_keeps_order_but_local_injection_is_hard():
    cands = [_cand("c0", "https://a/0"), _cand("c1", "https://a/1"), _cand("c2", "https://a/2")]
    out = rubric.select_policy(cands, None, set(), local_injection_ids={"c1"})
    assert [c.cid for c in out.kept] == ["c0", "c2"]
    assert out.dropped_injection == ["c1"]
    assert all("unjudged:degraded" in c.flags for c in out.kept)
    assert out.answerable is None and out.raw_sufficiency is None


# ── rubric: recovery policy ───────────────────────────────────────────
def _next(action, conf=0.9):
    return Answer("choice", choice=action, probabilities={action: conf}, confidence=conf)


def test_recovery_policy():
    ok = dict(budget_ok=True, untried_sources=2)
    assert rubric.recovery_policy(0.3, _next("broaden_sources"), **ok).recover
    assert rubric.recovery_policy(0.3, _next("need_fresher"), **ok).prefer_fresh
    assert rubric.recovery_policy(0.3, _next("broaden_sources", conf=0.2), **ok).reason == "low_confidence"
    assert rubric.recovery_policy(0.8, _next("broaden_sources"), **ok).reason == "enough"
    assert rubric.recovery_policy(0.3, _next("accept"), **ok).reason == "enough"
    assert rubric.recovery_policy(0.3, _next("insufficient"), **ok).reason == "insufficient"
    assert rubric.recovery_policy(0.3, _next("broaden_sources"), budget_ok=False, untried_sources=2).reason == "budget_exhausted"
    assert rubric.recovery_policy(0.3, _next("broaden_sources"), budget_ok=True, untried_sources=0).reason == "no_untried_sources"
    assert rubric.recovery_policy(None, None, **ok).reason == "no_sufficiency_judgment"
