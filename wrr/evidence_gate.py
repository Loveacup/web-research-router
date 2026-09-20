"""Pure offline evaluator for Stage-S decision-evidence JSONL.

This module owns versioned wire decoders for persisted schemas v1/v2. It does
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
_V2_TOP_FIELDS = (_TOP_FIELDS - {"actual_provider"}) | {
    "context_status", "comparison_status", "execution_protection", "context_cohort_id",
}
_V2_BLOCKERS = ["D5_COHORT_WINDOW_UNRESOLVED", "D6_DURABLE_COVERAGE_UNRESOLVED"]
_CAMPAIGN_POLICY_VERSION = "EV-D5-v1"
_CAMPAIGN_MIN_SAMPLES = 50
_CAMPAIGN_MIN_OBSERVATION_SECONDS = 86400
_CAMPAIGN_MAX_U1_COUNT = 1
_CAMPAIGN_MAX_U1_RATIO = 0.02
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


def _validate_v2(value: dict) -> None:
    """Validate the persisted v2 wire independently of live runtime imports."""
    for field, allowed, reason in (
        ("context_status", {"available", "cold", "refresh_failed"}, "INVALID_CONTEXT_STATUS"),
        ("comparison_status", {"compared", "context_unavailable", "context_build_failed",
                               "context_expired", "context_mismatch", "comparison_failed"},
         "INVALID_COMPARISON_STATUS"),
        ("execution_protection", {"not_required", "protected_by_legacy", "unprotected_empty",
                                  "unprotected_error", "unobservable"}, "INVALID_EXECUTION_PROTECTION"),
    ):
        if type(value[field]) is not str or value[field] not in allowed:
            raise _InvalidRow(reason)
    cohort = value["context_cohort_id"]
    if cohort is not None:
        try:
            canonical = _parse_uuid4(cohort)
        except _InvalidRow as exc:
            raise _InvalidRow("INVALID_CONTEXT_COHORT_ID") from exc
        if canonical != cohort:
            raise _InvalidRow("INVALID_CONTEXT_COHORT_ID")
    shadow = value["shadow_comparison"]
    if shadow is not None:
        _validate_v2_shadow(shadow)
    context, comparison = value["context_status"], value["comparison_status"]
    compared = comparison == "compared"
    valid_context = (
        (context == "cold" and cohort is None and comparison == "context_unavailable")
        or (context == "available" and cohort is not None
            and comparison not in {"context_unavailable", "context_build_failed"})
        or (context == "refresh_failed" and (
            (cohort is None and comparison == "context_build_failed")
            or (cohort is not None and comparison in {
                "compared", "context_expired", "context_mismatch", "comparison_failed"})))
    )
    if not valid_context or compared != (shadow is not None):
        raise _InvalidRow("INVALID_V2_CROSS_FIELDS")
    outcome, count = value["outcome"], value["result_count"]
    # actual_provider is intentionally absent from the persisted v2 wire.
    if (outcome == "success") != (count > 0):
        raise _InvalidRow("INVALID_V2_OUTCOME")
    if not compared:
        expected = "unobservable"
    elif shadow["descriptor_provider_count"] > 0:
        expected = "not_required"
    else:
        expected = {"success": "protected_by_legacy", "empty": "unprotected_empty",
                    "error": "unprotected_error"}[outcome]
    if value["execution_protection"] != expected:
        raise _InvalidRow("INVALID_V2_CROSS_FIELDS")


def _validate_v2_shadow(shadow: object) -> None:
    if not isinstance(shadow, dict):
        raise _InvalidRow("INVALID_SHADOW_TYPE")
    count_fields = ("legacy_provider_count", "descriptor_provider_count",
                    "omitted_provider_count", "added_provider_count")
    if set(shadow) != {"code", "safe", "reasons_complete", *count_fields}:
        raise _InvalidRow("INVALID_SHADOW_FIELDS")
    code = shadow["code"]
    if type(code) is not str or code not in {"E0", "E1", "E2", "U1", "U2", "U3"}:
        raise _InvalidRow("INVALID_SHADOW_CODE")
    if type(shadow["safe"]) is not bool:
        raise _InvalidRow("INVALID_SHADOW_SAFE")
    if any(type(shadow[field]) is not int or shadow[field] < 0 for field in count_fields):
        raise _InvalidRow("INVALID_SHADOW_COUNTS")
    if type(shadow["reasons_complete"]) is not bool:
        raise _InvalidRow("INVALID_SHADOW_REASONS_COMPLETE")
    legacy, descriptor, omitted, added = (shadow[field] for field in count_fields)
    valid = (shadow["safe"] == code.startswith("E") and omitted <= legacy
             and added <= descriptor and descriptor == legacy - omitted + added)
    if code in {"E0", "E2"}:
        valid = valid and legacy == descriptor and omitted == added == 0
    elif code == "E1":
        valid = (valid and descriptor < legacy and omitted == legacy - descriptor
                 and added == 0 and shadow["reasons_complete"])
    elif code == "U1":
        valid = valid and added == 0 and omitted > 0
    elif code == "U2":
        valid = valid and added > 0
    if not valid:
        raise _InvalidRow("INVALID_SHADOW_SEMANTICS")


def _parse_row(raw: bytes) -> dict:
    if not raw.strip():
        raise _InvalidRow("EMPTY_LINE")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _InvalidRow("INVALID_UTF8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except _InvalidRow:
        raise
    except (ValueError, RecursionError) as exc:
        raise _InvalidRow("INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise _InvalidRow("ROW_NOT_OBJECT")
    version = value.get("schema_version")
    # Legacy-shaped bad rows retain their original keyset-first diagnostics.
    # Recognizable v2 envelopes still dispatch unsupported versions explicitly.
    if version != 2 and not (set(value) & (_V2_TOP_FIELDS - _TOP_FIELDS)):
        if set(value) - _TOP_FIELDS:
            raise _InvalidRow("UNKNOWN_FIELDS")
        if _TOP_REQUIRED_FIELDS - set(value):
            raise _InvalidRow("MISSING_FIELDS")
    if "schema_version" not in value:
        raise _InvalidRow("MISSING_FIELDS")
    if type(version) is not int or version not in (1, 2):
        raise _InvalidRow("UNSUPPORTED_SCHEMA_VERSION")
    fields = _TOP_FIELDS if version == 1 else _V2_TOP_FIELDS
    if set(value) - fields:
        raise _InvalidRow("UNKNOWN_FIELDS")
    if (fields - {"shadow_comparison"}) - set(value):
        raise _InvalidRow("MISSING_FIELDS")
    value.setdefault("shadow_comparison", None)
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
    provider = value.get("actual_provider")
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
    try:
        valid_elapsed = type(elapsed) in (int, float) and isfinite(elapsed) and elapsed >= 0
    except OverflowError:
        valid_elapsed = False
    if not valid_elapsed:
        raise _InvalidRow("INVALID_ROUTE_ELAPSED_MS")
    if version == 2:
        _validate_v2(value)
        return value
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


def _v2_summary(rows: list[dict]) -> dict:
    """All validated persisted rows, never an authoritative request denominator."""
    compared = sum(row["comparison_status"] == "compared" for row in rows)
    protection = _stable_counts(row["execution_protection"] for row in rows)
    return {
        "population": "validated_persisted_rows_not_request_attempts",
        "valid_rows": len(rows),
        "comparable_rows": compared,
        "noncomparable_rows": len(rows) - compared,
        "context_status_counts": _stable_counts(row["context_status"] for row in rows),
        "comparison_status_counts": _stable_counts(row["comparison_status"] for row in rows),
        "execution_protection_counts": protection,
        "effective_u4_rows": protection.get("unprotected_empty", 0) + protection.get("unprotected_error", 0),
        "u4_unobservable_rows": protection.get("unobservable", 0),
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
    unscoped_versions = Counter()
    for row in rows:
        if row["mode"] is None:
            unscoped += 1
            unscoped_versions[row["schema_version"]] += 1
        else:
            by_mode[row["mode"]].append(row)

    mode_reports = []
    for mode in modes:
        report = _empty_mode(mode)
        scoped = [row for row in by_mode.get(mode, []) if row["schema_version"] == 1]
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
        if unscoped_versions[1]:
            global_reasons.append("CONTEXT_FAILURE_UNOBSERVABLE_V1")
        if unscoped_versions[2]:
            global_reasons.append("UNSCOPED_EVIDENCE_V2")
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
    v2_rows = [row for row in rows if row["schema_version"] == 2]
    if v2_rows:
        payload["reasons"] = list(_V2_BLOCKERS)
        payload["persisted_v2"] = _v2_summary(v2_rows)
        for report in mode_reports:
            scoped_v2 = [row for row in v2_rows if row["mode"] == report["mode"]]
            if not scoped_v2:
                continue
            legacy_count = report["valid_rows"]
            for cohort in report["cohorts"]:
                cohort["schema_version"] = 1
            grouped_v2 = defaultdict(list)
            for row in scoped_v2:
                grouped_v2[row["context_cohort_id"]].append(row)
            for cohort_id, cohort_rows in sorted(grouped_v2.items(), key=lambda pair: pair[0] or ""):
                summary = _v2_summary(cohort_rows)
                codes = {code: 0 for code in _CODES}
                for row in cohort_rows:
                    if row["comparison_status"] == "compared":
                        codes[row["shadow_comparison"]["code"]] += 1
                reasons = list(_V2_BLOCKERS)
                if summary["effective_u4_rows"]:
                    reasons.append("U4_PRESENT")
                if summary["u4_unobservable_rows"]:
                    reasons.append("U4_UNOBSERVABLE_V2")
                report["cohorts"].append({
                    **summary,
                    "schema_version": 2,
                    "context_cohort_id": cohort_id,
                    "status": "NOT_READY",
                    "reasons": sorted(reasons),
                    "sample_count": summary["comparable_rows"],
                    "codes": codes,
                })
            summary = _v2_summary(scoped_v2)
            report["persisted_v2"] = summary
            report["valid_rows"] += summary["valid_rows"]
            report["comparable_rows"] += summary["comparable_rows"]
            report["noncomparable_rows"] += summary["noncomparable_rows"]
            report["comparison_unavailable_rows"] += summary["noncomparable_rows"]
            if report["context_build_failure_count"] is not None:
                report["context_build_failure_count"] += summary["comparison_status_counts"].get("context_build_failed", 0)
            report["execution_protection_observable"] = not legacy_count and not summary["u4_unobservable_rows"]
            report["selection_status"] = "NOT_READY"
            if not legacy_count:
                report["reasons"] = [reason for reason in report["reasons"] if not reason.endswith("_V1")]
            if report["comparable_rows"]:
                report["reasons"] = [reason for reason in report["reasons"] if reason != "NO_COMPARABLE_SAMPLES"]
            report["reasons"] = sorted(set(report["reasons"] + _V2_BLOCKERS))
        payload["selection_status"] = "NOT_READY"
    return GateReport(payload)


def _canonical_evidence_bytes(evidence: object) -> bytes:
    """Render one admission's evidence as canonical JSON bytes, fail-closed."""
    try:
        text = json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        # Unserialisable evidence never leaks its value; it decodes to a
        # structurally invalid row that the shared parser rejects.
        return b"null"
    return text.encode("utf-8")


