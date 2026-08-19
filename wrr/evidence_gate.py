"""Pure offline evaluator for Stage-S decision-evidence JSONL.

This module owns the versioned wire decoder for persisted schema v1. It does
not import the live routing schema because that import reads runtime config.
Bytes in, deterministic privacy-bounded report out; no file, env, clock, or
network access.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
from math import ceil, isfinite
import re
from typing import Iterable, Sequence
import uuid


MODES = (
    "academic",
    "broad",
    "discovery",
    "grounding",
    "local",
    "platform",
    "recovery",
    "research",
)
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_ROWS = 100_000
MAX_LIST_ITEMS = 128
_CODES = ("E0", "E1", "E2", "E3", "U1", "U2", "U3", "U4")
_TERMINALS = {
    "routed", "explicit_provider", "recovery", "recovery_blocked",
    "all_engines_failed", "execution_error",
}
_OUTCOMES = {"success", "empty", "error"}
_VERDICTS = {"complete", "degraded_success", "insufficient", "failed"}
_TOP_FIELDS = {
    "request_key", "recorded_at", "schema_version", "stage", "mode",
    "terminal", "outcome", "actual_provider", "result_count",
    "quality_verdict", "route_elapsed_ms", "shadow_comparison",
}
_TOP_REQUIRED_FIELDS = _TOP_FIELDS - {"shadow_comparison"}
_SHADOW_FIELDS = {
    "code", "safe", "legacy_provider_ids", "descriptor_provider_ids",
    "omitted_provider_ids", "added_provider_ids", "reasons",
    "context_snapshot_version", "config_fingerprint",
}
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,256}\Z")
_PROVIDER_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
_RFC3339_Z = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?Z\Z"
)


@dataclass(frozen=True)
class GateReport:
    """Privacy-bounded deterministic report returned by the evaluator."""

    payload: dict

    def to_dict(self) -> dict:
        return deepcopy(self.payload)


class EvidenceInputLimitError(ValueError):
    """The explicit evidence input exceeded a deterministic resource bound."""


class _InvalidRow(ValueError):
    """Internal control flow carrying one fixed, data-free reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not _RFC3339_Z.match(value):
        raise _InvalidRow("INVALID_RECORDED_AT")
    try:
        return datetime.fromisoformat(value[:-1])
    except ValueError as exc:
        raise _InvalidRow("INVALID_RECORDED_AT") from exc


def _parse_uuid4(value: object) -> str:
    if not isinstance(value, str):
        raise _InvalidRow("INVALID_REQUEST_KEY")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise _InvalidRow("INVALID_REQUEST_KEY") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise _InvalidRow("INVALID_REQUEST_KEY")
    return str(parsed)


def _token_list(value: object, *, providers: bool) -> list[str]:
    pattern = _PROVIDER_TOKEN if providers else _TOKEN
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not pattern.match(item) for item in value
    ):
        reason = "INVALID_SHADOW_PROVIDER_LIST" if providers else "INVALID_SHADOW_REASONS"
        raise _InvalidRow(reason)
    if len(value) > MAX_LIST_ITEMS:
        reason = "INVALID_SHADOW_PROVIDER_LIST" if providers else "INVALID_SHADOW_REASONS"
        raise _InvalidRow(reason)
    return value


def _expected_difference(left: list[str], right: list[str]) -> list[str]:
    right_set = set(right)
    return [item for item in left if item not in right_set]


def _e1_reasons_complete(omitted: list[str], reasons: list[str]) -> bool:
    matched: set[str] = set()
    ordered = sorted(omitted, key=lambda item: (-len(item), item))
    for reason in reasons:
        owner = next(
            (provider for provider in ordered if reason.startswith(provider + ":")),
            None,
        )
        if owner is not None and len(reason) > len(owner) + 1:
            matched.add(owner)
    return matched == set(omitted)


