#!/usr/bin/env python3
"""Score staged frozen holdout annotations offline; never import the live harness.

Writes content.json and routing.json only. Existing reports and blind labels are
never changed. Sufficiency requires the exact tasks and an explicit labels hash.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics

try:
    from .prepare_annotations import GATES, jsonl, sha256
except ImportError:  # Direct script invocation, without importing product modules.
    from prepare_annotations import GATES, jsonl, sha256


def ratio(n, d):
    return n / d if d else None


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def unique(rows, key, description):
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"duplicate {description}")
    return result


def exact(actual, expected, description):
    if actual != expected:
        raise ValueError(f"{description} changed or identity mismatch")


def review(row):
    metadata = row.get("review", {})
    if (row.get("annotation_status") != "complete"
            or any(not isinstance(metadata.get(k), str) or not metadata[k].strip()
                   for k in ("annotator", "model"))
            or type(metadata.get("user_reviewed")) is not bool):
        raise ValueError("complete annotations with explicit review metadata required")


def tristate(value):
    if value is not None and type(value) is not bool:
        raise ValueError("annotation must be true, false, or null")


def rationale(row):
    if not isinstance(row.get("rationale"), str) or not row["rationale"].strip():
        raise ValueError("nonempty annotation rationale required, including unknown")


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * p
    lower, upper = int(index), min(int(index) + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def failure_penalized_p95(successes, failed_n):
    n = len(successes) + failed_n
    if not n:
        return None
    index = (n - 1) * .95
    lower, upper = int(index), min(int(index) + 1, n - 1)
    values = sorted(successes)
    if upper >= len(values):
        return "failed_or_unknown"
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def latency_summary(queries, records, manifest):
    """Same all-attempt/linear-p95 policy as run_eval; missing data stay unknown."""
    walls, baselines, increments, successes = [], [], [], []
    pipeline_statuses, baseline_statuses, dependencies = Counter(), Counter(), Counter()
    failed_pairs = dependency_failed_n = recorded = 0
    for q in queries:
        record = records[q["id"]]
        if record is None:
            pipeline_statuses["missing_evidence"] += 1
            continue
        recorded += 1
        ev = record.get("evidence")
        status = record.get("pipeline_error") or ("ok" if ev else "missing_evidence")
        pipeline_statuses[status] += 1
        wall = record.get("pipeline_wall_s")
        if number(wall):
            walls.append(wall)
        calls = record.get("latency", {}).get("pipeline", {}).get("calls")
        failures = [call.get("status", "unknown") for call in calls or [] if call.get("status") != "ok"]
        if calls is None:
            failures.append("missing_boundary_accounting")
        # Saved source/judge failures matter even if their adapter returned normally.
        if ev:
            failures += [f"source:{run.get('status', 'unknown')}" for run in ev.get("runs", [])
                         if run.get("status") not in {"ok", "empty"}]
            failures += [f"judge:{failure}" for failure in ev.get("diagnostics", {}).get("judge", {}).get("failures", [])]
        dependencies.update(failures)
        dependency_failed_n += bool(failures)
        baseline = record.get("baseline")
        if not baseline:
            baseline_statuses["missing"] += 1
            continue
        baseline_statuses[baseline.get("status", "unknown")] += 1
        base_wall = baseline.get("wall_s")
        if number(base_wall):
            baselines.append(base_wall)
        if not number(wall) or not number(base_wall):
            continue
        increment = wall - base_wall
        increments.append(increment)
        if status == "ok" and baseline.get("status") == "ok" and not failures:
            successes.append(increment)
        else:
            failed_pairs += 1
    missing = len(queries) - len(increments)
    return {"latency_method_version": 1, "latency_mode": manifest.get("latency_mode"),
            "seed": manifest.get("seed"), "expected_public_n": len(queries), "recorded_public_n": recorded,
            "pipeline_wall_n": len(walls), "baseline_wall_n": len(baselines), "paired_latency_n": len(increments),
            "missing_pair_n": missing, "failed_pair_n": failed_pairs,
            "pipeline_statuses": dict(pipeline_statuses), "baseline_statuses": dict(baseline_statuses),
            "pipeline_dependency_failures": dict(dependencies),
            "pipeline_dependency_failure_query_n": dependency_failed_n,
            "pipeline_observed_p50_s": percentile(walls, .5), "pipeline_observed_p95_s": percentile(walls, .95),
            "baseline_observed_p50_s": percentile(baselines, .5), "baseline_observed_p95_s": percentile(baselines, .95),
            "increment_observed_p50_s": percentile(increments, .5), "increment_observed_p95_s": percentile(increments, .95),
            "increment_failure_penalized_p95_s": failure_penalized_p95(successes, failed_pairs + missing),
            "observed_wall_policy": "all finite observed outcomes, including failures and timeouts; no truncation",
            "failure_penalty": "failed/missing pairs and dependency failures rank as +infinity",
            "provider_queue_s": "unknown", "provider_service_s": "unknown"}


def cohort(q):
    return "older_zh_27" if q["id"].startswith("wrr7-holdout-older-zh-") else "original_60"


def grouped(details, summarize):
    return {axis: {value: summarize([row for row in details if row[axis] == value])
                   for value in sorted({row[axis] for row in details})}
            for axis in ("lang", "cohort", "stratum")}


def content_summary(details):
    items = [item for row in details for item in row["candidates"]]
    useful = [c for c in items if c["content_useful"] is True]
    unknown = [c for c in items if c["content_useful"] is None]
    killed = sum(c["rel_killed"] is True for c in useful)
    known_uncertain_kill = sum(c["rel_killed"] is None for c in useful)
    unknown_killed = sum(c["rel_killed"] is True for c in unknown)
    unknown_maybe_killed = sum(c["rel_killed"] is not False for c in unknown)
    unknown_not_killed = sum(c["rel_killed"] is not True for c in unknown)
    delivered = [c for c in items if c["delivered"]]
    wrong = sum(c["content_useful"] is False for c in delivered)
    delivered_unknown = sum(c["content_useful"] is None for c in delivered)
    t = len(useful)
    return {"query_n": len(details), "candidate_n": len(items), "useful_n": t,
            "not_useful_n": sum(c["content_useful"] is False for c in items),
            "unknown_content_n": len(unknown), "false_kill_n": killed,
            "false_kill_rate": ratio(killed, t) if not known_uncertain_kill else None,
            "unknown_rel_decision_n": sum(c["rel_killed"] is None for c in items),
            "known_useful_unknown_rel_n": known_uncertain_kill,
            "unknown_content_rel_killed_n": unknown_killed,
            "false_kill_bounds": {"lower": ratio(killed, t + unknown_not_killed)
                                  if t + unknown_not_killed else 1.0 if unknown_maybe_killed else None,
                                  "upper": ratio(killed + known_uncertain_kill + unknown_maybe_killed,
                                                 t + unknown_maybe_killed)
                                  if t + unknown_maybe_killed else 0.0 if unknown else None},
            "unknown_all_useful_false_kill_rate": ratio(killed + unknown_killed, t + len(unknown))
                if not any(c["rel_killed"] is None for c in useful + unknown) else None,
            "unknown_all_not_useful_false_kill_rate": ratio(killed, t) if not known_uncertain_kill else None,
            "unobserved_frozen_candidate_n": sum(row["frozen_candidate_n"] for row in details
                                                if row["status"] == "missing_evidence"),
            "false_keep_n": wrong, "delivered_n": len(delivered), "delivered_unknown_n": delivered_unknown,
            "false_keep_rate": ratio(wrong, len(delivered)),
            "false_keep_bounds": {"lower": ratio(wrong, len(delivered)),
                                  "upper": ratio(wrong + delivered_unknown, len(delivered))},
            "failed_or_missing_query_n": sum(row["status"] != "ok" for row in details),
            "empty_candidate_query_n": sum(not row["candidates"] for row in details),
            "status_counts": dict(Counter(row["status"] for row in details))}


def routing_summary(details):
    known = [row for row in details if row["routing_status"] == "known"]
    gates = [row for row in details if row["gate_status"] == "known"]
    return {"query_n": len(details), "known_routing_n": len(known),
            "unknown_routing_n": len(details) - len(known),
            "useful_source_hit_n": sum(row["useful_source_hit"] for row in known),
            "useful_source_hit_rate": ratio(sum(row["useful_source_hit"] for row in known), len(known)),
            "strict_old_formula_candidate_coverage_numerator": sum(row["candidate_coverage"] for row in known),
            "strict_old_formula_candidate_coverage_denominator": len(known),
            "strict_old_formula_candidate_coverage": statistics.mean(row["candidate_coverage"] for row in known) if known else None,
            "known_gate_n": len(gates), "unknown_gate_n": len(details) - len(gates),
            "gate_correct_n": sum(row["gate_correct"] for row in gates),
            "gate_accuracy": ratio(sum(row["gate_correct"] for row in gates), len(gates)),
            "deny_recall": None, "gate_specificity": None,
            "gate_limitation": "allow-only cohort cannot establish deny recall or specificity",
            "missing_or_failed_plan_n": sum(not row["plan_observed"] for row in details),
            "selected_source_counts": dict(Counter(source for row in details for source in row["selected_sources"]))}


def posts_summary(queries, records):
    details = []
    for q in queries:
        record = records[q["id"]]
        ev = record.get("evidence") if record else None
        count = ev.get("diagnostics", {}).get("judge", {}).get("posts") if ev else None
        recovered = ev.get("quality", {}).get("recovered") if ev else None
        limit = GATES["recovery_judge_posts_max"] if recovered is True else GATES["regular_judge_posts_max"]
        calls = record.get("latency", {}).get("pipeline", {}).get("calls") if record else None
        boundaries = sum(call.get("boundary") == "judge" and call.get("delegate_started") is True for call in calls) if calls is not None else None
        known = type(count) is int and count >= 0 and type(recovered) is bool
        details.append({"id": q["id"], "diagnostics_posts": count, "recovered": recovered,
                        "limit": limit if known else None, "within_limit": count <= limit if known else None,
                        "judge_delegate_boundaries": boundaries})
    return {"known_query_n": sum(d["within_limit"] is not None for d in details),
            "unknown_query_n": sum(d["within_limit"] is None for d in details),
            "violation_n": sum(d["within_limit"] is False for d in details),
            "diagnostics_posts_histogram": dict(Counter(str(d["diagnostics_posts"]) for d in details)),
            "counting_note": "diagnostics posts differ from observed judge delegate boundaries; neither is physical HTTP capture",
            "queries": details}


def sufficiency_summary(private_dir, queries, records, labels_path, tasks_path, labels_hash):
    base = {"status": "not_established", "brier": None, "brier_n": 0, "all_public_n": len(queries),
            "by_scope": {}, "reason": "independent sufficiency labels not supplied"}
    if labels_path is None:
        if tasks_path is not None or labels_hash is not None:
            raise ValueError("sufficiency labels required with tasks/hash")
        return base
    if not labels_hash:
        raise ValueError("sufficiency labels require explicit frozen labels hash")
    if tasks_path is None:
        raise ValueError("sufficiency tasks required for exact scope/hash binding")
    exact(sha256(labels_path), labels_hash, "sufficiency labels hash")
    index = json.loads((Path(private_dir) / "sufficiency-index.json").read_text())
    exact(sha256(tasks_path), index["tasks_sha256"], "sufficiency tasks hash")
    tasks = unique(jsonl(tasks_path), "task_id", "sufficiency tasks")
    labels = unique(jsonl(labels_path), "task_id", "sufficiency labels")
    bindings = unique(index["bindings"], "task_id", "sufficiency bindings")
    exact(set(tasks), set(bindings), "sufficiency tasks coverage")
    exact(set(labels), set(bindings), "sufficiency labels coverage")
    query_by_id = {q["id"]: q for q in queries}
    saved = json.loads((Path(private_dir) / "index.json").read_text())
    saved_records = {entry["id"]: entry["record"] for entry in saved["queries"]}
    details = []
    for task_id, binding in bindings.items():
        sid = binding["id"]
        if sid not in query_by_id:
            raise ValueError("sufficiency binding query outside frozen cohort")
        label, task = labels[task_id], tasks[task_id]
        review(label)
        tristate(label.get("enough"))
        rationale(label)
        if not isinstance(label.get("missing_aspects"), list) or not all(isinstance(x, str) for x in label["missing_aspects"]):
            raise ValueError("sufficiency missing_aspects must be a list of strings")
        exact(task["query"], query_by_id[sid]["query"], "sufficiency query")
        record = records[sid]
        # A missing raw record stays unscored; its frozen snapshot only validates
        # the already-issued packet instead of inventing a current observation.
        binding_record = record if record is not None else saved_records[sid]
        ev = binding_record.get("evidence") if binding_record else None
        prediction = (ev or {}).get("quality", {}).get("sufficiency", {})
        scope, probability = prediction.get("scope"), prediction.get("enough")
        exact(binding["prediction_scope"], scope, "sufficiency prediction scope")
        delivered = (ev or {}).get("results", [])
        assessed = [c for c in delivered if c.get("judgment") and "inj:suspect" not in c.get("flags", [])]
        if binding["scope_key"] == "full_delivery":
            candidates = delivered
            comparable = probability is not None and (scope == "delivered" or scope == "public_only" and len(assessed) == len(delivered))
        elif binding["scope_key"] == "public_assessed":
            candidates = assessed
            comparable = probability is not None and scope == "public_only" and len(assessed) != len(delivered)
        else:
            raise ValueError("unreconstructible sufficiency scope")
        exact(binding["candidate_urls"], [c["url"] for c in candidates], "sufficiency exact candidate URLs")
        texts = lambda cs: Counter(tuple(c[k] for k in ("title", "url", "snippet")) for c in cs)
        exact(texts(task["candidates"]), texts(candidates), "sufficiency exact candidate texts")
        exact(binding["matches_prediction"], comparable, "sufficiency scope match")
        if probability is not None and (not number(probability) or not 0 <= probability <= 1):
            raise ValueError("sufficiency prediction must be a finite probability")
        eligible = comparable and record is not None and not record.get("pipeline_error")
        reason = ("missing_evidence" if record is None else "missing_prediction" if probability is None
                  else "scope_mismatch" if not eligible else "unknown_gold" if label["enough"] is None else "scored")
        loss = (probability - int(label["enough"])) ** 2 if reason == "scored" else None
        q = query_by_id[sid]
        details.append({"task_id": task_id, "id": sid, "lang": q["lang"], "cohort": cohort(q),
                        "stratum": q["stratum"], "scope_key": binding["scope_key"],
                        "prediction_scope": scope, "eligible": eligible, "probability": probability,
                        "enough": label["enough"], "loss": loss, "reason": reason})
    def summarize(scope_rows, public_n=None):
        public_n = len(queries) if public_n is None else public_n
        losses = [d["loss"] for d in scope_rows if d["loss"] is not None]
        eligible = sum(d["eligible"] for d in scope_rows)
        return {"brier": statistics.mean(losses) if losses else None, "n": len(losses),
                "eligible_n": eligible, "coverage": ratio(len(losses), eligible),
                "all_public_n": public_n, "public_coverage": ratio(len(losses), public_n),
                "task_n": len(scope_rows), "missing_reasons": dict(Counter(d["reason"] for d in scope_rows if d["reason"] != "scored"))}
    full_delivery = summarize([d for d in details if d["scope_key"] == "full_delivery"])
    return {"status": "established_for_scored_scopes_only" if any(d["loss"] is not None for d in details) else "not_established",
            "brier_target": "full_delivery", "brier": full_delivery["brier"], "brier_n": full_delivery["n"],
            "eligible_n": full_delivery["eligible_n"],
            "all_public_n": len(queries), "coverage": full_delivery["coverage"],
            "by_scope": {scope: summarize([d for d in details if d["scope_key"] == scope])
                         for scope in sorted({d["scope_key"] for d in details})},
            "groups": {axis: {value: {
                scope: summarize([d for d in details if d[axis] == value and d["scope_key"] == scope],
                                 sum((cohort(q) if axis == "cohort" else q[axis]) == value for q in queries))
                for scope in sorted({d["scope_key"] for d in details})}
                for value in sorted({d[axis] for d in details})}
                for axis in ("lang", "cohort", "stratum")},
            "by_prediction_and_target_scope": {
                f"{prediction_scope}/{target}": summarize([d for d in details
                    if d["prediction_scope"] == prediction_scope and d["scope_key"] == target])
                for prediction_scope, target in sorted(
                    {(d["prediction_scope"], d["scope_key"]) for d in details}, key=str)},
            "labels_sha256": labels_hash, "tasks_sha256": index["tasks_sha256"],
            "binding_index_sha256": sha256(Path(private_dir) / "sufficiency-index.json"),
            "unknown_predictions": index["unknown_predictions"], "queries": details,
            "limitation": "same-scope scored cohort only; not full-delivery calibration for all public queries"}


def score_run(*, queries, run_dir, private_dir, routing_gold, protocol, preregistration,
              sufficiency_labels=None, sufficiency_tasks=None, sufficiency_labels_sha256=None,
              content_tasks=None, content_labels=None):
    run_dir, private_dir = Path(run_dir), Path(private_dir)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    registration = json.loads(Path(preregistration).read_text())
    index = json.loads((private_dir / "index.json").read_text())
    freeze = json.loads((private_dir / "content-freeze.json").read_text())
    content_tasks = Path(content_tasks or index["content_tasks_path"])
    content_labels = Path(content_labels or freeze["labels_path"])
    exact(sha256(content_tasks), index["content_tasks_sha256"], "content tasks hash")
    exact(sha256(content_tasks), freeze["content_tasks_sha256"], "frozen content tasks hash")
    exact(sha256(content_labels), freeze["labels_sha256"], "frozen content labels hash")
    for expected in (manifest["queries_sha256"], index["queries_sha256"], registration["queries_sha256"]):
        exact(sha256(queries), expected, "queries hash")
    exact(sha256(protocol), manifest["protocol_sha256"], "protocol hash")
    exact(sha256(protocol), registration["protocol_sha256"], "registered protocol hash")
    exact(sha256(preregistration), manifest["preregistration_sha256"], "preregistration hash")
    exact(manifest["acceptance_gates"], GATES, "acceptance gates")
    exact(registration["gates"], GATES, "registered acceptance gates")
    query_rows = jsonl(queries)
    query_by_id = unique(query_rows, "id", "query IDs")
    exact(sorted(query_by_id), sorted(manifest["selected_ids"]), "registered query IDs")
    exact(len(query_rows), manifest["query_count"], "query count")
    entries = unique(index["queries"], "id", "coordinator query IDs")
    exact(set(entries), set(query_by_id), "coordinator coverage")
    tasks = unique(jsonl(content_tasks), "task_id", "content tasks")
    labels = unique(jsonl(content_labels), "task_id", "content labels")
    expected_tasks = {entry["task_id"] for entry in entries.values() if "task_id" in entry}
    exact(set(tasks), expected_tasks, "content tasks coverage")
    exact(set(labels), expected_tasks, "content labels coverage")
    gold = unique(jsonl(routing_gold), "id", "routing gold IDs")
    exact(set(gold), set(query_by_id), "routing gold coverage")
    public = [q for q in query_rows if q["stratum"] not in {"sensitive_negative", "敏感负例"}]
    exact(len(public), manifest["public_query_count"], "public count")
    records, content_details, routing_details = {}, [], []
    routable = {source["id"] for source in manifest["inventory"] if source["routable"]}
    for q in public:
        sid, entry = q["id"], entries[q["id"]]
        record_path = run_dir / "evidence" / f"{sid}.json"
        record = None
        if record_path.exists():
            exact(sha256(record_path), entry["record_sha256"], f"evidence hash: {sid}")
            record = json.loads(record_path.read_text())
            exact(record, entry["record"], f"coordinator evidence: {sid}")
            exact(record["sample"], q, f"evidence query: {sid}")
        records[sid] = record
        ev = record.get("evidence") if record else None
        status = "missing_evidence" if record is None else record.get("pipeline_error") or ("ok" if ev else "missing_evidence")
        task, label = tasks[entry["task_id"]], labels[entry["task_id"]]
        exact(task["query"], q["query"], "content query")
        review(label)
        mapping = unique(entry["mapping"], "item_id", "opaque mappings")
        texts = unique(task["candidates"], "item_id", "opaque candidate texts")
        judgments = unique(label["candidate_labels"], "item_id", "candidate labels")
        exact(set(mapping), set(texts), "opaque text coverage")
        exact(set(mapping), set(judgments), "opaque label coverage")
        trace = record.get("selection_trace", []) if record else []
        candidates = trace[-1]["candidates"] if trace else []
        by_identity = unique(candidates, "cid", "final candidate cid")
        if record is not None:
            exact({(m["cid"], m["url"]) for m in mapping.values()},
                  {(c["cid"], c["url"]) for c in candidates}, "final cid+URL coverage")
        dropped = set(trace[-1].get("dropped_relevance", [])) if trace else set()
        if not dropped <= set(by_identity):
            raise ValueError("dropped relevance cid outside final round")
        delivered = (ev or {}).get("results", [])
        by_url = unique(candidates, "url", "final candidate URL")
        delivered_urls = {c["url"] for c in delivered}
        if not delivered_urls <= set(by_url):
            raise ValueError("delivered URL outside frozen final candidates")
        candidate_details = []
        for item_id, m in mapping.items():
            item = judgments[item_id]
            tristate(item.get("content_useful"))
            rationale(item)
            if record is None:
                continue  # No observed frozen outcome; not a zero-error candidate.
            candidate = by_identity[m["cid"]]
            exact(candidate["url"], m["url"], "candidate cid+URL")
            exact({k: candidate[k] for k in ("title", "url", "snippet")},
                  {k: texts[item_id][k] for k in ("title", "url", "snippet")}, "candidate text")
            rel = (candidate.get("judgment") or {}).get("rel")
            killed = False if m["cid"] not in dropped else rel < .5 if number(rel) else None
            candidate_details.append({"item_id": item_id, "cid": m["cid"], "url": m["url"],
                                      "content_useful": item["content_useful"], "rel": rel,
                                      "rel_killed": killed, "delivered": m["url"] in delivered_urls})
        meta = {"id": sid, "query": q["query"], "lang": q["lang"], "stratum": q["stratum"], "cohort": cohort(q)}
        content_details.append({**meta, "task_id": task["task_id"], "status": status,
                                "frozen_candidate_n": len(mapping), "candidates": candidate_details})
        g = gold[sid]
        exact(g["query"], q["query"], "routing query")
        known = g["expected_sources_status"] == "known"
        expected = g.get("expected_sources")
        if known and (not isinstance(expected, list) or not expected or len(set(expected)) != len(expected)
                      or not set(expected) <= routable):
            raise ValueError("known routing gold requires nonempty distinct routable candidate sources")
        if not known and expected is not None:
            raise ValueError("unknown routing gold must preserve null")
        semantics = g.get("expected_sources_semantics")
        if semantics != "useful candidate set; alternatives, not mandatory fan-out":
            raise ValueError("unrecognized routing gold semantics")
        plan = ev.get("plan") if ev else None
        selected = plan.get("sources", []) if plan else []
        overlap = len(set(selected) & set(expected)) if known else None
        predicted_gate = plan.get("gate") if plan else "missing_plan"
        predicted_gate = "allow" if predicted_gate is None else "sensitive" if predicted_gate == "sensitive_query" else predicted_gate
        routing_details.append({**meta, "routing_status": "known" if known else "unknown",
            "expected_sources": expected, "selected_sources": selected, "plan_observed": bool(plan),
            "useful_source_hit": bool(overlap) if known else None,
            "candidate_coverage": overlap / len(expected) if known else None,
            "gate_status": g["gate_status"], "expected_gate": g.get("expected_gate"),
            "predicted_gate": predicted_gate, "gate_correct": predicted_gate == g.get("expected_gate")})
    hashes = {"queries_sha256": sha256(queries), "protocol_sha256": sha256(protocol),
              "preregistration_sha256": sha256(preregistration), "manifest_sha256": sha256(manifest_path),
              "coordinator_index_sha256": sha256(private_dir / "index.json"),
              "content_freeze_sha256": sha256(private_dir / "content-freeze.json"),
              "content_tasks_sha256": index["content_tasks_sha256"], "content_labels_sha256": freeze["labels_sha256"],
              "routing_gold_sha256": sha256(routing_gold)}
    common = {"schema_version": 1, "hashes": hashes, "acceptance_gates": GATES,
              "final_pass": False, "user_audit_status": "pending", "user_audit_required_query_n": math.ceil(.2 * len(public)),
              "annotation_kind": "assistant annotations, not human-adjudicated gold",
              "final_pass_blockers": ["user query audit pending", "independence/exposure audit not established by hashes"]}
    content = {**common, "overall": content_summary(content_details),
               "groups": grouped(content_details, content_summary), "queries": content_details,
               "metric_definition": "final frozen round only: dropped_relevance AND saved rel < 0.5; exact query/cid/URL",
               "unknown_policy": "null never coerced to false; bounds optimize assignments, not merely the two uniform scenarios",
               "sufficiency": sufficiency_summary(private_dir, public, records, sufficiency_labels,
                                                   sufficiency_tasks, sufficiency_labels_sha256)}
    routing = {**common, "overall": routing_summary(routing_details),
               "groups": grouped(routing_details, routing_summary), "queries": routing_details,
               "semantics": "any useful candidate hit, NOT all-of required-source recall; initial selected plan only, not recovery",
               "strict_old_formula_note": "mean(|initial sources intersect candidate set| / |candidate set|) over known gold, failed/missing plan = 0; alternatives are not jointly mandatory; not a valid replacement acceptance claim",
               "source_recall_acceptance_established": False,
               "posts": posts_summary(public, records), "latency_summary": latency_summary(public, records, manifest),
               "safety": {"negative_n": len(query_rows) - len(public), "status": "not_established" if len(query_rows) == len(public) else "not_scored",
                          "reason": "public cohort cannot establish privacy/no-egress negative-test acceptance"}}
    return content, routing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--routing-gold", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--preregistration", type=Path, required=True)
    parser.add_argument("--sufficiency-labels", type=Path)
    parser.add_argument("--sufficiency-tasks", type=Path)
    parser.add_argument("--sufficiency-labels-sha256")
    parser.add_argument("--content-tasks", type=Path, help="Relocated byte-identical frozen packet")
    parser.add_argument("--content-labels", type=Path, help="Relocated byte-identical frozen labels")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = vars(parser.parse_args())
    output_dir = args.pop("output_dir")
    content, routing = score_run(**args)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("content.json", content), ("routing.json", routing)):
        path = output_dir / name
        if path.exists():
            raise ValueError(f"refuse to overwrite existing output: {path}")
    for name, value in (("content.json", content), ("routing.json", routing)):
        (output_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"content": str(output_dir / "content.json"), "routing": str(output_dir / "routing.json"),
                      "final_pass": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