def _campaign_join_ok(facts: object, admissions: object) -> bool:
    """The admission ledger must exactly, unambiguously cover the facts.

    Every admission carries precisely ``{seq, request_key, evidence}``; the
    ``seq`` values form a gap-free ``1..N`` run; the admitted ``request_key``
    sequence-to-key mapping equals the durable facts order (so swaps,
    duplicates, and missing/extra keys fail closed); and each row's persisted
    ``request_key`` matches the key it was admitted under.
    """
    if not isinstance(admissions, list):
        return False
    facts_keys = getattr(facts, "request_keys", None)
    if not isinstance(facts_keys, tuple):
        return False
    capacity = getattr(facts, "capacity", None)
    if (
        type(capacity) is not int
        or len(admissions) != capacity
        or len(facts_keys) != capacity
        or any(not isinstance(key, str) for key in facts_keys)
        or len(set(facts_keys)) != capacity
    ):
        return False
    seqs: list[int] = []
    keys: list[str] = []
    for admission in admissions:
        if not isinstance(admission, dict) or set(admission) != {
            "seq", "request_key", "evidence"
        }:
            return False
        seq = admission["seq"]
        if type(seq) is not int:  # rejects bool and non-int seq values
            return False
        if not isinstance(admission["request_key"], str):
            return False
        seqs.append(seq)
        keys.append(admission["request_key"])
        evidence = admission["evidence"]
        if (
            not isinstance(evidence, dict)
            or evidence.get("request_key") != admission["request_key"]
        ):
            return False
    if sorted(seqs) != list(range(1, len(admissions) + 1)):
        return False
    if len(set(keys)) != len(keys):
        return False
    ordered_keys = tuple(key for _, key in sorted(zip(seqs, keys)))
    if ordered_keys != facts_keys:
        return False
    return True