def _validate_shadow_semantics(shadow: dict) -> None:
    legacy = shadow["legacy_provider_ids"]
    descriptor = shadow["descriptor_provider_ids"]
    omitted = shadow["omitted_provider_ids"]
    added = shadow["added_provider_ids"]
    reasons = shadow["reasons"]
    code = shadow["code"]

    if any(
        len(values) != len(set(values))
        for values in (legacy, descriptor, omitted, added)
    ):
        raise _InvalidRow("INVALID_SHADOW_SEMANTICS")

    if omitted != _expected_difference(legacy, descriptor):
        raise _InvalidRow("INVALID_SHADOW_SEMANTICS")
    if added != _expected_difference(descriptor, legacy):
        raise _InvalidRow("INVALID_SHADOW_SEMANTICS")
    if shadow["safe"] != code.startswith("E"):
        raise _InvalidRow("INVALID_SHADOW_SEMANTICS")

    if code == "E0":
        valid = legacy == descriptor and not omitted and not added and not reasons
    elif code == "E1":
        valid = (
            legacy != descriptor
            and set(descriptor) < set(legacy)
            and not added
            and bool(omitted)
            and _e1_reasons_complete(omitted, reasons)
        )
    elif code == "E2":
        valid = (
            legacy != descriptor
            and set(legacy) == set(descriptor)
            and not omitted
            and not added
        )
    elif code == "E3":
        valid = True
    elif code == "U1":
        valid = not added and bool(omitted)
    elif code == "U2":
        valid = bool(added)
    elif code == "U3":
        valid = "nondeterministic_selection" in reasons
    else:  # U4 is structurally valid but never full-gate ready in schema v1.
        valid = True
    if not valid:
        raise _InvalidRow("INVALID_SHADOW_SEMANTICS")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidRow("INVALID_JSON")
        result[key] = value
    return result


def _parse_row(raw: bytes) -> dict:
    if not raw.strip():
        raise _InvalidRow("EMPTY_LINE")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _InvalidRow("INVALID_UTF8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise _InvalidRow("INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise _InvalidRow("ROW_NOT_OBJECT")
    if set(value) - _TOP_FIELDS:
        raise _InvalidRow("UNKNOWN_FIELDS")
    if _TOP_REQUIRED_FIELDS - set(value):
        raise _InvalidRow("MISSING_FIELDS")
    value.setdefault("shadow_comparison", None)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise _InvalidRow("UNSUPPORTED_SCHEMA_VERSION")
    if type(value["stage"]) is not str or value["stage"] != "S":
        raise _InvalidRow("INVALID_STAGE")
    value["request_key"] = _parse_uuid4(value["request_key"])
    value["_recorded_dt"] = _parse_time(value["recorded_at"])
    if value["mode"] is not None and (
        not isinstance(value["mode"], str) or value["mode"] not in MODES
    ):
        raise _InvalidRow("INVALID_MODE")
    if value["terminal"] is not None and (
        not isinstance(value["terminal"], str)
        or value["terminal"] not in _TERMINALS
    ):
        raise _InvalidRow("INVALID_TERMINAL")
    if not isinstance(value["outcome"], str) or value["outcome"] not in _OUTCOMES:
        raise _InvalidRow("INVALID_OUTCOME")
    provider = value["actual_provider"]
    if provider is not None and (
        not isinstance(provider, str) or not _PROVIDER_TOKEN.match(provider)
    ):
        raise _InvalidRow("INVALID_ACTUAL_PROVIDER")
    if isinstance(value["result_count"], bool) or not isinstance(value["result_count"], int) or value["result_count"] < 0:
        raise _InvalidRow("INVALID_RESULT_COUNT")
    verdict = value["quality_verdict"]
    if verdict is not None and (
        not isinstance(verdict, str) or verdict not in _VERDICTS
    ):
        raise _InvalidRow("INVALID_QUALITY_VERDICT")
    elapsed = value["route_elapsed_ms"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not isfinite(elapsed) or elapsed < 0:
        raise _InvalidRow("INVALID_ROUTE_ELAPSED_MS")
    shadow = value["shadow_comparison"]
    if shadow is not None:
        if not isinstance(shadow, dict):
            raise _InvalidRow("INVALID_SHADOW_TYPE")
        if set(shadow) != _SHADOW_FIELDS:
            raise _InvalidRow("INVALID_SHADOW_FIELDS")
        if not isinstance(shadow["code"], str) or shadow["code"] not in _CODES:
            raise _InvalidRow("INVALID_SHADOW_CODE")
        if type(shadow["safe"]) is not bool:
            raise _InvalidRow("INVALID_SHADOW_SAFE")
        for field_name in (
            "legacy_provider_ids", "descriptor_provider_ids",
            "omitted_provider_ids", "added_provider_ids",
        ):
            shadow[field_name] = _token_list(shadow[field_name], providers=True)
        shadow["reasons"] = _token_list(shadow["reasons"], providers=False)
        for field_name in ("config_fingerprint", "context_snapshot_version"):
            token = shadow[field_name]
            if not isinstance(token, str) or (token and not _TOKEN.match(token)):
                raise _InvalidRow("INVALID_SHADOW_FINGERPRINT")
        _validate_shadow_semantics(shadow)
    return value


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, ceil(percentile * len(ordered)) - 1)]


