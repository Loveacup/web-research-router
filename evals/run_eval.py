#!/usr/bin/env python3
"""WRR 7 live evaluation; run, blind-label, then summarize without new API calls.

Run: .venv/bin/python evals/run_eval.py run
Score: .venv/bin/python evals/run_eval.py summarize --run-dir evals/runs/<ts>
Credentials must already be loaded; never persisted. No changes to wrr/.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import statistics
import sys
import time
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wrr import config
from wrr.core import rubric
from wrr.core.inventory import build_inventory
from wrr.core.judge import JudgeError, resolve_judge
from wrr.core.pipeline import search
from wrr.core.runtime import prepare_environment
from wrr.core.sources import build_adapters, _failure_status
from wrr.engines._fusion import canonical_url
from wrr.engines.exa import ExaEngine
from wrr.schemas import SearchOptions
from wrr.search_filter import _ENTROPY, _normalize

_TRACE: ContextVar[list | None] = ContextVar("eval_trace", default=None)
_ORIGINAL_SELECT = rubric.select_policy


def traced_select(*args, **kwargs):
    outcome = _ORIGINAL_SELECT(*args, **kwargs)
    trace = _TRACE.get()
    if trace is not None:
        trace.append({"candidates": [dict(c.to_dict(), cid=c.cid) for c in args[0]],
                      "dropped_relevance": list(outcome.dropped_relevance),
                      "dropped_injection": list(outcome.dropped_injection)})
    return outcome


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sensitive(row):
    return row["stratum"] in {"sensitive_negative", "敏感负例"}


def blind_form(row, trace, evidence):
    # Only content and opaque ids reach the annotator; no flags, rel, enough,
    # plan, source ids, dropped reasons or baseline results.
    candidates = trace[-1]["candidates"] if trace else []
    delivered = {canonical_url(c["url"]) for c in evidence.get("results", [])}
    return {"id": row["id"], "query": row["query"],
            "candidates": [{"cid": c["cid"], "title": c["title"], "url": c["url"],
                            "snippet": c["snippet"]} for c in candidates],
            "delivered_ids": [c["cid"] for c in candidates if canonical_url(c["url"]) in delivered]}


class GuardJudge:
    """Fail a negative test before any judge content can leave this process."""
    def __init__(self, backend, attempts):
        self.backend = backend
        self.attempts = attempts

    async def judge(self, *args, **kwargs):
        self.attempts["judge"] += 1
        raise JudgeError("unavailable", posts=0)


class GuardSource:
    def __init__(self, source_id, attempts):
        self.source_id = source_id
        self.attempts = attempts

    async def search(self, *args, **kwargs):
        self.attempts["sources"].append(self.source_id)
        raise RuntimeError("negative_test_egress_blocked")


def negative_safety(evidence, attempts, local_ids):
    try:
        assert evidence is not None
        assert evidence["diagnostics"]["judge"]["posts"] == 0
        assert evidence["plan"]["judge"] == "none" or evidence["plan"]["judge"].startswith("skipped")
        assert attempts["judge"] == 0
        assert not (set(attempts["sources"]) - local_ids)
    except AssertionError:
        return False
    return True


def paired_schedule(rows, seed):
    """Randomize strata/query order and assign globally balanced AB/BA arms."""
    rng = random.Random(seed)
    groups, negatives = {}, []
    for row in sorted(rows, key=lambda row: row["id"]):
        if sensitive(row):
            negatives.append(row)
        else:
            groups.setdefault(row["stratum"], []).append(row)
    blocks = list(groups.values())
    for block in blocks:
        rng.shuffle(block)
    rng.shuffle(blocks)
    ordered = [row for block in blocks for row in block]
    orders = ["AB", "BA"] * (len(ordered) // 2)
    if len(ordered) % 2:
        orders.append(rng.choice(("AB", "BA")))
    rng.shuffle(orders)
    return list(zip(ordered, orders)) + [(row, "A") for row in negatives]


async def timed_call(calls, name, delegate, outbound, *args, **kwargs):
    """Observe harness queue/delegate intervals, never provider-internal time."""
    started = time.perf_counter()
    acquired = None
    status = "ok"
    try:
        if outbound is not None:
            await outbound.acquire()
        acquired = time.perf_counter()
        return await delegate(*args, **kwargs)
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception as exc:
        status = _failure_status(exc).value
        raise
    finally:
        ended = time.perf_counter()
        if acquired is not None and outbound is not None:
            outbound.release()
        calls.append({
            "boundary": name, "status": status, "delegate_started": acquired is not None,
            "queue_start_monotonic_s": started, "acquired_monotonic_s": acquired or "unknown",
            "service_end_monotonic_s": ended,
            "queue_s": (acquired or ended) - started if outbound is not None else 0.0,
            "service_s": ended - acquired if acquired is not None else "unknown",
            "wall_s": ended - started,
            "provider_queue_s": "unknown", "provider_service_s": "unknown",
        })


def arm_timing(started, elapsed, calls, isolated, *, direct=False):
    queue = 0.0 if isolated else calls[0]["queue_s"] if direct and calls else "unknown"
    return {"started_monotonic_s": started, "ended_monotonic_s": started + elapsed,
            "wall_s": elapsed, "queue_s": queue,
            "service_s": elapsed - queue if queue != "unknown" else "unknown",
            "queue_scope": "harness only; legacy query-admission wait excluded from wall",
            "service_scope": "arm coroutine execution; includes orchestration and internal waits",
            "provider_queue_s": "unknown", "provider_service_s": "unknown", "calls": calls}


def holdout_manifest(args, rows):
    """Validate frozen holdout registration before initializing live execution."""
    protocol = getattr(args, "protocol", None)
    registration_path = getattr(args, "preregistration", None)
    if protocol is None and registration_path is None:
        return {}
    if protocol is None or registration_path is None:
        raise ValueError("formal holdout requires both --protocol and --preregistration")
    from evals.holdout.prepare_annotations import GATES

    registration = json.loads(registration_path.read_text())
    if registration.get("holdout_protocol_version") == 3:
        from evals.holdout.registration_v3 import holdout_manifest as v3_manifest
        return v3_manifest(args, rows, registration)
    protocol_hash = hashlib.sha256(protocol.read_bytes()).hexdigest()
    query_hash = hashlib.sha256(args.queries.read_bytes()).hexdigest()
    if registration.get("protocol_sha256") != protocol_hash:
        raise ValueError("protocol changed after preregistration")
    if registration.get("queries_sha256") != query_hash:
        raise ValueError("queries changed after preregistration")
    if registration.get("gates") != GATES or registration.get("holdout_protocol_version") != 1:
        raise ValueError("holdout acceptance gates or protocol version changed")
    real = [row for row in rows if not sensitive(row) and row["synthetic"] is False
            and row["source"] in {"omp", "hermes"}]
    normalized = [" ".join(row["query"].casefold().split()) for row in rows]
    ids = [row["id"] for row in rows]
    if (len(real) < GATES["real_independent_queries_min"] or len(set(ids)) != len(ids)
            or len(set(normalized)) != len(normalized)):
        raise ValueError("formal holdout requires at least 60 distinct real public queries")
    if args.ids or registration.get("selected_ids") != sorted(ids):
        raise ValueError("formal holdout cannot use partial or changed query IDs")
    expected = {"latency_mode": "isolated-paired", "query_concurrency": 1,
                "harness_retries": 0, "repetitions": 1, "deadline_s": 15, "count": 8}
    if (any(registration.get(key) != value for key, value in expected.items())
            or getattr(args, "latency_mode", "legacy") != "isolated-paired"
            or args.concurrency != 1
            or registration.get("seed") != getattr(args, "seed", 20261020)
            or registration.get("real_public_query_count") != len(real)):
        raise ValueError("formal holdout execution differs from preregistration")
    return {"holdout_protocol_version": 1, "protocol_sha256": protocol_hash,
            "preregistration_sha256": hashlib.sha256(registration_path.read_bytes()).hexdigest(),
            "acceptance_gates": dict(GATES), "final_pass": False,
            "user_audit_status": "pending"}


async def run(args):
    rows = jsonl(args.queries)
    if args.ids:
        rows = [r for r in rows if r["id"] in args.ids.split(",")]
    holdout = holdout_manifest(args, rows)
    mode = getattr(args, "latency_mode", "legacy")
    isolated = mode == "isolated-paired"
    seed = getattr(args, "seed", 20261020)
    if isolated and args.concurrency != 1:
        raise ValueError("isolated-paired requires --concurrency 1")
    schedule = paired_schedule(rows, seed) if isolated else [(row, "AB") for row in rows]
    run_dir = args.run_dir or ROOT / "evals/runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    identity, snapshot, env = prepare_environment()
    inventory = build_inventory(runtime=identity, env=snapshot)
    adapters = build_adapters(inventory, env=env)
    judge = resolve_judge(env=env)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "queries_sha256": hashlib.sha256(args.queries.read_bytes()).hexdigest(),
                "queries": str(args.queries.resolve()), "query_count": len(rows),
                "public_query_count": sum(not sensitive(row) for row in rows),
                "selected_ids": sorted(row["id"] for row in rows),
                "query_concurrency": args.concurrency, "outbound_concurrency": None if isolated else 2,
                "latency_mode": mode, "latency_method_version": 1,
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "seed": seed if isolated else None,
                "pair_schedule": [{"id": row["id"], "order": order} for row, order in schedule],
                "arm_mapping": {"A": "pipeline", "B": "direct_exa"},
                "order_balance_scope": "global; query order randomized within shuffled stratum blocks" if isolated else "fixed AB",
                "external_semaphore": "none" if isolated else "shared across pipeline sources, judge and baseline",
                "warmup": "none; existing delegate connection/cache behavior unchanged; provider cache unknown",
                "repetitions": 1, "harness_retries": 0,
                "timing_scope": "monotonic harness wall/queue/delegate execution; provider queue/service unknown",
                "failure_policy": "all attempted walls and pairs retained; failure-penalized tail also reported",
                "deadline_s": 15, "count": 8, "judge_backend": judge.backend,
                "inventory": [s.to_dict() for s in inventory],
                "negative_test_policy": "all adapters and judge denied before execution; attempted external egress fails safety; local attempts are permitted"}
    manifest.update(holdout)
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        for key in ("protocol_sha256", "preregistration_sha256", "acceptance_gates"):
            if previous.get(key) != manifest.get(key):
                raise ValueError(f"cannot resume with changed {key}; use a new run directory")
        if previous.get("latency_mode", "legacy") != mode:
            raise ValueError("cannot resume with changed latency_mode; use a new run directory")
        keys = ("queries_sha256", "query_count", "query_concurrency", "deadline_s", "count", "judge_backend", "inventory")
        for key in keys + (("seed", "pair_schedule", "selected_ids", "harness_sha256") if isolated else ()):
            if previous[key] != manifest[key]:
                raise ValueError(f"cannot resume with changed {key}; use a new run directory")
    else:
        dump(manifest_path, manifest)
    slots = asyncio.Semaphore(args.concurrency)
    outbound = None if isolated else asyncio.Semaphore(2)
    rubric.select_policy = traced_select

    async def one(row, order):
        async with slots:
            target = run_dir / "evidence" / f"{row['id']}.json"
            if target.exists():
                print(f"{row['id']} resumed", flush=True)
                return
            trace, pipeline_calls, baseline_calls = [], [], []
            attempts = {"judge": 0, "sources": []}
            requested_count = holdout.get("request_counts", {}).get(row["id"], 8)
            neg = sensitive(row)
            evidence, error, baseline = None, None, None
            elapsed = 0.0
            latency = {}

            class TimedSource:
                def __init__(self, sid, delegate):
                    self.sid, self.delegate = sid, delegate

                async def search(self, *a, **kw):
                    return await timed_call(pipeline_calls, f"source:{self.sid}",
                                            self.delegate.search, outbound, *a, **kw)

            class TimedJudge:
                def __init__(self):
                    self.backend = judge.backend

                async def judge(self, *a, **kw):
                    return await timed_call(pipeline_calls, "judge", judge.judge, outbound, *a, **kw)

            async def pipeline_arm():
                nonlocal evidence, error, elapsed
                token = _TRACE.set(trace)
                active_judge = GuardJudge(judge.backend, attempts) if neg else TimedJudge()
                active_adapters = {
                    sid: GuardSource(sid, attempts) if neg else TimedSource(sid, adapter)
                    for sid, adapter in adapters.items()
                }
                started = time.perf_counter()
                try:
                    evidence = (await search(row["query"], inventory=inventory, adapters=active_adapters,
                                             judge=active_judge, deadline_s=15, count=requested_count)).to_dict()
                except Exception as exc:
                    error = _failure_status(exc).value
                finally:
                    elapsed = time.perf_counter() - started
                    _TRACE.reset(token)
                    latency["pipeline"] = arm_timing(started, elapsed, pipeline_calls, isolated)

            async def baseline_arm():
                nonlocal baseline
                started = time.perf_counter()
                try:
                    with config.source_environment(env):
                        results = await timed_call(baseline_calls, "direct_exa", ExaEngine().search,
                                                   outbound, SearchOptions(query=row["query"], count=requested_count))
                    baseline = {"status": "ok", "wall_s": time.perf_counter() - started,
                                "results": [{"title": x.title, "url": x.url, "snippet": x.snippet} for x in results]}
                except Exception as exc:
                    baseline = {"status": _failure_status(exc).value, "wall_s": time.perf_counter() - started, "results": []}
                latency["baseline"] = arm_timing(started, baseline["wall_s"], baseline_calls, isolated, direct=True)

            for arm in order if not neg else "A":
                await (pipeline_arm() if arm == "A" else baseline_arm())
            safety = None
            if neg:
                local_ids = {s.id for s in inventory if s.privacy_class.value == "local"}
                safety = negative_safety(evidence, attempts, local_ids)
            record = {"sample": row, "pipeline_wall_s": elapsed, "pipeline_error": error,
                      "evidence": evidence, "selection_trace": trace, "baseline": baseline,
                      "latency_mode": mode, "pair_order": order if not neg else "A", "latency": latency,
                      "negative_safety_pass": safety, "blocked_egress_attempts": attempts}
            dump(target, record)
            dump(run_dir / "blind" / f"{row['id']}.json", blind_form(row, trace, evidence or {}))
            print(f"{row['id']} saved ({elapsed:.2f}s; baseline={'skipped' if neg else baseline['status']})", flush=True)

    try:
        if isolated:
            for row, order in schedule:
                await one(row, order)
        else:
            await asyncio.gather(*(one(row, order) for row, order in schedule))
    finally:
        rubric.select_policy = _ORIGINAL_SELECT
    records = [json.loads(path.read_text()) for path in sorted((run_dir / "evidence").glob("*.json"))]
    dump(run_dir / "latency-summary.json", latency_summary(records, manifest))
    print(str(run_dir), flush=True)


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def failure_penalized_p95(success_values, failed_n):
    """Treat failures/missing attempts as +infinity without non-JSON floats."""
    n = len(success_values) + failed_n
    if not n:
        return None
    values = sorted(success_values)
    index = (n - 1) * .95
    lower, upper = int(index), min(int(index) + 1, n - 1)
    if upper >= len(values):
        return "failed_or_unknown"
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def latency_summary(records, manifest):
    """Label-free attempt accounting; never drop failures from latency tails."""
    public = [record for record in records if not sensitive(record["sample"])]
    expected = manifest.get("public_query_count", len(public))
    walls, baseline_walls, increments, success_increments = [], [], [], []
    pipeline_statuses, baseline_statuses, dependencies = Counter(), Counter(), Counter()
    failed_pairs = dependency_failure_queries = 0
    for record in public:
        pipeline_status = record["pipeline_error"] or "ok"
        pipeline_statuses[pipeline_status] += 1
        walls.append(record["pipeline_wall_s"])
        calls = record.get("latency", {}).get("pipeline", {}).get("calls", [])
        call_failures = [call["status"] for call in calls if call["status"] != "ok"]
        dependencies.update(call_failures)
        dependency_failure_queries += bool(call_failures)
        baseline = record.get("baseline")
        if baseline is None:
            continue
        baseline_statuses[baseline["status"]] += 1
        baseline_walls.append(baseline["wall_s"])
        increment = record["pipeline_wall_s"] - baseline["wall_s"]
        increments.append(increment)
        if pipeline_status == "ok" and baseline["status"] == "ok" and not call_failures:
            success_increments.append(increment)
        else:
            failed_pairs += 1
    missing_pairs = max(0, expected - len(increments))
    return {
        "latency_method_version": 1, "latency_mode": manifest.get("latency_mode", "legacy"),
        "seed": manifest.get("seed"), "expected_public_n": expected,
        "recorded_public_n": len(public), "pipeline_wall_n": len(walls),
        "baseline_wall_n": len(baseline_walls), "paired_latency_n": len(increments),
        "missing_pair_n": missing_pairs, "failed_pair_n": failed_pairs,
        "pipeline_statuses": dict(pipeline_statuses), "baseline_statuses": dict(baseline_statuses),
        "pipeline_dependency_failures": dict(dependencies),
        "pipeline_dependency_failure_query_n": dependency_failure_queries,
        "observed_wall_policy": "all attempted outcomes, including failures/timeouts; not success latency",
        "paired_policy": "all observed pairs, including failed arms; no success-only filtering",
        "pipeline_observed_p50_s": percentile(walls, .5),
        "pipeline_observed_p95_s": percentile(walls, .95),
        "baseline_observed_p50_s": percentile(baseline_walls, .5),
        "baseline_observed_p95_s": percentile(baseline_walls, .95),
        "increment_observed_p50_s": percentile(increments, .5),
        "increment_observed_p95_s": percentile(increments, .95),
        "increment_failure_penalized_p95_s": failure_penalized_p95(
            success_increments, failed_pairs + missing_pairs),
        "failure_penalty": "failed/missing pairs rank as +infinity; source/judge boundary failures included",
        "performance_pass_claim": False,
        "interpretation": "descriptive single-observation probe; no formal p95 acceptance claim",
        "provider_queue_s": "unknown", "provider_service_s": "unknown",
    }


def ratio(a, b):
    return a / b if b else None


def has_entropy_match(text):
    return any(any(ch.isupper() for ch in match.group())
               and any(ch.islower() for ch in match.group())
               and any(ch.isdigit() for ch in match.group())
               for match in _ENTROPY.finditer(_normalize(text)))


def full_delivery_scope(evidence):
    """Match the assessed content to the full-delivery annotation target.

    public_only is conservative: excluded candidates outside the delivered
    top-k do not change the assessed delivery. Raw recovery sufficiency and
    all_rejected are different targets and must not be pooled with this gold.
    """
    scope = evidence["quality"]["sufficiency"]["scope"]
    return scope in {"delivered", "public_only"} and all(
        c["judgment"] and "inj:suspect" not in c["flags"]
        for c in evidence["results"]
    )


def score(records, labels):
    recalls, briers, mixed_briers, walls, baselines, increments = [], [], [], [], [], []
    scoped_briers = {}
    useful = killed = delivered = wrong = candidate_count = flagged = flagged_useful = 0
    recovered = recovery_new_useful = enough_n = timeout_queries = 0
    brier_scope_mismatch_n = 0
    entropy_visible = entropy_path = delivered_flagged = 0
    posts, regular_posts, failures, source_statuses = Counter(), Counter(), Counter(), Counter()
    gate_correct = gate_n = 0
    negatives = [r for r in records if sensitive(r["sample"])]
    public = [r for r in records if not sensitive(r["sample"])]
    detail = []
    for record in public:
        sid = record["sample"]["id"]
        label = labels[sid]
        ev = record["evidence"]
        walls.append(record["pipeline_wall_s"])
        base = record["baseline"]
        if base:
            baselines.append(base["wall_s"])
            if base["status"] == "ok" or record.get("latency_mode") == "isolated-paired":
                increments.append(record["pipeline_wall_s"] - base["wall_s"])
            if base["status"] != "ok":
                failures[f"baseline:{base['status']}"] += 1
        if ev is None:
            failures[f"pipeline:{record['pipeline_error']}"] += 1
            timeout_queries += record["pipeline_error"] == "timeout"
            if label["expected_sources"]:
                recalls.append(0.0)
            detail.append({"id": sid, "error": record["pipeline_error"]})
            continue
        gold = set(label["expected_sources"])
        recall = ratio(len(set(ev["plan"]["sources"]) & gold), len(gold))
        if recall is not None:
            recalls.append(recall)
        gate_n += 1
        predicted_gate = "sensitive" if ev["plan"]["gate"] == "sensitive_query" else ev["plan"]["gate"]
        gate_correct += predicted_gate == label["gate"]
        diagnostics = ev["diagnostics"]["judge"]
        posts[diagnostics["posts"]] += 1
        if not ev["quality"]["recovered"] and ev["plan"]["gate"] is None:
            regular_posts[diagnostics["posts"]] += 1
        timed_out = False
        for failure in diagnostics["failures"]:
            failures[f"judge:{failure}"] += 1
            timed_out |= failure == "timeout"
        for source_run in ev["runs"]:
            status = source_run["status"]
            source_statuses[f"{source_run['source_id']}:{status}"] += 1
            if status not in {"ok", "empty"}:
                failures[f"source:{status}"] += 1
            timed_out |= status == "timeout"
        timeout_queries += timed_out
        trace = record["selection_trace"]
        candidates = trace[-1]["candidates"] if trace else []
        by_cid = {item["cid"]: item for item in label["candidate_labels"]}
        by_url = {canonical_url(c["url"]): c for c in candidates}
        dropped = set(trace[-1]["dropped_relevance"]) if trace else set()
        for c in candidates:
            is_useful = by_cid[c["cid"]]["useful"]
            useful += is_useful
            killed += is_useful and c["cid"] in dropped and c["judgment"].get("rel", 1) < 0.5
            candidate_count += 1
            is_flagged = "unjudged:sensitive" in c["flags"]
            flagged += is_flagged
            flagged_useful += is_flagged and is_useful
            if is_flagged:
                entropy_visible += has_entropy_match(" ".join(c[k] for k in ("title", "url", "snippet")))
                entropy_path += has_entropy_match(unquote(urlsplit(c["url"]).path))
        for c in ev["results"]:
            delivered += 1
            delivered_flagged += "unjudged:sensitive" in c["flags"]
            wrong += not by_cid[by_url[canonical_url(c["url"])]["cid"]]["useful"]
        enough = ev["quality"]["sufficiency"]["enough"]
        scope = ev["quality"]["sufficiency"]["scope"]
        comparable = full_delivery_scope(ev)
        if enough is not None:
            loss = (enough - int(label["delivered_enough"])) ** 2
            mixed_briers.append(loss)
            scoped_briers.setdefault(scope, []).append(loss)
            if comparable:
                briers.append(loss)
                enough_n += 1
            else:
                brier_scope_mismatch_n += 1
        newly_useful = []
        if ev["quality"]["recovered"]:
            recovered += 1
            # Recovery is a new source round. A new URL must be absent from
            # initial runs, and have a useful human label after recovery.
            initial_ids = set(ev["plan"]["sources"])
            for c in candidates:
                if not any(x["source_id"] in initial_ids for x in c["contributions"]) and by_cid[c["cid"]]["useful"]:
                    newly_useful.append(c["cid"])
            recovery_new_useful += bool(newly_useful)
        detail.append({"id": sid, "source_recall": recall, "delivered_enough": label["delivered_enough"],
                       "enough": enough, "enough_scope": scope, "brier_comparable": comparable,
                       "recovery_new_useful": newly_useful})
    privacy_negatives = [r for r in negatives if labels[r["sample"]["id"]].get("safety_contract") == "no_external_egress"]
    return {"n_public": len(public), "n_negative": len(negatives),
            "n_real": sum(not r["sample"]["synthetic"] for r in records),
            "n_synthetic": sum(r["sample"]["synthetic"] for r in records),
            "source_recall": statistics.mean(recalls) if recalls else None, "source_recall_n": len(recalls),
            "gate_accuracy": ratio(gate_correct, gate_n), "gate_correct_n": gate_correct, "gate_n": gate_n,
            "false_kill_rate": ratio(killed, useful), "false_kill_n": killed, "useful_n": useful,
            "false_keep_rate": ratio(wrong, delivered), "false_keep_n": wrong, "delivered_n": delivered,
            "brier": statistics.mean(briers) if briers else None, "brier_n": enough_n,
            "brier_target": "full_delivery_realized_scope",
            "brier_scope_mismatch_n": brier_scope_mismatch_n,
            "brier_legacy_mixed_scope": statistics.mean(mixed_briers) if mixed_briers else None,
            "brier_legacy_mixed_scope_n": len(mixed_briers),
            "brier_by_declared_scope": {scope: {"n": len(losses), "brier": statistics.mean(losses)}
                                      for scope, losses in sorted(scoped_briers.items())},
            "pipeline_p50_s": percentile(walls, .5), "pipeline_p95_s": percentile(walls, .95),
            "baseline_p50_s": percentile(baselines, .5), "baseline_p95_s": percentile(baselines, .95),
            "increment_p50_s": percentile(increments, .5), "increment_p95_s": percentile(increments, .95),
            "baseline_wall_n": len(baselines),
            "paired_latency_n": len(increments), "judge_posts": dict(sorted(posts.items())),
            "regular_judge_posts": dict(sorted(regular_posts.items())), "failures": dict(failures),
            "source_statuses": dict(source_statuses), "timeout_query_rate": ratio(timeout_queries, len(public)),
            "recovery_rate": ratio(recovered, len(public)), "recovery_n": recovered,
            "recovery_new_useful_n": recovery_new_useful, "recovery_new_useful_rate": ratio(recovery_new_useful, recovered),
            "sensitive_flag_rate": ratio(flagged, candidate_count), "sensitive_flag_n": flagged,
            "sensitive_flag_useful_n": flagged_useful, "candidate_n": candidate_count,
            "sensitive_entropy_visible_n": entropy_visible,
            "sensitive_entropy_public_path_n": entropy_path,
            "sensitive_entropy_public_path_rate": ratio(entropy_path, candidate_count),
            "delivered_sensitive_flag_n": delivered_flagged,
            "negative_safety_pass_n": sum(r["negative_safety_pass"] is True for r in negatives),
            "privacy_negative_n": len(privacy_negatives),
            "privacy_negative_safety_pass_n": sum(r["negative_safety_pass"] is True for r in privacy_negatives),
            "instruction_only_negative_n": sum(labels[r["sample"]["id"]].get("safety_contract") == "instruction_only_robustness"
                                               for r in negatives),
            "delivered_enough_rate": ratio(sum(labels[r["sample"]["id"]]["delivered_enough"] for r in public), len(public)),
            "details": detail}


def validate_source_labels(rows, records):
    """Bind partial overlay annotations to exact frozen (query, cid, URL) keys."""
    frozen = {}
    for record in records:
        sid = record["sample"]["id"]
        if sid in frozen:
            raise ValueError(f"duplicate evidence id: {sid}")
        trace = record["selection_trace"]
        candidates = trace[-1]["candidates"] if trace else []
        frozen[sid] = {c["cid"]: c["url"] for c in candidates}
        if len(frozen[sid]) != len(candidates):
            raise ValueError(f"duplicate frozen candidate id: {sid}")
    validated = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("source label requires a string id")
        sid = row["id"]
        if sid in validated:
            raise ValueError(f"duplicate source label id: {sid}")
        if sid not in frozen:
            raise ValueError(f"unknown source label id: {sid}")
        constraint = row.get("source_constraint")
        if not isinstance(constraint, dict) or constraint.get("mode") not in (
            "none", "preference", "hard"
        ):
            raise ValueError(f"invalid source constraint mode: {sid}")
        mode, domains = constraint["mode"], constraint.get("target_domains")
        if (not isinstance(domains, list)
                or any(not isinstance(domain, str) or not domain.strip() for domain in domains)
                or len(set(domains)) != len(domains)
                or (mode == "none" and domains) or (mode != "none" and not domains)):
            raise ValueError(f"invalid source target_domains: {sid}")
        if not isinstance(constraint.get("rationale"), str):
            raise ValueError(f"missing source constraint rationale: {sid}")
        review = row.get("review")
        if (not isinstance(review, dict)
                or any(not isinstance(review.get(key), str) or not review[key]
                       for key in ("annotator", "model"))
                or type(review.get("user_reviewed")) is not bool):
            raise ValueError(f"invalid source review metadata: {sid}")
        if not isinstance(row.get("candidates"), list):
            raise ValueError(f"source candidates must be a list: {sid}")
        seen = set()
        for candidate in row["candidates"]:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("cid"), str):
                raise ValueError(f"invalid source candidate id: {sid}")
            cid = candidate["cid"]
            if cid in seen:
                raise ValueError(f"duplicate source candidate id: {sid}/{cid}")
            seen.add(cid)
            if cid not in frozen[sid]:
                raise ValueError(f"unknown source candidate id: {sid}/{cid}")
            if candidate.get("url") != frozen[sid][cid]:
                raise ValueError(f"source candidate URL mismatch: {sid}/{cid}")
            for key in ("content_useful", "source_match"):
                if key not in candidate or (candidate[key] is not None
                                            and type(candidate[key]) is not bool):
                    raise ValueError(f"invalid source {key}: {sid}/{cid}")
            if mode == "none" and candidate["source_match"] is not None:
                raise ValueError(f"source_match must be null without a target: {sid}/{cid}")
            if not isinstance(candidate.get("rationale"), str):
                raise ValueError(f"missing source candidate rationale: {sid}/{cid}")
        validated[sid] = row
    return validated


def load_source_labels(path, records):
    return validate_source_labels(jsonl(path), records)


def effective_useful(content_useful, source_match, mode):
    """Three-valued AND for hard constraints; preferences never veto content."""
    if mode != "hard":
        return content_useful
    if content_useful is False or source_match is False:
        return False
    if content_useful is None or source_match is None:
        return None
    return True


def source_score(records, labels, source_labels):
    """Score independent axes without changing any historical useful labels."""
    source_labels = validate_source_labels(list(source_labels.values()), records)
    public = [record for record in records if not sensitive(record["sample"])]
    counts = {key: Counter() for key in ("content", "effective", "hard_effective")}
    source_counts, coverage = Counter(), Counter()
    tables = {key: Counter() for key in ("content", "effective", "source_match")}
    modes = Counter()
    details = []

    def state(value):
        return "unknown" if value is None else str(value).lower()

    def observe(counter, value, delivered, killed):
        counter["candidate_n"] += 1
        counter["unknown_candidate_n" if value is None else "known_candidate_n"] += 1
        counter["useful_n"] += value is True
        counter["false_kill_n"] += value is True and killed
        if delivered:
            counter["delivered_n"] += 1
            counter["unknown_delivered_n" if value is None else "known_delivered_n"] += 1
            counter["false_keep_n"] += value is False

    for record in public:
        sid = record["sample"]["id"]
        overlay = source_labels.get(sid)
        mode = overlay["source_constraint"]["mode"] if overlay else None
        modes[mode or "unannotated"] += 1
        coverage["query_n"] += 1
        coverage["annotated_query_n"] += overlay is not None
        if overlay:
            coverage["user_reviewed_query_n"] += overlay["review"]["user_reviewed"]
        trace = record["selection_trace"]
        candidates = trace[-1]["candidates"] if trace else []
        annotations = {(c["cid"], c["url"]): c for c in overlay["candidates"]} if overlay else {}
        old = {(c["cid"], c["url"]): c["useful"] for c in labels[sid]["candidate_labels"]}
        dropped = set(trace[-1]["dropped_relevance"]) if trace else set()
        ev = record["evidence"]
        delivered_urls = {canonical_url(c["url"]) for c in ev["results"]} if ev else set()
        frozen_urls = {canonical_url(c["url"]) for c in candidates}
        if delivered_urls - frozen_urls:
            raise ValueError(f"delivered URL absent from frozen candidates: {sid}")
        targeted = overlay is not None and bool(overlay["source_constraint"]["target_domains"])
        delivered_matches = []
        query_counts = Counter()
        for candidate in candidates:
            key = (candidate["cid"], candidate["url"])
            annotation = annotations.get(key)
            content = annotation["content_useful"] if annotation else None
            match = annotation["source_match"] if annotation else None
            effective = effective_useful(content, match, mode)
            delivered = canonical_url(candidate["url"]) in delivered_urls
            killed = (candidate["cid"] in dropped
                      and (candidate.get("judgment") or {}).get("rel", 1) < 0.5)
            coverage["candidate_n"] += 1
            coverage["annotated_candidate_n"] += annotation is not None
            observe(counts["content"], content, delivered, killed)
            observe(counts["effective"], effective, delivered, killed)
            if mode == "hard":
                observe(counts["hard_effective"], effective, delivered, killed)
            for axis, value in (("content", content), ("effective", effective), ("source_match", match)):
                tables[axis][(state(old.get(key)), state(value))] += 1
            if targeted:
                source_counts["candidate_n"] += 1
                source_counts["unknown_candidate_n" if match is None else "known_candidate_n"] += 1
                source_counts["matched_candidate_n"] += match is True
                if delivered:
                    delivered_matches.append(match)
                    source_counts["delivered_n"] += 1
                    source_counts["unknown_delivered_n" if match is None else "known_delivered_n"] += 1
                    source_counts["matched_delivered_n"] += match is True
            query_counts["candidate_n"] += 1
            query_counts["annotated_candidate_n"] += annotation is not None
            query_counts["unknown_content_n"] += content is None
            query_counts["unknown_effective_n"] += effective is None
        if targeted:
            source_counts["target_query_n"] += 1
            if any(value is True for value in delivered_matches):
                source_counts["matched_query_n"] += 1
                source_counts["known_query_n"] += 1
            elif any(value is None for value in delivered_matches):
                source_counts["unknown_query_n"] += 1
            else:
                source_counts["known_query_n"] += 1
        details.append({"id": sid, "mode": mode, "explicit_target": targeted,
                        "pipeline_error": record.get("pipeline_error"), **query_counts})

    def usefulness_metrics(counter):
        keys = ("candidate_n", "known_candidate_n", "unknown_candidate_n", "useful_n",
                "false_kill_n", "delivered_n", "known_delivered_n", "unknown_delivered_n",
                "false_keep_n")
        return {**{key: counter[key] for key in keys},
                "false_kill_rate": ratio(counter["false_kill_n"], counter["useful_n"]),
                "false_keep_rate": ratio(counter["false_keep_n"], counter["known_delivered_n"])}

    source_keys = ("candidate_n", "known_candidate_n", "unknown_candidate_n", "matched_candidate_n",
                   "delivered_n", "known_delivered_n", "unknown_delivered_n", "matched_delivered_n",
                   "target_query_n", "known_query_n", "unknown_query_n", "matched_query_n")
    return {
        "coverage": {key: coverage[key] for key in (
            "query_n", "annotated_query_n", "user_reviewed_query_n", "candidate_n", "annotated_candidate_n")},
        "constraint_modes": dict(modes),
        **{key: usefulness_metrics(counter) for key, counter in counts.items()},
        "source_delivery": {
            **{key: source_counts[key] for key in source_keys},
            "match_rate": ratio(source_counts["matched_delivered_n"], source_counts["known_delivered_n"]),
            "query_coverage_rate": ratio(source_counts["matched_query_n"], source_counts["known_query_n"]),
            "scope": "explicit target_domains only; unknown excluded from rate denominators",
        },
        "legacy_useful_cross_tables": {
            axis: {old: {new: table[(old, new)] for new in ("true", "false", "unknown")}
                   for old in ("true", "false", "unknown")}
            for axis, table in tables.items()
        },
        "details": details,
    }


def summarize(args):
    records = [json.loads(p.read_text()) for p in sorted((args.run_dir / "evidence").glob("*.json"))]
    labels = {row["id"]: row for row in jsonl(args.labels)}
    source_path = getattr(args, "source_labels", None)
    source_labels = load_source_labels(source_path, records) if source_path else None
    output = args.output or args.run_dir / ("metrics-source-v2.json" if source_path else "metrics.json")
    if source_path and (output.exists() or output.name in {"metrics.json", "metrics-review.json"}):
        raise ValueError("source v2 output must be a new file, not historical metrics/metrics-review")
    manifest = json.loads((args.run_dir / "manifest.json").read_text())
    routable = {s["id"] for s in manifest["inventory"] if s["routable"]}
    for record in records:
        sid = record["sample"]["id"]
        if sid not in labels:
            raise ValueError(f"missing label: {sid}")
        label = labels[sid]
        if sensitive(record["sample"]):
            local_ids = {s["id"] for s in manifest["inventory"] if s["privacy_class"] == "local"}
            record["negative_safety_pass"] = negative_safety(
                record["evidence"], record["blocked_egress_attempts"], local_ids)
        if not set(label["expected_sources"]) <= routable:
            raise ValueError(f"non-routable gold source: {sid}")
        if not isinstance(label["delivered_enough"], bool):
            raise ValueError(f"missing delivered_enough: {sid}")
        blind_path = args.run_dir / "blind" / f"{sid}.json"
        if label.get("blind_sha256") != hashlib.sha256(blind_path.read_bytes()).hexdigest():
            raise ValueError(f"labels belong to different blind content: {sid}")
        actual_urls = {c["cid"]: c["url"] for c in record["selection_trace"][-1]["candidates"]} if record["selection_trace"] else {}
        if any(actual_urls.get(c["cid"]) != c["url"] for c in label["candidate_labels"]):
            raise ValueError(f"candidate URL mismatch: {sid}")
        actual = {c["cid"] for c in record["selection_trace"][-1]["candidates"]} if record["selection_trace"] else set()
        supplied = [c["cid"] for c in label["candidate_labels"]]
        if actual != set(supplied) or len(supplied) != len(set(supplied)) or any(not isinstance(c["useful"], bool) for c in label["candidate_labels"]):
            raise ValueError(f"candidate label mismatch: {sid}")
    if len(records) != manifest["query_count"]:
        raise ValueError("incomplete run: evidence count does not match manifest")
    if source_labels is not None:
        def subset_score(subset):
            ids = {record["sample"]["id"] for record in subset}
            return source_score(subset, labels, {sid: row for sid, row in source_labels.items() if sid in ids})

        scores = {"overall": subset_score(records), "by_language": {}, "by_stratum": {},
                  "by_provenance": {}}
        for field, target in (("lang", "by_language"), ("stratum", "by_stratum")):
            for value in sorted({r["sample"][field] for r in records}):
                scores[target][value] = subset_score([r for r in records if r["sample"][field] == value])
        for synthetic in (False, True):
            scores["by_provenance"]["synthetic" if synthetic else "real"] = subset_score(
                [r for r in records if r["sample"]["synthetic"] is synthetic])
        result = {
            "source_metric_version": 2, "run_dir": str(args.run_dir),
            "source_labels": str(source_path),
            "source_labels_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "annotation_status": "assistant overlay; not user sign-off",
            "unknown_policy": "null/missing excluded from known denominators; hard uses three-valued AND",
            "source_scores_v2": scores,
        }
        dump(output, result)
        print(json.dumps({k: v for k, v in scores["overall"].items() if k != "details"},
                         ensure_ascii=False, indent=2))
        return
    result = {"metric_version": 2, "run_dir": str(args.run_dir),
              "overall": score(records, labels), "by_language": {}, "by_stratum": {},
              "by_provenance": {}}
    for field, target in (("lang", "by_language"), ("stratum", "by_stratum")):
        for value in sorted({r["sample"][field] for r in records}):
            result[target][value] = score([r for r in records if r["sample"][field] == value], labels)
    for synthetic in (False, True):
        result["by_provenance"]["synthetic" if synthetic else "real"] = score(
            [r for r in records if r["sample"]["synthetic"] is synthetic], labels)
    dump(output, result)
    print(json.dumps({k: v for k, v in result["overall"].items() if k != "details"}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "summarize"))
    parser.add_argument("--queries", type=Path, default=ROOT / "evals/queries.jsonl")
    parser.add_argument("--labels", type=Path, default=ROOT / "evals/labels.jsonl")
    parser.add_argument("--source-labels", type=Path,
                        help="Offline content/source overlay; writes a separate source v2 summary, never legacy metrics")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--protocol", type=Path, help="Frozen formal holdout protocol; requires --preregistration")
    parser.add_argument("--preregistration", type=Path, help="Offline holdout preregistration JSON; validated before execution")
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=1)
    parser.add_argument("--latency-mode", choices=("legacy", "isolated-paired"), default="legacy",
                        help="legacy shares two outbound slots; isolated-paired runs serial balanced AB/BA without an external semaphore")
    parser.add_argument("--seed", type=int, default=20261020,
                        help="Fixed query/order randomization seed for isolated-paired")
    parser.add_argument("--ids", help="Optional comma-separated sample ids for a separate partial run")
    parser.add_argument("--output", type=Path, help="Summary output path; use a new file to preserve historical metrics")
    args = parser.parse_args()
    if args.command == "run" and args.source_labels is not None:
        parser.error("--source-labels is only supported by summarize")
    if args.command != "run" and (args.protocol is not None or args.preregistration is not None):
        parser.error("--protocol and --preregistration are only supported by run")
    if args.command == "run":
        asyncio.run(run(args))
    else:
        if args.run_dir is None:
            parser.error("summarize requires --run-dir")
        summarize(args)


if __name__ == "__main__":
    main()
