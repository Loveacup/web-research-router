"""Stage-S offline evidence-gate contract tests.

The evaluator is pure: bytes in, privacy-bounded deterministic report out.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json

import pytest

import wrr.evidence_gate as evidence_gate
from wrr.evidence_gate import evaluate_jsonl
from wrr.schemas import DecisionEvidence


def _valid_e0_row() -> dict:
    return {
        "actual_provider": "rrf:grounding",
        "mode": "grounding",
        "outcome": "success",
        "quality_verdict": "complete",
        "recorded_at": "2026-08-19T00:00:00.000000Z",
        "request_key": "00000000-0000-4000-8000-000000000001",
        "result_count": 3,
        "route_elapsed_ms": 12.5,
        "schema_version": 1,
        "shadow_comparison": {
            "added_provider_ids": [],
            "code": "E0",
            "config_fingerprint": "cfg-v1",
            "context_snapshot_version": "ctx-v1",
            "descriptor_provider_ids": ["brave", "exa"],
            "legacy_provider_ids": ["brave", "exa"],
            "omitted_provider_ids": [],
            "reasons": [],
            "safe": True,
        },
        "stage": "S",
        "terminal": "routed",
    }


def _line(row: dict) -> bytes:
    return (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")


def _e0_lines(count: int, *, observation_seconds: int = 86400) -> list[bytes]:
    start = datetime(2026, 8, 19)
    lines = []
    for index in range(count):
        row = _valid_e0_row()
        row["request_key"] = f"00000000-0000-4000-8000-{index + 1:012d}"
        offset = observation_seconds if index == count - 1 and count > 1 else 0
        row["recorded_at"] = (start + timedelta(seconds=offset)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        lines.append(_line(row))
    return lines


def _set_shadow_code(row: dict, code: str) -> None:
    shadow = row["shadow_comparison"]
    if code in {"E1", "U1"}:
        shadow.update({
            "code": code,
            "safe": code == "E1",
            "descriptor_provider_ids": ["brave"],
            "omitted_provider_ids": ["exa"],
            "added_provider_ids": [],
            "reasons": ["exa:descriptor_blocked" if code == "E1" else "unexplained_omission"],
        })
    elif code == "U2":
        shadow.update({
            "code": "U2",
            "safe": False,
            "descriptor_provider_ids": ["brave", "exa", "skill"],
            "omitted_provider_ids": [],
            "added_provider_ids": ["skill"],
            "reasons": ["unsafe_addition"],
        })
    elif code == "U3":
        shadow.update({"code": "U3", "safe": False, "reasons": ["nondeterministic_selection"]})
    elif code == "U4":
        shadow.update({"code": "U4", "safe": False, "reasons": ["fallback_unprotected"]})
    elif code == "E3":
        shadow.update({"code": "E3", "safe": True, "reasons": ["policy_v1"]})


def test_valid_e0_row_is_counted_once_without_exposing_request_key():
    row = _valid_e0_row()

    report = evaluate_jsonl([_line(row)], requested_modes=("grounding",)).to_dict()

    assert report["input"] == {
        "event_rows": 1,
        "valid_rows": 1,
        "invalid_rows": 0,
        "invalid_rows_by_reason": {},
        "unscoped_noncomparable_rows": 0,
    }
    assert report["requested_modes"] == ["grounding"]
    assert report["modes"][0]["mode"] == "grounding"
    assert report["modes"][0]["comparable_rows"] == 1
    assert report["modes"][0]["cohorts"][0]["codes"]["E0"] == 1
    assert report["modes"][0]["selection_status"] == "NOT_READY"
    assert report["modes"][0]["status"] == "NOT_READY"
    assert row["request_key"] not in json.dumps(report, sort_keys=True)


def test_forbidden_query_field_is_unknown_fields_without_echoing_value():
    row = _valid_e0_row()
    row["query"] = "private search text"

    report = evaluate_jsonl([_line(row)], requested_modes=("grounding",)).to_dict()
    rendered = json.dumps(report, sort_keys=True)

    assert report["input"]["valid_rows"] == 0
    assert report["input"]["invalid_rows_by_reason"] == {"UNKNOWN_FIELDS": 1}
    assert "private search text" not in rendered
    assert row["request_key"] not in rendered


def test_invalid_utf8_is_a_data_free_row_error():
    report = evaluate_jsonl([b'{"query":"\xff"}\n'], requested_modes=("grounding",)).to_dict()

    assert report["input"]["valid_rows"] == 0
    assert report["input"]["invalid_rows_by_reason"] == {"INVALID_UTF8": 1}


def test_duplicate_json_object_key_is_invalid_json():
    text = json.dumps(_valid_e0_row(), sort_keys=True)
    text = text.replace(
        '"mode": "grounding",',
        '"mode": "research", "mode": "grounding",',
        1,
    )

    report = evaluate_jsonl(
        [(text + "\n").encode()], requested_modes=("grounding",)
    ).to_dict()

    assert report["input"]["invalid_rows_by_reason"] == {"INVALID_JSON": 1}


def test_duplicate_request_key_invalidates_every_copy_without_counting_samples():
    row = _valid_e0_row()

    report = evaluate_jsonl(
        [_line(row), _line(row)], requested_modes=("grounding",)
    ).to_dict()

    assert report["input"]["valid_rows"] == 0
    assert report["input"]["invalid_rows"] == 2
    assert report["input"]["invalid_rows_by_reason"] == {
        "DUPLICATE_REQUEST_KEY": 2,
    }
    assert report["modes"][0]["comparable_rows"] == 0


def test_duplicate_request_key_is_case_insensitive_after_uuid_canonicalization():
    lower = _valid_e0_row()
    upper = _valid_e0_row()
    key = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    lower["request_key"] = key
    upper["request_key"] = key.upper()

    report = evaluate_jsonl(
        [_line(lower), _line(upper)], requested_modes=("grounding",)
    ).to_dict()

    assert report["input"]["valid_rows"] == 0
    assert report["input"]["invalid_rows_by_reason"] == {
        "DUPLICATE_REQUEST_KEY": 2,
    }
    assert report["modes"][0]["comparable_rows"] == 0


def test_e0_with_different_provider_order_is_invalid_shadow_semantics():
    row = _valid_e0_row()
    row["shadow_comparison"]["descriptor_provider_ids"] = ["exa", "brave"]

    report = evaluate_jsonl([_line(row)], requested_modes=("grounding",)).to_dict()

    assert report["input"]["valid_rows"] == 0
    assert report["input"]["invalid_rows_by_reason"] == {
        "INVALID_SHADOW_SEMANTICS": 1,
    }
    assert report["modes"][0]["comparable_rows"] == 0


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda row: row.pop("terminal"), "MISSING_FIELDS"),
        (lambda row: row.__setitem__("schema_version", 2), "UNSUPPORTED_SCHEMA_VERSION"),
        (lambda row: row.__setitem__("stage", "C"), "INVALID_STAGE"),
        (lambda row: row.__setitem__("request_key", "not-a-uuid"), "INVALID_REQUEST_KEY"),
        (lambda row: row.__setitem__("recorded_at", "20260819T000000Z"), "INVALID_RECORDED_AT"),
        (lambda row: row.__setitem__("mode", "unknown"), "INVALID_MODE"),
        (lambda row: row.__setitem__("terminal", "unknown"), "INVALID_TERMINAL"),
        (lambda row: row.__setitem__("outcome", "unknown"), "INVALID_OUTCOME"),
        (lambda row: row.__setitem__("actual_provider", "bad provider"), "INVALID_ACTUAL_PROVIDER"),
        (lambda row: row.__setitem__("result_count", True), "INVALID_RESULT_COUNT"),
        (lambda row: row.__setitem__("quality_verdict", "unknown"), "INVALID_QUALITY_VERDICT"),
        (lambda row: row.__setitem__("route_elapsed_ms", float("inf")), "INVALID_ROUTE_ELAPSED_MS"),
        (lambda row: row.__setitem__("shadow_comparison", []), "INVALID_SHADOW_TYPE"),
        (lambda row: row["shadow_comparison"].pop("safe"), "INVALID_SHADOW_FIELDS"),
        (lambda row: row["shadow_comparison"].__setitem__("code", "E9"), "INVALID_SHADOW_CODE"),
        (lambda row: row["shadow_comparison"].__setitem__("safe", "yes"), "INVALID_SHADOW_SAFE"),
        (lambda row: row["shadow_comparison"].__setitem__("legacy_provider_ids", "exa"), "INVALID_SHADOW_PROVIDER_LIST"),
        (lambda row: row["shadow_comparison"].__setitem__("reasons", "reason"), "INVALID_SHADOW_REASONS"),
        (lambda row: row["shadow_comparison"].__setitem__("config_fingerprint", "bad fingerprint"), "INVALID_SHADOW_FINGERPRINT"),
    ],
)
def test_invalid_contract_fields_use_fixed_primary_reason(mutate, reason):
    row = _valid_e0_row()
    mutate(row)

    report = evaluate_jsonl([_line(row)], requested_modes=("grounding",)).to_dict()

    assert report["input"]["invalid_rows_by_reason"] == {reason: 1}


@pytest.mark.parametrize(
    ("count", "selection_status"),
    [(49, "NOT_READY"), (50, "READY"), (51, "READY")],
)
def test_selection_sample_threshold_is_50_per_cohort(count, selection_status):
    report = evaluate_jsonl(
        _e0_lines(count), requested_modes=("grounding",)
    ).to_dict()

    mode = report["modes"][0]
    assert mode["comparable_rows"] == count
    assert mode["selection_status"] == selection_status
    assert mode["status"] == "NOT_READY"
    assert mode["execution_protection_observable"] is False


def test_comparison_unavailable_is_not_a_sample_and_blocks_selection():
    row = _valid_e0_row()
    row["shadow_comparison"] = None

    report = evaluate_jsonl([_line(row)], requested_modes=("grounding",)).to_dict()
    mode = report["modes"][0]

    assert mode["valid_rows"] == 1
    assert mode["comparable_rows"] == 0
    assert mode["comparison_unavailable_rows"] == 1
    assert mode["context_build_failure_count"] is None
    assert "CONTEXT_FAILURE_UNOBSERVABLE_V1" in mode["reasons"]
    assert mode["selection_status"] == "NOT_READY"


def test_mode_null_is_unscoped_and_blocks_requested_mode():
    row = _valid_e0_row()
    row["mode"] = None
    row["shadow_comparison"] = None

    report = evaluate_jsonl([_line(row)], requested_modes=("grounding",)).to_dict()

    assert report["input"]["unscoped_noncomparable_rows"] == 1
    assert report["selection_status"] == "NOT_READY"
    assert report["modes"][0]["selection_status"] == "NOT_READY"


@pytest.mark.parametrize(
    ("unsafe_codes", "selection_status", "reason"),
    [
        (["U1"], "READY", None),
        (["U1", "U1"], "NOT_READY", "U1_LIMIT_EXCEEDED"),
        (["U2"], "NOT_READY", "U2_PRESENT"),
        (["U3"], "NOT_READY", "U3_PRESENT"),
        (["U4"], "NOT_READY", "U4_PRESENT"),
        (["E3"], "NOT_READY", "E3_POLICY_UNVERIFIABLE_V1"),
    ],
)
def test_selection_code_thresholds_are_fail_closed(unsafe_codes, selection_status, reason):
    rows = []
    for index, raw in enumerate(_e0_lines(50)):
        row = json.loads(raw)
        if index < len(unsafe_codes):
            _set_shadow_code(row, unsafe_codes[index])
        rows.append(_line(row))

    report = evaluate_jsonl(rows, requested_modes=("grounding",)).to_dict()
    mode = report["modes"][0]

    assert mode["selection_status"] == selection_status
    if reason is not None:
        assert reason in mode["reasons"]


def test_metrics_and_outcome_counts_are_deterministic_nearest_rank():
    rows = []
    for index, raw in enumerate(_e0_lines(50)):
        row = json.loads(raw)
        row["route_elapsed_ms"] = index
        if index == 0:
            row["quality_verdict"] = "degraded_success"
        rows.append(_line(row))

    cohort = evaluate_jsonl(
        reversed(rows), requested_modes=("grounding",)
    ).to_dict()["modes"][0]["cohorts"][0]

    assert cohort["route_elapsed_ms"] == {
        "minimum": 0.0,
        "maximum": 49.0,
        "p50": 24.0,
        "p95": 47.0,
    }
    assert cohort["quality_verdict_counts"] == {
        "complete": 49,
        "degraded_success": 1,
    }


def test_multiple_fingerprint_cohorts_are_not_merged():
    rows = _e0_lines(50)
    second = json.loads(_e0_lines(1)[0])
    second["request_key"] = "00000000-0000-4000-8000-000000000999"
    second["shadow_comparison"]["config_fingerprint"] = "cfg-v2"
    rows.append(_line(second))

    mode = evaluate_jsonl(rows, requested_modes=("grounding",)).to_dict()["modes"][0]

    assert len(mode["cohorts"]) == 2
    assert [cohort["sample_count"] for cohort in mode["cohorts"]] == [50, 1]
    assert "MULTIPLE_COHORTS" in mode["reasons"]
    assert mode["selection_status"] == "NOT_READY"


def test_any_invalid_row_blocks_each_requested_mode():
    rows = _e0_lines(50) + [b"not-json\n"]

    report = evaluate_jsonl(rows, requested_modes=("grounding",)).to_dict()
    mode = report["modes"][0]

    assert report["input"]["invalid_rows_by_reason"] == {"INVALID_JSON": 1}
    assert "INVALID_ROWS_PRESENT" in mode["reasons"]
    assert mode["selection_status"] == "NOT_READY"


def test_current_emitter_without_shadow_is_accepted_as_noncomparable():
    evidence = DecisionEvidence(
        request_key="00000000-0000-4000-8000-000000000777",
        recorded_at="2026-08-19T00:00:00.000000Z",
        mode="grounding",
        terminal="routed",
        outcome="success",
        actual_provider="rrf:grounding",
        result_count=2,
        quality_verdict="complete",
        route_elapsed_ms=3.5,
        shadow_comparison=None,
    )

    report = evaluate_jsonl(
        [_line(evidence.to_dict())], requested_modes=("grounding",)
    ).to_dict()

    assert report["input"]["valid_rows"] == 1
    assert report["input"]["invalid_rows"] == 0
    assert report["modes"][0]["noncomparable_rows"] == 1


def test_quality_verdict_precedes_elapsed_in_primary_reason_order():
    row = _valid_e0_row()
    row["quality_verdict"] = "unknown"
    row["route_elapsed_ms"] = float("inf")

    report = evaluate_jsonl([_line(row)], requested_modes=("grounding",)).to_dict()

    assert report["input"]["invalid_rows_by_reason"] == {
        "INVALID_QUALITY_VERDICT": 1,
    }


def test_shadow_reasons_structure_precedes_duplicate_provider_semantics():
    row = _valid_e0_row()
    row["shadow_comparison"]["legacy_provider_ids"] = ["exa", "exa"]
    row["shadow_comparison"]["reasons"] = "not-a-list"

    report = evaluate_jsonl([_line(row)], requested_modes=("grounding",)).to_dict()

    assert report["input"]["invalid_rows_by_reason"] == {
        "INVALID_SHADOW_REASONS": 1,
    }


def test_nullable_terminal_and_quality_are_counted_as_none_tokens():
    rows = []
    for index, raw in enumerate(_e0_lines(50)):
        row = json.loads(raw)
        if index == 0:
            row["terminal"] = None
            row["quality_verdict"] = None
        rows.append(_line(row))

    cohort = evaluate_jsonl(
        rows, requested_modes=("grounding",)
    ).to_dict()["modes"][0]["cohorts"][0]

    assert cohort["terminal_counts"] == {"none": 1, "routed": 49}
    assert cohort["quality_verdict_counts"] == {"complete": 49, "none": 1}


def test_unexpected_evaluator_error_is_not_mislabeled_invalid_json(monkeypatch):
    monkeypatch.setattr(
        evidence_gate,
        "_parse_row",
        lambda _raw: (_ for _ in ()).throw(RuntimeError("programming defect")),
    )

    with pytest.raises(RuntimeError, match="programming defect"):
        evaluate_jsonl([_line(_valid_e0_row())], requested_modes=("grounding",))


def test_line_and_shadow_list_limits_fail_closed():
    with pytest.raises(evidence_gate.EvidenceInputLimitError):
        evaluate_jsonl(
            [b"x" * (evidence_gate.MAX_LINE_BYTES + 1)],
            requested_modes=("grounding",),
        )

    row = _valid_e0_row()
    providers = [f"p{index}" for index in range(evidence_gate.MAX_LIST_ITEMS + 1)]
    row["shadow_comparison"].update({
        "legacy_provider_ids": providers,
        "descriptor_provider_ids": providers,
    })
    report = evaluate_jsonl([_line(row)], requested_modes=("grounding",)).to_dict()
    assert report["input"]["invalid_rows_by_reason"] == {
        "INVALID_SHADOW_PROVIDER_LIST": 1,
    }


def test_gate_report_to_dict_is_a_defensive_copy():
    report = evaluate_jsonl(
        [_line(_valid_e0_row())], requested_modes=("grounding",)
    )

    first = report.to_dict()
    first["status"] = "READY"

    assert report.to_dict()["status"] == "NOT_READY"
