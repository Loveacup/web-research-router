"""Plan → execute → fuse → judge/select → at most one recovery round."""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Mapping, Sequence, TYPE_CHECKING

from wrr.engines._fusion import canonical_url
from wrr.search_filter import _INSTRUCTION, _normalize, _public_url, candidate_sensitive

from . import rubric
from .contracts import (
    Candidate, Evidence, JudgeDiagnostics, Kind, Plan, PrivacyClass, Quality, RunStatus,
    SourceDescriptor, SourceRun, Sufficiency, validate_evidence,
)
from .constraints import parse_constraints
from .execute import execute
from .fuse import fuse
from .judge import Judge, JudgeError, JudgeResponse, Question
from .plan import plan as build_plan

if TYPE_CHECKING:
    from .sources import SourceAdapter



class _MeteredJudge:
    def __init__(self, judge: Judge, diagnostics: JudgeDiagnostics):
        self.delegate = judge
        self.backend = judge.backend
        self.diagnostics = diagnostics

    async def judge(self, state, questions: Mapping[str, Question], *, deadline=None) -> JudgeResponse:
        diagnostics = self.diagnostics
        if diagnostics.calls >= rubric.MAX_JUDGE_CALLS:
            raise JudgeError("budget", posts=0)
        diagnostics.calls += 1
        budget = None
        try:
            limit = min(deadline, asyncio.get_running_loop().time() + rubric.judge_budget_s(state, questions))
            if limit <= asyncio.get_running_loop().time():
                raise JudgeError("timeout", posts=0)
            try:
                async with asyncio.timeout_at(limit) as budget:
                    response = await self.delegate.judge(state, questions, deadline=limit)
            except TimeoutError:
                raise JudgeError("timeout", posts=1) from None
        except JudgeError as error:
            diagnostics.posts += getattr(error, "posts", int(error.reason != "unavailable"))
            reason = ("timeout" if error.reason == "cancelled" and budget is not None
                      and budget.expired() else error.reason)
            diagnostics.failures.append(reason)
            if reason == "cancelled":
                raise asyncio.CancelledError from None
            if reason != error.reason:
                raise JudgeError(reason, posts=0) from None
            raise
        diagnostics.posts += response.posts
        diagnostics.backend = response.backend
        diagnostics.model = response.model or diagnostics.model
        for key, value in response.usage.items():
            diagnostics.usage[key] = diagnostics.usage.get(key, 0) + value
        return response


def _screen(candidates: Sequence[Candidate], runs: Sequence[SourceRun],
            known_secret: str = "") -> tuple[set[str], set[str]]:
    """Screen full, unclipped material; exclude unsafe items, not the batch."""
    sensitive: set[str] = set()
    injections: set[str] = set()
    by_url = {canonical_url(c.url): c for c in candidates}
    for run in runs:
        for result in run.results:
            cid = by_url[canonical_url(result.url)].cid
            values = [_normalize(v) for v in (result.title, result.url, result.snippet, *result.highlights)]
            if _INSTRUCTION.search("\n".join(values)):
                injections.add(cid)
            candidate = by_url[canonical_url(result.url)]
            if candidate.privacy_class == PrivacyClass.PUBLIC and (
                    not _public_url(values[1]) or any(candidate_sensitive(v, known_secret) for v in values)):
                sensitive.add(cid)
    return sensitive, injections


def _execution_quality(runs: Sequence[SourceRun]) -> str:
    if any(run.status == RunStatus.CANCELLED for run in runs):
        return "cancelled"
    successes = sum(run.status in {RunStatus.OK, RunStatus.EMPTY} for run in runs)
    return "ok" if successes == len(runs) and runs else "partial" if successes else "failed"


def _scope(candidates: Sequence[Candidate], delivered: Sequence[Candidate],
           dropped_injection: Sequence[str], dropped_relevance: Sequence[str]) -> dict:
    return {
        "private_ids": [c.cid for c in candidates if c.privacy_class != PrivacyClass.PUBLIC],
        "unjudged_ids": [c.cid for c in candidates if not c.judgment],
        "suspect_ids": [c.cid for c in candidates if "inj:suspect" in c.flags],
        "sensitive_ids": [c.cid for c in candidates if "unjudged:sensitive" in c.flags],
        "excluded_count": sum(not c.judgment or "inj:suspect" in c.flags for c in candidates),
        "dropped_injection_ids": list(dropped_injection),
        "dropped_relevance_ids": list(dropped_relevance),
        "selection_policy": rubric.SELECTION_POLICY,
        "topk_truncated_ids": [c.cid for c in candidates
                               if c not in delivered and c.cid not in dropped_injection],
        "low_confidence_retained_ids": [c.cid for c in candidates
                                        if "rel:low_confidence_retained" in c.flags],
        "uncertain_retained_ids": [c.cid for c in candidates
                                   if "rel:uncertain_retained" in c.flags],
        "not_delivered_ids": [c.cid for c in candidates if c not in delivered],
    }


