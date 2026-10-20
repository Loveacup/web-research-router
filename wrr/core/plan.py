"""Deterministic routing gates followed by one typed planning judgment."""
from __future__ import annotations

from datetime import date
import re
from typing import Sequence

from wrr.search_filter import _sensitive
from wrr.engines._academic_query import academic_query

from . import rubric
from .contracts import Kind, Plan, SourceDescriptor
from .judge import Judge, JudgeError, NoJudge

_BIBLIOGRAPHIC = re.compile(r"(?i)^(?:doi:|references:|citations:|10\.\d{4,9}/\S+)")


async def plan(query: str, inventory: Sequence[SourceDescriptor], judge: Judge, *,
               deadline: float, sources: Sequence[str] | None = None) -> Plan:
    routable = [s for s in inventory if s.routable and s.capabilities.search]
    if sources is not None:
        requested = list(dict.fromkeys(sources))
        available = {s.id for s in routable}
        missing = [sid for sid in requested if sid not in available]
        if missing:
            raise ValueError(f"sources not routable: {', '.join(missing)}")
        if len(requested) > rubric.MAX_SOURCES:
            raise ValueError(f"at most {rubric.MAX_SOURCES} explicit sources allowed")
        return Plan(requested, {sid: "explicit" for sid in requested},
                    "skipped:explicit_sources", gate="explicit_sources")
    stripped = query.strip()
    gate = ("bibliographic" if academic_query(stripped) or _BIBLIOGRAPHIC.match(stripped) else
            "local" if stripped.lower().startswith("local:") else None)
    if gate:
        kind = Kind.ACADEMIC if gate == "bibliographic" else Kind.LOCAL
        selected = [s.id for s in routable if s.kind == kind][:rubric.MAX_SOURCES]
        return Plan(selected, {sid: f"gate:{gate}" for sid in selected},
                    f"skipped:{gate}", gate=gate)
    if _sensitive(query):
        chosen = rubric.plan_policy(routable, None, "skipped:sensitive_query")
        chosen.gate = "sensitive_query"
        return chosen
    if isinstance(judge, NoJudge) or judge.backend == "none":
        return rubric.plan_policy(routable, None, "none")
    try:
        response = await judge.judge(
            rubric.plan_state(query, date.today().isoformat(), routable),
            rubric.plan_questions(routable), deadline=deadline,
        )
    except JudgeError as error:
        if error.reason == "cancelled":
            import asyncio
            raise asyncio.CancelledError from None
        return rubric.plan_policy(routable, None, "degraded")
    # `fit.*` nouls are independent of `evidence_shape` (TypeSafe answers do not
    # see each other), so a low-confidence shape only hides the shape label; it
    # must not discard valid per-source routing answers.
    shape = response.answers.get("evidence_shape")
    if shape is None or (shape.confidence or 0) < rubric.CHOICE_CONFIDENCE_MIN:
        answers = {k: v for k, v in response.answers.items() if k != "evidence_shape"}
        return rubric.plan_policy(routable, answers, f"{response.backend}:shape_low_confidence")
    return rubric.plan_policy(routable, response.answers, response.backend)
