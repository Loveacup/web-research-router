#!/usr/bin/env python3
"""Freeze/register v3 locally; no product imports, credentials or network calls."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unicodedata

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_VERSION = 3
GATES_V3 = {
    "source_recall_min": 0.8,
    "capacity_normalized_useful_loss_max": 0.10,
    "delivery_precision_min": 0.85,
    "same_scope_enough_brier_max": 0.2,
    "isolated_paired_increment_p95_s_max": 8.0,
    "regular_judge_posts_max": 3,
    "recovery_judge_posts_max": 4,
    "external_egress_attempts_max": 0,
    "hard_source_constraint_violations_max": 0,
    "real_independent_queries_min": 60,
    "user_query_audit_fraction_min": 0.20,
}
SAMPLING = {
    "rule": "query-only-stratified-time-first-v1",
    "real_query_target": 60,
    "language_targets": {"zh": 30, "en": 30},
    "timestamp_cutoff_exclusive_utc": "2026-10-20T00:00:00Z",
    "sources": ["omp", "hermes"],
    "order": ["timestamp_utc_ascending", "source_enum_omp_then_hermes", "normalized_query_utf8_ascending"],
    "normalization": "NFKC-casefold-whitespace",
    "selection": "screen privacy/provenance/exposure/task-family duplicates before first 30 per language; no scores or outcomes",
    "language_policy": "30 zh + 30 en required; report shortage, never substitute or synthesize",
}
EXECUTION = {"latency_mode": "isolated-paired", "query_concurrency": 1,
             "harness_retries": 0, "repetitions": 1, "deadline_s": 15, "count": 8}
METRICS = {"primary_aggregation": "query_macro", "pooled_ratios": "diagnostic_only",
           "undefined_or_unknown": "retain_coverage_and_block_formal_pass",
           "legacy_rel_gate": "report_only", "legacy_latency_limit_s": 1.5}
HISTORICAL_QUERIES = (ROOT / "evals/queries.jsonl", ROOT / "evals/holdout/queries-combined.jsonl")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalized(query):
    return " ".join(unicodedata.normalize("NFKC", query).casefold().split())


def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def save_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def historical_bindings():
    return {str(path.relative_to(ROOT)): sha256(path) for path in HISTORICAL_QUERIES}


def freeze(protocol, output):
    """Create the immutable method artifact before any new query selection."""
    protocol = Path(protocol)
    if not protocol.read_text().startswith("# WRR7 正式新留出集协议（v3）"):
        raise ValueError("v3 requires its own PROTOCOL-v3.md, not a renamed legacy registration")
    result = {
        "holdout_protocol_version": PROTOCOL_VERSION,
        "artifact_type": "pre_sampling_protocol_freeze",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256(protocol), "gates": dict(GATES_V3),
        "sampling": dict(SAMPLING), "execution": dict(EXECUTION), "metrics": dict(METRICS),
        "historical_query_files": historical_bindings(),
        "registration_tool_sha256": sha256(__file__),
        "next_run_environment_note": {
            "local_venv_rebuilt_python": "3.13.12", "mcp_dependency": "missing",
            "scope": "next run only; does not replace historical run environments or results",
        },
        "cohort_status": "not_sampled", "final_pass": False,
    }
    save_new(output, result)
    return result


def validate_freeze(protocol, freeze_path):
    frozen = json.loads(Path(freeze_path).read_text())
    expected = {"holdout_protocol_version": 3, "artifact_type": "pre_sampling_protocol_freeze",
                "protocol_sha256": sha256(protocol), "gates": GATES_V3,
                "sampling": SAMPLING, "execution": EXECUTION, "metrics": METRICS,
                "historical_query_files": historical_bindings(),
                "registration_tool_sha256": sha256(__file__)}
    for key, value in expected.items():
        if frozen.get(key) != value:
            raise ValueError(f"v3 pre-sampling freeze changed: {key}")
    return frozen


def validate_queries(rows):
    if not rows:
        raise ValueError("formal v3 requires at least 60 real independent queries")
    ids, keys, counts = [], [], {}
    excluded = {normalized(row["query"]) for path in HISTORICAL_QUERIES for row in jsonl(path)}
    for row in rows:
        sid, query = row.get("id"), row.get("query")
        if (not isinstance(sid, str) or not sid or Path(sid).name != sid or sid in {".", ".."}
                or not isinstance(query, str) or not normalized(query)):
            raise ValueError("nonempty filename-safe id and query required")
        if (row.get("synthetic") is not False or row.get("source") not in SAMPLING["sources"]
                or row.get("stratum") in {None, "sensitive_negative", "敏感负例"}
                or row.get("lang") not in {"zh", "en"}):
            raise ValueError("v3 formal cohort contains real public omp/hermes queries only; negatives separate")
        key = normalized(query)
        if key in excluded:
            raise ValueError("previously exposed query cannot become a new formal v3 holdout")
        count = row.get("count", 8)
        if type(count) is not int or count < 1:
            raise ValueError("requested query count must be a positive integer")
        ids.append(sid)
        keys.append(key)
        counts[sid] = count
    if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
        raise ValueError("duplicate query or id")
    if len(rows) != SAMPLING["real_query_target"]:
        raise ValueError("v3 chronological first N requires exactly 60 real independent queries; shortages are not formal cohorts")
    if dict(Counter(row["lang"] for row in rows)) != SAMPLING["language_targets"]:
        raise ValueError("v3 requires 30 real zh and 30 real en queries; report language shortages")
    return counts


def validate_sampling(audit, query_hash, freeze_hash, rows):
    expected = {"protocol_freeze_sha256": freeze_hash, "queries_sha256": query_hash,
                "rule": SAMPLING["rule"], "selection_status": "complete",
                "independence_status": "audited", "outcome_accessed": False,
                "selected_real_count": len(rows)}
    for key, value in expected.items():
        if audit.get(key) != value or (key == "outcome_accessed" and type(audit.get(key)) is not bool):
            raise ValueError(f"v3 sampling audit mismatch: {key}")
    for key in ("source_snapshot_sha256", "screening_decisions_sha256", "exposed_task_families_sha256"):
        value = audit.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"v3 sampling audit requires {key}")
    reasons = audit.get("exclusion_reason_counts")
    if not isinstance(reasons, dict) or any(type(n) is not int or n < 0 for n in reasons.values()):
        raise ValueError("v3 sampling audit requires nonnegative exclusion reason counts")


def preregister(queries, protocol, protocol_freeze, sampling_audit, output, *, seed=20261020):
    validate_freeze(protocol, protocol_freeze)
    rows = jsonl(queries)
    counts = validate_queries(rows)
    audit = json.loads(Path(sampling_audit).read_text())
    validate_sampling(audit, sha256(queries), sha256(protocol_freeze), rows)
    result = {
        "holdout_protocol_version": 3, "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256(protocol), "queries_sha256": sha256(queries),
        "protocol_freeze_sha256": sha256(protocol_freeze),
        "sampling_audit_sha256": sha256(sampling_audit),
        "protocol_freeze_json": Path(protocol_freeze).read_bytes().decode("utf-8"),
        "sampling_audit_json": Path(sampling_audit).read_bytes().decode("utf-8"),
        "selected_ids": sorted(counts), "real_public_query_count": len(rows),
        "language_counts": dict(Counter(row["lang"] for row in rows)),
        "request_counts": counts, "gates": dict(GATES_V3), "metrics": dict(METRICS),
        "seed": seed, **EXECUTION,
        "independence_status": "provenance/exposure audit recorded; not independent human gold",
        "user_audit_status": "pending", "final_pass": False,
    }
    save_new(output, result)
    return result


def holdout_manifest(args, rows, registration):
    """Called before delegates by run_eval; legacy v1 registration stays separate."""
    counts = validate_queries(rows)
    frozen_json = registration.get("protocol_freeze_json", "{}")
    audit_json = registration.get("sampling_audit_json", "{}")
    frozen, audit = json.loads(frozen_json), json.loads(audit_json)
    # Embedded artifacts bind the run without requiring private coordinator paths.
    for key, value in {"holdout_protocol_version": 3, "artifact_type": "pre_sampling_protocol_freeze",
                       "protocol_sha256": sha256(args.protocol), "gates": GATES_V3,
                       "sampling": SAMPLING, "execution": EXECUTION, "metrics": METRICS,
                       "historical_query_files": historical_bindings(),
                       "registration_tool_sha256": sha256(__file__)}.items():
        if frozen.get(key) != value:
            raise ValueError(f"v3 frozen method mismatch: {key}")
    for key, value in {"holdout_protocol_version": 3, "protocol_sha256": sha256(args.protocol),
                       "queries_sha256": sha256(args.queries), "gates": GATES_V3,
                       "metrics": METRICS, "request_counts": counts,
                       "selected_ids": sorted(counts), "real_public_query_count": len(rows),
                       "language_counts": dict(Counter(row["lang"] for row in rows)),
                       **EXECUTION}.items():
        if registration.get(key) != value:
            raise ValueError(f"v3 holdout execution differs from preregistration: {key}")
    # Preserve original artifact bytes, including third-party audit formatting.
    for key, value in (("protocol_freeze_sha256", frozen_json), ("sampling_audit_sha256", audit_json)):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        if registration.get(key) != digest:
            raise ValueError(f"v3 embedded artifact hash mismatch: {key}")
    validate_sampling(audit, sha256(args.queries), registration["protocol_freeze_sha256"], rows)
    if (args.ids or getattr(args, "latency_mode", "legacy") != "isolated-paired"
            or args.concurrency != 1 or registration.get("seed") != getattr(args, "seed", 20261020)):
        raise ValueError("v3 formal holdout cannot use partial IDs or changed execution")
    return {"holdout_protocol_version": 3, "protocol_sha256": sha256(args.protocol),
            "preregistration_sha256": sha256(args.preregistration),
            "protocol_freeze_sha256": registration["protocol_freeze_sha256"],
            "sampling_audit_sha256": registration["sampling_audit_sha256"],
            "acceptance_gates": dict(GATES_V3), "acceptance_metrics": dict(METRICS),
            "request_counts": counts, "final_pass": False, "user_audit_status": "pending"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    method = sub.add_parser("freeze")
    method.add_argument("--protocol", type=Path, required=True)
    method.add_argument("--output", type=Path, required=True)
    pre = sub.add_parser("preregister")
    for flag in ("queries", "protocol", "protocol-freeze", "sampling-audit", "output"):
        pre.add_argument(f"--{flag}", type=Path, required=True)
    pre.add_argument("--seed", type=int, default=20261020)
    args = parser.parse_args()
    if args.command == "freeze":
        freeze(args.protocol, args.output)
    else:
        preregister(args.queries, args.protocol, args.protocol_freeze, args.sampling_audit,
                    args.output, seed=args.seed)


if __name__ == "__main__":
    main()