async def search(query: str, *, inventory: Sequence[SourceDescriptor],
                 adapters: Mapping[str, SourceAdapter], judge: Judge,
                 deadline_s: float = 15, count: int = 8,
                 sources: Sequence[str] | None = None, known_secret: str = "") -> Evidence:
    """Return source-grounded evidence; cancellation is explicit, never fail-open."""
    if count < 1 or deadline_s <= 0:
        raise ValueError("count and deadline_s must be positive")
    constraints = parse_constraints(query)
    excluded_by_source: dict[str, int] = {}
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + deadline_s
    diagnostics = JudgeDiagnostics(backend=judge.backend)
    metered = _MeteredJudge(judge, diagnostics)
    today = date.today().isoformat()
    chosen = Plan([], {}, "none")
    runs: list[SourceRun] = []
    delivered: list[Candidate] = []
    candidates: list[Candidate] = []
    outcome = None
    relevance = "unjudged"
    sufficiency = Sufficiency(None, None, None, "none")
    recovered = False
    cancelled = False

    async def judge_candidates(candidates, new_candidates, previous_answers, previous_ids):
        sensitive, injections = _screen(candidates, runs, known_secret)
        answers = dict(previous_answers)
        judged_ids = set(previous_ids)
        # A private provenance discovered during recovery invalidates an earlier
        # public judgment; it must never survive on the merged candidate.
        for c in candidates:
            if c.privacy_class != PrivacyClass.PUBLIC or c.cid in sensitive or c.cid in injections:
                judged_ids.discard(c.cid)
                answers.pop(f"rel.{c.cid}", None)
                answers.pop(f"inj.{c.cid}", None)
        def select():
            return rubric.select_policy(candidates, answers or None, judged_ids,
                                        local_injection_ids=injections, sensitive_ids=sensitive)

        if not may_judge:
            status = "skipped" if chosen.gate else "unjudged"
            return select(), answers, judged_ids, status, False
        public = [c for c in new_candidates if c.privacy_class == PrivacyClass.PUBLIC
                  and c.cid not in sensitive and c.cid not in injections][:rubric.MAX_CANDIDATES]
        if not public:
            return select(), answers, judged_ids, "judged" if judged_ids else "unjudged", True
        try:
            response = await metered.judge(rubric.candidate_state(query, today, public),
                                           rubric.candidate_questions(public), deadline=deadline)
        except JudgeError:
            # Recovery preserves earlier valid decisions, but never invents
            # judgments for new material after a failed batch.
            return select(), answers, judged_ids, "degraded", False
        answers.update(response.answers)
        judged_ids.update(c.cid for c in public)
        return select(), answers, judged_ids, "judged", True

    try:
        chosen = await build_plan(query, inventory, metered, deadline=deadline, sources=sources)
        if chosen.gate == "sensitive_query":
            # Terminate before execute, not merely before cloud judgments.
            evidence = Evidence(
                query, chosen, [], [],
                Quality("blocked", "skipped", sufficiency, 0),
                round(deadline_s * 1000), round((loop.time() - started) * 1000), diagnostics,
            )
            validate_evidence(evidence)
            return evidence
        # Exact bibliography and local queries are deterministic end-to-end.
        may_judge = (judge.backend != "none"
                     and chosen.gate not in {"bibliographic", "local"})
        runs = await execute(query, chosen, adapters, deadline=deadline, count=count, inventory=inventory)
        excluded_by_source.update(constraints.filter_runs(runs))
        if _execution_quality(runs) == "cancelled":
            cancelled = True
        else:
            candidates = fuse(runs, inventory)
            outcome, answers, judged_ids, relevance, safe = await judge_candidates(candidates, candidates, {}, set())
            delivered = outcome.kept[:count]
            next_answer = None
            all_rejected = bool(outcome.dropped_injection) and not outcome.kept
            if all_rejected:
                sufficiency = Sufficiency(0, "insufficient", None, "all_rejected")
            constraint_empty = bool(excluded_by_source) and not candidates
            if constraint_empty:
                sufficiency = Sufficiency(None, None, None, "site_filtered_empty")
            if may_judge and safe and not all_rejected and not constraint_empty:
                scope = _scope(candidates, delivered, outcome.dropped_injection, outcome.dropped_relevance)
                if constraints.site_hosts:
                    scope["source_constraint_excluded_count"] = sum(excluded_by_source.values())
                assessed = [c for c in delivered if c.judgment and "inj:suspect" not in c.flags]
                scope_label = "public_only" if any(scope[key] for key in
                    ("private_ids", "unjudged_ids", "suspect_ids")) else "delivered"
                try:
                    response = await metered.judge(rubric.delivered_state(query, today, assessed, scope),
                                                   rubric.delivered_questions(), deadline=deadline)
                    next_answer = response.answers["next_action"]
                    action = (next_answer.choice if (next_answer.confidence or 0) >= rubric.CHOICE_CONFIDENCE_MIN
                              else "unknown")
                    sufficiency = Sufficiency(response.answers["enough"].p, action,
                                              next_answer.confidence, scope_label)
                except JudgeError:
                    sufficiency = Sufficiency(None, None, None, scope_label)
            # Deterministic gates restrict the entire search, including recovery.
            untried = [s for s in inventory if s.routable and s.capabilities.search
                       and getattr(s, "executable", True) and s.id in adapters
                       and (s.kind != Kind.LOCAL or chosen.gate == "local")
                       and not chosen.reasons.get(s.id, "").startswith("rejected:")
                       and s.id not in chosen.sources] if chosen.gate is None and not all_rejected and safe else []
            def recovery_fit(source):
                reason = chosen.reasons.get(source.id, "")
                return float(reason.split(":", 1)[1]) if reason.startswith(("uncertain:", "not_selected:")) else 0
            untried.sort(key=lambda s: -recovery_fit(s))
            recovery = rubric.recovery_policy(sufficiency.enough, next_answer,
                budget_ok=deadline - loop.time() >= rubric.RECOVERY_MIN_BUDGET_S,
                untried_sources=len(untried))
            if recovery.recover:
                if recovery.prefer_fresh:
                    untried.sort(key=lambda s: not s.capabilities.fresh)
                extra = untried[:rubric.MAX_SOURCES]
                recovery_plan = Plan([s.id for s in extra], {s.id: f"recovery:{recovery.reason}" for s in extra},
                                     chosen.judge)
                new_runs = await execute(query, recovery_plan, adapters, deadline=deadline,
                                         count=count, inventory=inventory)
                excluded_by_source.update(constraints.filter_runs(new_runs))
                runs.extend(new_runs)
                recovered = True
                if _execution_quality(new_runs) == "cancelled":
                    cancelled = True
                else:
                    old = {canonical_url(c.url): c for c in candidates}
                    candidates = fuse(runs, inventory)
                    new = []
                    next_id = len(old)
                    for candidate in candidates:
                        previous = old.get(canonical_url(candidate.url))
                        if previous is not None:
                            candidate.cid = previous.cid
                        else:
                            candidate.cid = f"c{next_id}"
                            next_id += 1
                            new.append(candidate)
                    outcome, answers, judged_ids, new_relevance, _ = await judge_candidates(
                        candidates, new, answers, judged_ids)
                    relevance = "degraded" if relevance == "degraded" and new_relevance == "judged" else new_relevance
                    delivered = outcome.kept[:count]
                    newly_judged = any(c.cid in judged_ids for c in new) and new_relevance == "judged"
                    enough = outcome.raw_sufficiency if newly_judged else None
                    scope_label = "after_recovery:raw_scope" if newly_judged else "after_recovery:not_judged"
                    sufficiency = Sufficiency(enough, "unknown", None, scope_label)
    except asyncio.CancelledError:
        cancelled = True
    if cancelled:
        for run in runs:
            run.status = RunStatus.CANCELLED
            run.results = []
            run.error = None
        delivered = []
        relevance = "unjudged"
        sufficiency = Sufficiency(None, None, None, "none")
    descriptors = {s.id: s for s in inventory}
    families = {descriptors[c.source_id].upstream_family
                for candidate in delivered for c in candidate.contributions}
    families.discard("host-web:opaque")
    quality = Quality("cancelled" if cancelled else _execution_quality(runs), relevance,
                      sufficiency, len(families), recovered)
    evidence = Evidence(query, chosen, runs, delivered, quality, round(deadline_s * 1000),
                        round((loop.time() - started) * 1000), diagnostics)
    if outcome is not None and not cancelled:
        scope = _scope(candidates, delivered, outcome.dropped_injection, outcome.dropped_relevance)
        evidence.selection = {key: scope[key] for key in (
            "selection_policy", "dropped_injection_ids", "dropped_relevance_ids",
            "topk_truncated_ids", "low_confidence_retained_ids", "uncertain_retained_ids")}
    if constraints.site_hosts or constraints.warnings:
        evidence.constraints = {
            "site_hosts": list(constraints.site_hosts),
            "excluded_count": sum(excluded_by_source.values()),
            "excluded_by_source": excluded_by_source,
        }
        if constraints.warnings:
            evidence.constraints["warnings"] = list(constraints.warnings)
        if constraints.site_path_hints:
            evidence.constraints["site_path_hints"] = list(constraints.site_path_hints)
    validate_evidence(evidence)
    return evidence