def _stable_counts(values: Iterable[str | None]) -> dict[str, int]:
    counts = Counter("none" if value is None else value for value in values)
    return dict(sorted(counts.items()))


def _empty_mode(mode: str) -> dict:
    return {
        "mode": mode,
        "status": "NOT_READY",
        "selection_status": "NOT_READY",
        "reasons": ["NO_COMPARABLE_SAMPLES", "U4_UNOBSERVABLE_V1"],
        "valid_rows": 0,
        "comparable_rows": 0,
        "noncomparable_rows": 0,
        "comparison_unavailable_rows": 0,
        "context_build_failure_count": 0,
        "execution_protection_observable": False,
        "cohorts": [],
    }


def evaluate_jsonl(
    lines: Iterable[bytes], requested_modes: Sequence[str] | None = None,
) -> GateReport:
    """Evaluate persisted schema-v1 evidence without performing any I/O."""

    modes = sorted(set(requested_modes or MODES))
    if any(not isinstance(mode, str) or mode not in MODES for mode in modes):
        raise ValueError("requested_modes must contain only current Stage-S modes")
    rows: list[dict] = []
    event_rows = 0
    input_bytes = 0
    invalid = Counter()
    for raw in lines:
        event_rows += 1
        if event_rows > MAX_ROWS:
            raise EvidenceInputLimitError("maximum evidence row count exceeded")
        if len(raw) > MAX_LINE_BYTES:
            raise EvidenceInputLimitError("maximum evidence line size exceeded")
        input_bytes += len(raw)
        if input_bytes > MAX_FILE_BYTES:
            raise EvidenceInputLimitError("maximum evidence file size exceeded")
        try:
            rows.append(_parse_row(raw))
        except _InvalidRow as exc:
            invalid[exc.reason] += 1

    request_key_counts = Counter(row["request_key"] for row in rows)
    duplicate_keys = {
        key for key, count in request_key_counts.items() if count > 1
    }
    if duplicate_keys:
        duplicate_count = sum(
            count for key, count in request_key_counts.items()
            if key in duplicate_keys
        )
        invalid["DUPLICATE_REQUEST_KEY"] += duplicate_count
        rows = [row for row in rows if row["request_key"] not in duplicate_keys]

    by_mode: dict[str, list[dict]] = defaultdict(list)
    unscoped = 0
    for row in rows:
        if row["mode"] is None:
            unscoped += 1
        else:
            by_mode[row["mode"]].append(row)

    mode_reports = []
    for mode in modes:
        report = _empty_mode(mode)
        scoped = by_mode.get(mode, [])
        report["valid_rows"] = len(scoped)
        comparable = [
            row for row in scoped
            if row["shadow_comparison"] is not None
            and row["shadow_comparison"]["config_fingerprint"]
            and row["shadow_comparison"]["context_snapshot_version"]
        ]
        report["comparable_rows"] = len(comparable)
        report["noncomparable_rows"] = len(scoped) - len(comparable)
        report["comparison_unavailable_rows"] = len(scoped) - len(comparable)
        if report["comparison_unavailable_rows"]:
            report["context_build_failure_count"] = None
            report["reasons"] = sorted(set(report["reasons"] + [
                "CONTEXT_FAILURE_UNOBSERVABLE_V1",
            ]))
        if comparable:
            grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
            for row in comparable:
                shadow = row["shadow_comparison"]
                grouped[(shadow["config_fingerprint"], shadow["context_snapshot_version"])].append(row)
            cohorts = []
            for (config_fp, context_v), cohort_rows in sorted(grouped.items()):
                code_counts = {code: 0 for code in _CODES}
                for row in cohort_rows:
                    code_counts[row["shadow_comparison"]["code"]] += 1
                times = [row["_recorded_dt"] for row in cohort_rows]
                elapsed = [float(row["route_elapsed_ms"]) for row in cohort_rows]
                sample_count = len(cohort_rows)
                observation_seconds = (max(times) - min(times)).total_seconds()
                u1_ratio = code_counts["U1"] / sample_count
                cohort_reasons = []
                if sample_count < 50:
                    cohort_reasons.append("INSUFFICIENT_SAMPLES")
                if observation_seconds < 86400:
                    cohort_reasons.append("INSUFFICIENT_OBSERVATION")
                if code_counts["U1"] > 1 or u1_ratio > 0.02:
                    cohort_reasons.append("U1_LIMIT_EXCEEDED")
                for code in ("U2", "U3", "U4"):
                    if code_counts[code]:
                        cohort_reasons.append(f"{code}_PRESENT")
                if code_counts["E3"]:
                    cohort_reasons.append("E3_POLICY_UNVERIFIABLE_V1")
                cohort_status = "READY" if not cohort_reasons else "NOT_READY"
                cohorts.append({
                    "config_fingerprint": config_fp,
                    "context_snapshot_version": context_v,
                    "status": cohort_status,
                    "reasons": sorted(cohort_reasons),
                    "sample_count": sample_count,
                    "observation_seconds": observation_seconds,
                    "first_recorded_at": min(times).isoformat() + "Z",
                    "last_recorded_at": max(times).isoformat() + "Z",
                    "codes": code_counts,
                    "u1_ratio": u1_ratio,
                    "e1_rows": code_counts["E1"],
                    "e1_incomplete_reason_rows": 0,
                    "terminal_counts": _stable_counts(row["terminal"] for row in cohort_rows),
                    "outcome_counts": _stable_counts(row["outcome"] for row in cohort_rows),
                    "quality_verdict_counts": _stable_counts(row["quality_verdict"] for row in cohort_rows),
                    "result_count": {
                        "minimum": min(row["result_count"] for row in cohort_rows),
                        "maximum": max(row["result_count"] for row in cohort_rows),
                        "total": sum(row["result_count"] for row in cohort_rows),
                    },
                    "route_elapsed_ms": {
                        "minimum": min(elapsed),
                        "maximum": max(elapsed),
                        "p50": _nearest_rank(elapsed, 0.50),
                        "p95": _nearest_rank(elapsed, 0.95),
                    },
                })
            report["cohorts"] = cohorts
            selection_reasons = []
            if len(cohorts) > 1:
                selection_reasons.append("MULTIPLE_COHORTS")
            else:
                selection_reasons.extend(cohorts[0]["reasons"])
            if report["comparison_unavailable_rows"]:
                selection_reasons.append("CONTEXT_FAILURE_UNOBSERVABLE_V1")
                report["context_build_failure_count"] = None
            report["selection_status"] = (
                "READY" if not selection_reasons else "NOT_READY"
            )
            report["reasons"] = sorted(
                set(selection_reasons + ["U4_UNOBSERVABLE_V1"])
            )
        mode_reports.append(report)

    for report in mode_reports:
        global_reasons = []
        if invalid:
            global_reasons.append("INVALID_ROWS_PRESENT")
        if unscoped:
            global_reasons.append("CONTEXT_FAILURE_UNOBSERVABLE_V1")
        if global_reasons:
            report["selection_status"] = "NOT_READY"
            report["reasons"] = sorted(set(report["reasons"] + global_reasons))

    payload = {
        "schema_version": 1,
        "gate": "S_TO_C5_L2",
        "status": "NOT_READY",
        "selection_status": (
            "READY"
            if mode_reports
            and all(mode["selection_status"] == "READY" for mode in mode_reports)
            and not invalid
            and unscoped == 0
            else "NOT_READY"
        ),
        "requested_modes": modes,
        "thresholds": {
            "minimum_comparable_samples": 50,
            "minimum_observation_seconds": 86400,
            "maximum_u1_count": 1,
            "maximum_u1_ratio": 0.02,
            "maximum_u2_count": 0,
            "maximum_u3_count": 0,
            "maximum_u4_count": 0,
        },
        "input": {
            "event_rows": event_rows,
            "valid_rows": len(rows),
            "invalid_rows": sum(invalid.values()),
            "invalid_rows_by_reason": dict(sorted(invalid.items())),
            "unscoped_noncomparable_rows": unscoped,
        },
        "modes": mode_reports,
    }
    return GateReport(payload)
