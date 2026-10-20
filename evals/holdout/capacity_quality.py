#!/usr/bin/env python3
"""Offline v3 capacity metrics over score_frozen's exact query/cid/URL mapping.

K is configured delivery capacity, never observed list length. Unknown bounds
range over counts of useful delivered/undelivered unknowns, not uniform labels.
No report produced here establishes fresh-holdout or overall acceptance.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from . import score_frozen
except ImportError:
    import score_frozen


def _bounds(values):
    values = list(values)
    return {"lower": min(values) if values else None,
            "upper": max(values) if values else None}


def query_quality(row, capacity=8):
    if type(capacity) is not int or capacity <= 0:
        raise ValueError("capacity must be a positive integer")
    candidates = row["candidates"]
    score_frozen.unique(candidates, "cid", "candidate cid")
    score_frozen.unique(candidates, "url", "candidate URL")
    for candidate in candidates:
        score_frozen.tristate(candidate["content_useful"])
        if type(candidate["delivered"]) is not bool:
            raise ValueError("delivered must be boolean")
    delivered = [c for c in candidates if c["delivered"]]
    if len(delivered) > capacity:
        raise ValueError("delivered count exceeds configured capacity")
    useful = sum(c["content_useful"] is True for c in candidates)
    kept = sum(c["content_useful"] is True for c in delivered)
    d = sum(c["content_useful"] is None for c in delivered)
    t = sum(c["content_useful"] is None and not c["delivered"] for c in candidates)
    # (capacity-normalized opportunity, missed useful results). These counts
    # exhaust all realizable assignments because delivered is a candidate subset.
    states = {(min(capacity, useful + x + y), min(capacity, useful + x + y) - kept - x)
              for x in range(d + 1) for y in range(t + 1)}
    blocked = row["status"] != "ok"
    positive = [(den, loss) for den, loss in states if den]
    loss_bounds = _bounds(loss / den for den, loss in positive) if not blocked else _bounds([])
    precision = {"lower": score_frozen.ratio(kept, len(delivered)),
                 "upper": score_frozen.ratio(kept + d, len(delivered))} if not blocked else _bounds([])
    return {"id": row["id"], "status": row["status"], "capacity": capacity,
            "candidate_n": len(candidates), "delivered_n": len(delivered),
            "known_useful_n": useful, "delivered_known_useful_n": kept,
            "delivered_unknown_n": d, "undelivered_unknown_n": t,
            "zero_useful_possible": any(den == 0 for den, _ in states),
            "zero_useful_certain": not positive,
            "blocked_not_applicable": blocked or not positive,
            "loss": loss_bounds["lower"] if not d + t else None,
            "loss_bounds": loss_bounds, "precision": precision["lower"],
            "precision_bounds": precision,
            "_states": states if not blocked else {(0, 0)}}


def _aggregate_bounds(rows, macro):
    # Dynamic programming over attainable denominator totals avoids 2^N label
    # assignments. At each total retain only extreme numerator sums.
    totals = {0: (0.0, 0.0)}
    for row in rows:
        next_totals = {}
        options = {(int(den > 0), loss / den if den else 0) if macro else (den, loss)
                   for den, loss in row["_states"]}
        for total, (lower, upper) in totals.items():
            for denominator, numerator in options:
                key = total + denominator
                lo, hi = lower + numerator, upper + numerator
                previous = next_totals.get(key, (lo, hi))
                next_totals[key] = (min(previous[0], lo), max(previous[1], hi))
        totals = next_totals
    return {"lower": min((lo / den for den, (lo, _) in totals.items() if den), default=None),
            "upper": max((hi / den for den, (_, hi) in totals.items() if den), default=None),
            "applicable_denominator_bounds": {"lower": min(totals), "upper": max(totals)}}


def score_capacity(content, capacity=8):
    rows = [query_quality(row, capacity) for row in content["queries"]]
    observed = [r for r in rows if r["status"] == "ok"]
    delivered = sum(r["delivered_n"] for r in observed)
    kept = sum(r["delivered_known_useful_n"] for r in observed)
    unknown = sum(r["delivered_unknown_n"] for r in observed)
    macro = _aggregate_bounds(rows, True)
    pooled = _aggregate_bounds(rows, False)
    precision = {"lower": score_frozen.ratio(kept, delivered),
                 "upper": score_frozen.ratio(kept + unknown, delivered)}
    macro_precision = {
        side: (sum(r["precision_bounds"][side] for r in observed if r["delivered_n"])
               / sum(bool(r["delivered_n"]) for r in observed)) if delivered else None
        for side in ("lower", "upper")}
    coverage = {"expected_query_n": len(rows), "observed_ok_query_n": len(observed),
                "failed_or_missing_query_n": len(rows) - len(observed),
                "certain_zero_useful_query_n": sum(r["zero_useful_certain"] for r in observed),
                "possibly_zero_useful_query_n": sum(r["zero_useful_possible"] for r in observed),
                "empty_delivery_query_n": sum(not r["delivered_n"] for r in observed),
                "unknown_content_query_n": sum(r["delivered_unknown_n"] + r["undelivered_unknown_n"] > 0 for r in observed)}
    blocked = (len(observed) != len(rows) or any(r["zero_useful_possible"] for r in observed)
               or any(not r["delivered_n"] or r["delivered_unknown_n"] + r["undelivered_unknown_n"]
                      for r in observed)
               or macro_precision["lower"] is None or macro["upper"] is None)
    metric_status = "blocked_not_applicable" if blocked else (
        "within_thresholds" if macro["upper"] <= .10 and macro_precision["lower"] >= .85
        else "outside_or_unresolved_thresholds")
    stage_counts = {}
    for stage, predicate in (("rel", lambda c: c["rel_killed"] is True),
                             ("not_delivered", lambda c: not c["delivered"])):
        subset = [c for row in content["queries"] for c in row["candidates"] if predicate(c)]
        stage_counts[stage] = {"candidate_n": len(subset),
                               "known_useful_n": sum(c["content_useful"] is True for c in subset),
                               "unknown_useful_n": sum(c["content_useful"] is None for c in subset)}
    return {"schema_version": 3, "metric": "capacity_normalized_deliverable_useful_loss",
            "capacity": capacity, "hashes": content.get("hashes", {}),
            "loss_formula": "(min(K,U)-u)/min(K,U); U=0 undefined; same query candidate set",
            "primary_aggregation": "query_macro", "macro_loss_bounds": macro,
            "pooled_loss_bounds": pooled, "macro_precision_bounds": macro_precision,
            "pooled_precision_bounds": precision,
            "delivery_precision": macro_precision["lower"], "coverage": coverage,
            "thresholds": {"macro_loss_max": .10, "delivery_precision_min": .85},
            "metric_status": metric_status, "final_pass": False,
            "acceptance_status": "not_established",
            "acceptance_note": "Offline development evidence only; exposed old87 is not fresh formal holdout. Unknown bounds do not establish coverage or independent acceptance.",
            "candidate_loss_counts": stage_counts,
            "safety_topk_attribution": "unknown: score_frozen does not expose disjoint safety/topk decisions; not_delivered includes all causes",
            "legacy_rel_report": content.get("overall", {}),
            "queries": [{k: v for k, v in row.items() if k != "_states"} for row in rows]}


def score_run(*, capacity=8, **kwargs):
    content, _ = score_frozen.score_run(**kwargs)
    try:
        from . import policy_latency_tradeoffs as stages
    except ImportError:
        import policy_latency_tradeoffs as stages
    stage_rows = []
    # score_frozen uses a URL set for delivery. Check the raw list before using
    # that mapping so duplicate deliveries cannot silently inflate precision.
    for row in content["queries"]:
        path = Path(kwargs["run_dir"]) / "evidence" / f'{row["id"]}.json'
        if path.exists():
            record = json.loads(path.read_text())
            delivered = (record.get("evidence") or {}).get("results", [])
            score_frozen.unique(delivered, "url", "delivered URL")
            with_cid = [c for c in delivered if "cid" in c]
            score_frozen.unique(with_cid, "cid", "delivered cid")
            mapping = {c["url"]: c["cid"] for c in row["candidates"]}
            for candidate in with_cid:
                score_frozen.exact(candidate["cid"], mapping[candidate["url"]], "delivered cid+URL")
            trace = record.get("selection_trace", [])
            raw = trace[-1]["candidates"] if trace else []
            gate = ((record.get("evidence") or {}).get("plan") or {}).get("gate")
            selected = stages.select(raw, "current_rel_05",
                                     trace[-1].get("dropped_injection", []) if trace else [], gate)
            score_frozen.exact(
                [next(c["url"] for c in raw if c["cid"] == cid) for cid in selected["top8"]],
                [c["url"] for c in delivered], "current frozen delivery reconstruction")
            stage_rows.append({**row, **selected})
        else:
            stage_rows.append({**row, **stages.select([], "current_rel_05")})
    result = score_capacity(content, capacity)
    result["all_candidate_stage_diagnostics"] = stages.summarize(stage_rows)
    result["safety_topk_attribution"] = "frozen current policy reconstruction; all candidates retained in denominator"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("queries", "run-dir", "private-dir", "routing-gold", "protocol", "preregistration"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("content-tasks", "content-labels"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--capacity", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = vars(parser.parse_args())
    output = args.pop("output")
    report = score_run(**args)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