def _campaign_mode_report(mode: str, rows: list[dict]) -> dict:
    """Per-mode threshold projection over that mode's declared, comparable rows."""
    summary = _v2_summary(rows)
    comparable_rows = summary["comparable_rows"]
    effective_u4_rows = summary["effective_u4_rows"]
    compared = [row for row in rows if row["comparison_status"] == "compared"]
    times = [row["_recorded_dt"] for row in compared]
    observation_seconds = (
        (max(times) - min(times)).total_seconds() if len(times) >= 2 else 0.0
    )
    codes = {code: 0 for code in _CODES}
    for row in compared:
        codes[row["shadow_comparison"]["code"]] += 1
    u1_ratio = codes["U1"] / comparable_rows if comparable_rows else 0.0
    reasons: list[str] = []
    if comparable_rows < _CAMPAIGN_MIN_SAMPLES:
        reasons.append("INSUFFICIENT_SAMPLES")
    if observation_seconds < _CAMPAIGN_MIN_OBSERVATION_SECONDS:
        reasons.append("INSUFFICIENT_OBSERVATION")
    if codes["U1"] > _CAMPAIGN_MAX_U1_COUNT or u1_ratio > _CAMPAIGN_MAX_U1_RATIO:
        reasons.append("U1_LIMIT_EXCEEDED")
    if codes["U2"]:
        reasons.append("U2_PRESENT")
    if codes["U3"]:
        reasons.append("U3_PRESENT")
    if effective_u4_rows:
        reasons.append("U4_PRESENT")
    return {
        "mode": mode,
        "status": "READY" if not reasons else "NOT_READY",
        "valid_rows": summary["valid_rows"],
        "comparable_rows": comparable_rows,
        "effective_u4_rows": effective_u4_rows,
        "observation_seconds": observation_seconds,
        "reasons": sorted(reasons),
    }


