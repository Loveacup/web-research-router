"""EV-D5 fixed-campaign pure-evaluator contract tests.

``evaluate_campaign`` is a pure projection: a ``CampaignFacts``-like object plus
the ledger's ``export()`` rows in, a privacy-bounded deterministic report out.
It never performs I/O, never reaches READY, and never authorizes an automatic
Stage C — the strongest verdict it can return is manual C5 review eligibility.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest

import wrr.evidence_gate as evidence_gate
from wrr.evidence_gate import EvidenceInputLimitError, GateReport, evaluate_campaign

# Real durable projection objects: the evaluator must accept them structurally
# without importing the runtime/schema/config layer itself.
from wrr.runtime.campaign_ledger import CampaignDeclaration, CampaignFacts


POLICY = "EV-D5-v1"
COHORT = "11111111-1111-4111-8111-111111111111"
BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ── builders ────────────────────────────────────────────────────────────


def _key() -> str:
    return str(uuid.uuid4())


def _ts(index: int, *, step_seconds: int = 3600) -> str:
    return (BASE + timedelta(seconds=index * step_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _shadow(code: str) -> dict:
    table = {
        "E0": (2, 2, 0, 0, True),
        "U1": (2, 1, 1, 0, False),
        "U2": (1, 2, 0, 1, False),
        "U3": (2, 2, 0, 0, False),
    }
    legacy, descriptor, omitted, added, safe = table[code]
    return {
        "code": code,
        "safe": safe,
        "reasons_complete": code in {"E0"},
        "legacy_provider_count": legacy,
        "descriptor_provider_count": descriptor,
        "omitted_provider_count": omitted,
        "added_provider_count": added,
    }


def _compared_row(
    request_key: str,
    mode: str,
    recorded_at: str,
    *,
    code: str = "E0",
    cohort: str = COHORT,
    context_status: str = "available",
    comparison_status: str = "compared",
) -> dict:
    shadow = _shadow(code)
    return {
        "schema_version": 2,
        "request_key": request_key,
        "recorded_at": recorded_at,
        "stage": "S",
        "mode": mode,
        "terminal": "routed",
        "outcome": "success",
        "result_count": 3,
        "quality_verdict": "complete",
        "route_elapsed_ms": 120,
        "context_status": context_status,
        "comparison_status": comparison_status,
        "execution_protection": "not_required",
        "context_cohort_id": cohort,
        "shadow_comparison": shadow,
    }


def _effective_u4_row(request_key: str, mode: str, recorded_at: str) -> dict:
    # E1 shadow with an empty descriptor + empty outcome ⇒ unprotected_empty,
    # i.e. an effective-U4 execution with no legacy protection observed. The E1
    # code keeps it out of the U1/U2/U3 code tallies so U4 can be tested alone.
    return {
        "schema_version": 2,
        "request_key": request_key,
        "recorded_at": recorded_at,
        "stage": "S",
        "mode": mode,
        "terminal": "all_engines_failed",
        "outcome": "empty",
        "result_count": 0,
        "quality_verdict": "insufficient",
        "route_elapsed_ms": 55,
        "context_status": "available",
        "comparison_status": "compared",
        "execution_protection": "unprotected_empty",
        "context_cohort_id": COHORT,
        "shadow_comparison": {
            "code": "E1",
            "safe": True,
            "reasons_complete": True,
            "legacy_provider_count": 1,
            "descriptor_provider_count": 0,
            "omitted_provider_count": 1,
            "added_provider_count": 0,
        },
    }


def _refresh_failed_row(request_key: str, mode: str, recorded_at: str) -> dict:
    # A structurally valid v2 row whose context never became available.
    return {
        "schema_version": 2,
        "request_key": request_key,
        "recorded_at": recorded_at,
        "stage": "S",
        "mode": mode,
        "terminal": "routed",
        "outcome": "error",
        "result_count": 0,
        "quality_verdict": "failed",
        "route_elapsed_ms": 10,
        "context_status": "refresh_failed",
        "comparison_status": "context_expired",
        "execution_protection": "unobservable",
        "context_cohort_id": COHORT,
        "shadow_comparison": None,
    }


def _comparison_unavailable_row(request_key: str, mode: str, recorded_at: str) -> dict:
    # Context is available but the comparison itself never completed.
    return {
        "schema_version": 2,
        "request_key": request_key,
        "recorded_at": recorded_at,
        "stage": "S",
        "mode": mode,
        "terminal": "routed",
        "outcome": "error",
        "result_count": 0,
        "quality_verdict": "failed",
        "route_elapsed_ms": 10,
        "context_status": "available",
        "comparison_status": "context_expired",
        "execution_protection": "unobservable",
        "context_cohort_id": COHORT,
        "shadow_comparison": None,
    }


def _v1_row(request_key: str, mode: str = "academic") -> dict:
    return {
        "schema_version": 1,
        "request_key": request_key,
        "recorded_at": "2026-01-01T00:00:00Z",
        "stage": "S",
        "mode": mode,
        "terminal": "routed",
        "outcome": "success",
        "actual_provider": "brave",
        "result_count": 2,
        "quality_verdict": "complete",
        "route_elapsed_ms": 100,
        "shadow_comparison": None,
    }


def _declaration(modes=("academic",), *, policy=POLICY, cohort=COHORT):
    return CampaignDeclaration(
        policy_version=policy,
        requested_modes=tuple(modes),
        accepted_context_cohort_id=cohort,
    )


def _facts(capacity, request_keys, declaration, **overrides) -> CampaignFacts:
    base = dict(
        campaign_id="camp-1",
        epoch="epoch-1",
        status="clean",
        capacity=capacity,
        declaration=declaration,
        opened_at="2026-01-01T00:00:00.000000Z",
        closed_at="2026-01-03T00:00:00.000000Z",
        start_sequence=1,
        end_sequence=capacity,
        attempts_started=capacity,
        terminal_count=capacity,
        persisted_evidence_count=capacity,
        dropped_count=0,
        unresolved_count=0,
        duplicate_count=0,
        sequence_gaps=0,
        fault_count=0,
        session_closed_cleanly=True,
        request_keys=tuple(request_keys),
    )
    base.update(overrides)
    return CampaignFacts(**base)


def _admissions(rows):
    return [
        {"seq": i + 1, "request_key": row["request_key"], "evidence": row}
        for i, row in enumerate(rows)
    ]


def _campaign(rows, declaration=None, **facts_overrides):
    declaration = declaration if declaration is not None else _declaration()
    keys = [row["request_key"] for row in rows]
    capacity = facts_overrides.pop("capacity", len(rows))
    facts = _facts(capacity, keys, declaration, **facts_overrides)
    return facts, _admissions(rows)


def _single_mode_rows(n=50, mode="academic", *, code="E0", step_seconds=3600):
    return [
        _compared_row(_key(), mode, _ts(i, step_seconds=step_seconds), code=code)
        for i in range(n)
    ]


# ── happy path ──────────────────────────────────────────────────────────


def test_happy_50_over_24h_is_eligible_for_manual_review():
    rows = _single_mode_rows(50)
    facts, admissions = _campaign(rows)
    report = evaluate_campaign(facts, admissions)
    assert isinstance(report, GateReport)
    payload = report.to_dict()
    assert payload["status"] == "ELIGIBLE_FOR_MANUAL_C5_REVIEW"
    assert payload["automatic_action"] is False
    assert payload["reasons"] == []
    assert payload["policy_version"] == POLICY
    assert payload["requested_modes"] == ["academic"]
    mode = payload["modes"][0]
    assert mode["mode"] == "academic"
    assert mode["status"] == "READY"
    assert mode["comparable_rows"] == 50
    assert mode["observation_seconds"] >= 86400


def test_verdict_is_never_ready_and_never_auto_stage_c():
    rows = _single_mode_rows(50)
    payload = evaluate_campaign(*_campaign(rows)).to_dict()
    assert payload["status"] != "READY"
    assert payload["automatic_action"] is False


# ── campaign-level gates ────────────────────────────────────────────────


def test_undeclared_campaign_is_not_ready():
    rows = _single_mode_rows(50)
    keys = [row["request_key"] for row in rows]
    facts = _facts(50, keys, None)
    payload = evaluate_campaign(facts, _admissions(rows)).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "CAMPAIGN_NOT_DECLARED" in payload["reasons"]


def test_unsupported_policy_version_is_not_ready():
    rows = _single_mode_rows(50)
    facts, admissions = _campaign(rows, declaration=_declaration(policy="EV-D4-v9"))
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "POLICY_VERSION_UNSUPPORTED" in payload["reasons"]


def test_capacity_must_be_declared_positive():
    rows = _single_mode_rows(50)
    facts, admissions = _campaign(rows, capacity=None)
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "CAMPAIGN_CAPACITY_UNDECLARED" in payload["reasons"]


def test_campaign_must_close_clean():
    rows = _single_mode_rows(50)
    facts, admissions = _campaign(
        rows, status="dirty", session_closed_cleanly=False
    )
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "CAMPAIGN_NOT_CLEAN" in payload["reasons"]


def test_coverage_count_mismatch_is_incomplete():
    rows = _single_mode_rows(50)
    facts, admissions = _campaign(rows, persisted_evidence_count=49)
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "CAMPAIGN_COVERAGE_INCOMPLETE" in payload["reasons"]


def test_coverage_range_mismatch_is_incomplete():
    rows = _single_mode_rows(50)
    facts, admissions = _campaign(rows, start_sequence=2)
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "CAMPAIGN_COVERAGE_INCOMPLETE" in payload["reasons"]


def test_coverage_gap_or_fault_is_incomplete():
    rows = _single_mode_rows(50)
    facts, admissions = _campaign(rows, sequence_gaps=1, fault_count=2)
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "CAMPAIGN_COVERAGE_INCOMPLETE" in payload["reasons"]


# ── admission join gates ────────────────────────────────────────────────


def test_missing_admission_row_breaks_join():
    rows = _single_mode_rows(50)
    keys = [row["request_key"] for row in rows]
    facts = _facts(50, keys, _declaration())
    payload = evaluate_campaign(facts, _admissions(rows[:49])).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "ADMISSION_JOIN_MISMATCH" in payload["reasons"]


def test_extra_admission_row_breaks_join():
    rows = _single_mode_rows(50)
    keys = [row["request_key"] for row in rows]
    facts = _facts(50, keys, _declaration())
    extra = rows + [_compared_row(_key(), "academic", _ts(60))]
    payload = evaluate_campaign(facts, _admissions(extra)).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "ADMISSION_JOIN_MISMATCH" in payload["reasons"]


def test_duplicate_request_key_breaks_join():
    rows = _single_mode_rows(50)
    admissions = _admissions(rows)
    # Overwrite the second admission's request_key with the first's.
    admissions[1]["request_key"] = admissions[0]["request_key"]
    keys = [row["request_key"] for row in rows]
    facts = _facts(50, keys, _declaration())
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "ADMISSION_JOIN_MISMATCH" in payload["reasons"]


def test_evidence_request_key_mismatch_breaks_join():
    rows = _single_mode_rows(50)
    keys = [row["request_key"] for row in rows]
    admissions = _admissions(rows)
    # The admission and facts agree on the key, but the persisted evidence in
    # the first row names a different request_key than it was admitted under.
    rows[0]["request_key"] = _key()
    facts = _facts(50, keys, _declaration())
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "ADMISSION_JOIN_MISMATCH" in payload["reasons"]


def test_seq_discontinuity_breaks_join():
    rows = _single_mode_rows(50)
    admissions = _admissions(rows)
    admissions[10]["seq"] = 999
    keys = [row["request_key"] for row in rows]
    facts = _facts(50, keys, _declaration())
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "ADMISSION_JOIN_MISMATCH" in payload["reasons"]


def test_swapped_sequence_key_bindings_break_join():
    rows = _single_mode_rows(50)
    facts, admissions = _campaign(rows)
    admissions[0]["seq"], admissions[1]["seq"] = (
        admissions[1]["seq"],
        admissions[0]["seq"],
    )
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "ADMISSION_JOIN_MISMATCH" in payload["reasons"]


def test_facts_key_count_must_match_declared_capacity():
    rows = _single_mode_rows(50)
    keys = [row["request_key"] for row in rows]
    facts = _facts(51, keys, _declaration())
    payload = evaluate_campaign(facts, _admissions(rows)).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "ADMISSION_JOIN_MISMATCH" in payload["reasons"]


def test_duplicate_key_in_facts_and_evidence_still_breaks_join():
    rows = _single_mode_rows(50)
    rows[1]["request_key"] = rows[0]["request_key"]
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "ADMISSION_JOIN_MISMATCH" in payload["reasons"]


# ── evidence schema gates ───────────────────────────────────────────────


def test_v1_evidence_row_is_rejected():
    rows = _single_mode_rows(49)
    rows.append(_v1_row(_key()))
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "INVALID_EVIDENCE_ROWS_PRESENT" in payload["reasons"]


def test_malformed_evidence_row_is_rejected():
    rows = _single_mode_rows(49)
    rows.append({"schema_version": 2, "request_key": _key(), "totally": "broken"})
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "INVALID_EVIDENCE_ROWS_PRESENT" in payload["reasons"]


# ── row-scope contract gates ────────────────────────────────────────────


def test_undeclared_mode_row_is_rejected():
    rows = _single_mode_rows(49)
    rows.append(_compared_row(_key(), "broad", _ts(60)))
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "UNDECLARED_MODE_PRESENT" in payload["reasons"]


def test_cohort_mismatch_row_is_rejected():
    other = "22222222-2222-4222-8222-222222222222"
    rows = _single_mode_rows(49)
    rows.append(_compared_row(_key(), "academic", _ts(60), cohort=other))
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "COHORT_MISMATCH" in payload["reasons"]


def test_context_unavailable_row_is_rejected():
    rows = _single_mode_rows(49)
    rows.append(_refresh_failed_row(_key(), "academic", _ts(60)))
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "CONTEXT_UNAVAILABLE" in payload["reasons"]


def test_comparison_unavailable_row_is_rejected():
    rows = _single_mode_rows(49)
    rows.append(_comparison_unavailable_row(_key(), "academic", _ts(60)))
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    assert payload["status"] == "NOT_READY"
    assert "COMPARISON_UNAVAILABLE" in payload["reasons"]


# ── per-mode threshold isolation ────────────────────────────────────────


def test_modes_are_evaluated_in_isolation():
    academic = _single_mode_rows(50, "academic")
    broad = [_compared_row(_key(), "broad", _ts(i)) for i in range(10)]
    rows = academic + broad
    facts, admissions = _campaign(
        rows, declaration=_declaration(("academic", "broad"))
    )
    payload = evaluate_campaign(facts, admissions).to_dict()
    by_mode = {mode["mode"]: mode for mode in payload["modes"]}
    assert by_mode["academic"]["status"] == "READY"
    assert by_mode["broad"]["status"] == "NOT_READY"
    assert "INSUFFICIENT_SAMPLES" in by_mode["broad"]["reasons"]
    assert payload["status"] == "NOT_READY"


def test_insufficient_observation_window_blocks_mode():
    rows = _single_mode_rows(50, step_seconds=1)  # 50 rows within 49 seconds
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    mode = payload["modes"][0]
    assert mode["status"] == "NOT_READY"
    assert "INSUFFICIENT_OBSERVATION" in mode["reasons"]


def test_u1_over_limit_blocks_mode():
    rows = _single_mode_rows(48, code="E0")
    rows.append(_compared_row(_key(), "academic", _ts(48), code="U1"))
    rows.append(_compared_row(_key(), "academic", _ts(49), code="U1"))
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    mode = payload["modes"][0]
    assert mode["status"] == "NOT_READY"
    assert "U1_LIMIT_EXCEEDED" in mode["reasons"]


def test_single_u1_within_ratio_is_allowed():
    rows = _single_mode_rows(49, code="E0")
    rows.append(_compared_row(_key(), "academic", _ts(49), code="U1"))
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    mode = payload["modes"][0]
    assert mode["status"] == "READY"
    assert payload["status"] == "ELIGIBLE_FOR_MANUAL_C5_REVIEW"


def test_u2_presence_blocks_mode():
    rows = _single_mode_rows(49, code="E0")
    rows.append(_compared_row(_key(), "academic", _ts(49), code="U2"))
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    mode = payload["modes"][0]
    assert mode["status"] == "NOT_READY"
    assert "U2_PRESENT" in mode["reasons"]


def test_u3_presence_blocks_mode():
    rows = _single_mode_rows(49, code="E0")
    rows.append(_compared_row(_key(), "academic", _ts(49), code="U3"))
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    mode = payload["modes"][0]
    assert mode["status"] == "NOT_READY"
    assert "U3_PRESENT" in mode["reasons"]


def test_effective_u4_presence_blocks_mode():
    rows = _single_mode_rows(49, code="E0")
    rows.append(_effective_u4_row(_key(), "academic", _ts(49)))
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    mode = payload["modes"][0]
    assert mode["status"] == "NOT_READY"
    assert "U4_PRESENT" in mode["reasons"]
    assert mode["effective_u4_rows"] == 1


# ── privacy, determinism, input bounds ──────────────────────────────────


def test_report_never_exposes_raw_identifiers_or_timestamps():
    rows = _single_mode_rows(50)
    facts, admissions = _campaign(rows)
    payload = evaluate_campaign(facts, admissions).to_dict()
    blob = json.dumps(payload)
    assert COHORT not in blob
    for row in rows:
        assert row["request_key"] not in blob
    # No raw RFC3339 timestamp value leaks into the report.
    assert "T00:00:00Z" not in blob


def test_evaluation_is_deterministic():
    rows = _single_mode_rows(50)
    facts, admissions = _campaign(rows)
    first = evaluate_campaign(facts, admissions).to_dict()
    second = evaluate_campaign(facts, admissions).to_dict()
    assert first == second


def test_admission_row_count_bound_is_enforced(monkeypatch):
    monkeypatch.setattr(evidence_gate, "MAX_ROWS", 3)
    rows = _single_mode_rows(4)
    facts, admissions = _campaign(rows)
    with pytest.raises(EvidenceInputLimitError):
        evaluate_campaign(facts, admissions)


def test_admission_line_size_bound_is_enforced(monkeypatch):
    monkeypatch.setattr(evidence_gate, "MAX_LINE_BYTES", 10)
    rows = _single_mode_rows(2)
    facts, admissions = _campaign(rows)
    with pytest.raises(EvidenceInputLimitError):
        evaluate_campaign(facts, admissions)