def evaluate_campaign(facts: object, admissions: object) -> GateReport:
    """Pure projection of a fixed campaign into a manual-review verdict.

    ``facts`` is a ``CampaignFacts``-shaped durable projection and ``admissions``
    the ledger's ``export()`` rows. The evaluator performs no I/O, never reaches
    ``READY``, and never authorises an automatic Stage C: the strongest verdict
    it returns is ``ELIGIBLE_FOR_MANUAL_C5_REVIEW`` with ``automatic_action`` off.
    Structurally invalid or unexpected input fails closed without echoing raw
    identifiers, timestamps, or cohort values.
    """
    admission_list = admissions if isinstance(admissions, list) else []

    # Bounded, canonical decode of every admitted evidence row through the shared
    # v2 wire parser — the same size/count limits the JSONL evaluator enforces.
    parsed: list[dict | None] = []
    input_bytes = 0
    for index, admission in enumerate(admission_list):
        if index + 1 > MAX_ROWS:
            raise EvidenceInputLimitError("maximum evidence row count exceeded")
        evidence = (
            admission.get("evidence") if isinstance(admission, dict) else None
        )
        raw = _canonical_evidence_bytes(evidence)
        if len(raw) > MAX_LINE_BYTES:
            raise EvidenceInputLimitError("maximum evidence line size exceeded")
        input_bytes += len(raw)
        if input_bytes > MAX_FILE_BYTES:
            raise EvidenceInputLimitError("maximum evidence file size exceeded")
        try:
            parsed.append(_parse_row(raw))
        except _InvalidRow:
            parsed.append(None)

    valid_rows = [
        row for row in parsed if row is not None and row.get("schema_version") == 2
    ]

    reasons: list[str] = []

    declaration = getattr(facts, "declaration", None)
    if declaration is None:
        reasons.append("CAMPAIGN_NOT_DECLARED")
        policy_version = None
        requested_modes: list[str] = []
        accepted_cohort = None
    else:
        policy_version = getattr(declaration, "policy_version", None)
        requested_modes = list(getattr(declaration, "requested_modes", ()) or ())
        accepted_cohort = getattr(declaration, "accepted_context_cohort_id", None)
        if policy_version != _CAMPAIGN_POLICY_VERSION:
            reasons.append("POLICY_VERSION_UNSUPPORTED")

    capacity = getattr(facts, "capacity", None)
    if type(capacity) is not int or capacity <= 0:
        reasons.append("CAMPAIGN_CAPACITY_UNDECLARED")
        capacity = None

    if getattr(facts, "status", None) != "clean" or not getattr(
        facts, "session_closed_cleanly", False
    ):
        reasons.append("CAMPAIGN_NOT_CLEAN")

    if capacity is not None:
        coverage_ok = (
            getattr(facts, "persisted_evidence_count", None) == capacity
            and getattr(facts, "terminal_count", None) == capacity
            and getattr(facts, "attempts_started", None) == capacity
            and getattr(facts, "start_sequence", None) == 1
            and getattr(facts, "end_sequence", None) == capacity
            and getattr(facts, "sequence_gaps", None) == 0
            and getattr(facts, "fault_count", None) == 0
            and getattr(facts, "dropped_count", None) == 0
            and getattr(facts, "unresolved_count", None) == 0
            and getattr(facts, "duplicate_count", None) == 0
        )
        if not coverage_ok:
            reasons.append("CAMPAIGN_COVERAGE_INCOMPLETE")

    if not _campaign_join_ok(facts, admissions):
        reasons.append("ADMISSION_JOIN_MISMATCH")

    if any(
        row is None or row.get("schema_version") != 2 for row in parsed
    ):
        reasons.append("INVALID_EVIDENCE_ROWS_PRESENT")

    declared_modes = set(requested_modes)
    if any(row["mode"] not in declared_modes for row in valid_rows):
        reasons.append("UNDECLARED_MODE_PRESENT")
    if accepted_cohort is not None and any(
        row.get("context_cohort_id") != accepted_cohort for row in valid_rows
    ):
        reasons.append("COHORT_MISMATCH")
    if any(row["context_status"] != "available" for row in valid_rows):
        reasons.append("CONTEXT_UNAVAILABLE")
    if any(
        row["context_status"] == "available" and row["comparison_status"] != "compared"
        for row in valid_rows
    ):
        reasons.append("COMPARISON_UNAVAILABLE")

    mode_reports = []
    any_mode_not_ready = False
    for mode in requested_modes:
        scoped = [
            row
            for row in valid_rows
            if row["mode"] == mode
            and (accepted_cohort is None or row.get("context_cohort_id") == accepted_cohort)
        ]
        report = _campaign_mode_report(mode, scoped)
        mode_reports.append(report)
        if report["status"] != "READY":
            any_mode_not_ready = True

    ready = bool(mode_reports) and not reasons and not any_mode_not_ready
    payload = {
        "gate": "S_TO_C5_L2",
        "status": "ELIGIBLE_FOR_MANUAL_C5_REVIEW" if ready else "NOT_READY",
        "automatic_action": False,
        "reasons": sorted(set(reasons)),
        "policy_version": policy_version,
        "requested_modes": requested_modes,
        "thresholds": {
            "minimum_comparable_samples": _CAMPAIGN_MIN_SAMPLES,
            "minimum_observation_seconds": _CAMPAIGN_MIN_OBSERVATION_SECONDS,
            "maximum_u1_count": _CAMPAIGN_MAX_U1_COUNT,
            "maximum_u1_ratio": _CAMPAIGN_MAX_U1_RATIO,
            "maximum_u2_count": 0,
            "maximum_u3_count": 0,
            "maximum_u4_count": 0,
        },
        "modes": mode_reports,
    }
    return GateReport(payload)
