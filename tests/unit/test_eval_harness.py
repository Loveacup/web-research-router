"""Offline evaluation regressions; no providers or credentials are used."""
from copy import deepcopy
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from evals import run_eval


def public_record(*, scope="public_only", judged=False, enough=1.0):
    candidate = {
        "cid": "c0", "url": "https://example.org/a", "title": "A result",
        "snippet": "Public content", "flags": [] if judged else ["unjudged:sensitive"],
        "judgment": {"rel": 0.9, "inj": 0.0} if judged else {},
        "contributions": [{"source_id": "exa"}],
    }
    record = {
        "sample": {"id": "public", "stratum": "overview", "synthetic": False},
        "pipeline_wall_s": 1.0, "pipeline_error": None,
        "baseline": {"status": "ok", "wall_s": 0.5},
        "evidence": {
            "plan": {"sources": ["exa"], "gate": None},
            "diagnostics": {"judge": {"posts": 3, "failures": []}},
            "runs": [], "results": [candidate],
            "quality": {"recovered": False, "sufficiency": {"scope": scope, "enough": enough}},
        },
        "selection_trace": [{"candidates": [candidate], "dropped_relevance": [], "dropped_injection": []}],
    }
    label = {"id": "public", "expected_sources": ["exa"], "gate": None,
             "candidate_labels": [{"cid": "c0", "url": candidate["url"], "useful": True}],
             "delivered_enough": False}
    return record, label


def test_full_delivery_brier_excludes_unassessed_delivered_content():
    record, label = public_record()
    result = run_eval.score([record], {"public": label})
    assert result["brier"] is None
    assert result["brier_n"] == 0
    assert result["brier_legacy_mixed_scope"] == 1.0
    assert result["brier_scope_mismatch_n"] == 1


@pytest.mark.parametrize("scope,judged,expected_n", [
    ("delivered", True, 1),
    ("public_only", True, 1),
    ("after_recovery:raw_scope", True, 0),
    ("all_rejected", False, 0),
])
def test_calibration_uses_realized_full_delivery_scope(scope, judged, expected_n):
    record, label = public_record(scope=scope, judged=judged)
    result = run_eval.score([record], {"public": label})
    assert result["brier_n"] == expected_n
    assert result["brier"] == (1.0 if expected_n else None)
    assert result["brier_by_declared_scope"][scope] == {"n": 1, "brier": 1.0}


def test_false_kills_use_final_round_relevance_rejection_not_topk_omissions():
    record, label = public_record(scope="delivered", judged=True)
    original = record["selection_trace"][0]["candidates"][0]
    killed = deepcopy(original)
    killed.update(cid="c1", url="https://example.org/rejected", judgment={"rel": 0.49, "inj": 0.0})
    truncated = deepcopy(original)
    truncated.update(cid="c2", url="https://example.org/topk-truncated")
    record["selection_trace"] = [
        {"candidates": [dict(original, cid="c1")], "dropped_relevance": ["c1"], "dropped_injection": []},
        {"candidates": [original, killed, truncated], "dropped_relevance": ["c1"], "dropped_injection": []},
    ]
    label["candidate_labels"] += [
        {"cid": c["cid"], "url": c["url"], "useful": True} for c in (killed, truncated)
    ]
    result = run_eval.score([record], {"public": label})
    assert result["useful_n"] == 3
    assert result["false_kill_n"] == 1
    assert result["false_kill_rate"] == 1 / 3
    assert result["false_keep_n"] == 0


def test_instruction_only_case_is_separate_from_privacy_zero_egress():
    privacy = {"sample": {"id": "private", "stratum": "sensitive_negative", "synthetic": True},
               "negative_safety_pass": True}
    instruction = {"sample": {"id": "instruction", "stratum": "sensitive_negative", "synthetic": True},
                   "negative_safety_pass": False}
    labels = {
        "private": {"safety_contract": "no_external_egress"},
        "instruction": {"safety_contract": "instruction_only_robustness"},
    }
    result = run_eval.score([privacy, instruction], labels)
    assert result["n_negative"] == 2
    assert result["negative_safety_pass_n"] == 1
    assert result["privacy_negative_n"] == 1
    assert result["privacy_negative_safety_pass_n"] == 1
    assert result["instruction_only_negative_n"] == 1


def test_summary_preserves_historical_metrics_with_explicit_output(tmp_path):
    record, label = public_record(scope="delivered", judged=True)
    record["sample"].update(lang="en", source="omp")
    run_dir = tmp_path / "run"
    (run_dir / "evidence").mkdir(parents=True)
    (run_dir / "blind").mkdir()
    blind_bytes = b'{\"id\":\"public\"}\n'
    (run_dir / "blind/public.json").write_bytes(blind_bytes)
    label["blind_sha256"] = hashlib.sha256(blind_bytes).hexdigest()
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text(json.dumps(label) + "\n")
    (run_dir / "evidence/public.json").write_text(json.dumps(record))
    (run_dir / "manifest.json").write_text(json.dumps({
        "query_count": 1, "inventory": [{"id": "exa", "routable": True}],
    }))
    historical = run_dir / "metrics.json"
    historical.write_text('{\"historical\":true}\n')
    reviewed = run_dir / "metrics-review.json"
    run_eval.summarize(SimpleNamespace(run_dir=run_dir, labels=labels_path, output=reviewed))
    assert historical.read_text() == '{\"historical\":true}\n'
    result = json.loads(reviewed.read_text())
    assert result["metric_version"] == 2
    assert result["overall"]["brier_target"] == "full_delivery_realized_scope"
    assert result["by_provenance"]["real"]["n_public"] == 1
    assert result["by_provenance"]["synthetic"]["n_public"] == 0


@pytest.fixture
def latency_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    rows = [
        {"id": f"public-{i}", "query": f"public query {i}", "stratum": "overview",
         "lang": "en", "source": "synthetic", "synthetic": True}
        for i in range(6)
    ]
    queries = tmp_path / "queries.jsonl"
    queries.write_text("".join(json.dumps(row) + "\n" for row in rows))
    events = []
    active = set()

    async def baseline(self, options):
        assert not active, "another arm is active during baseline"
        active.add("baseline")
        events.append(("baseline", options.query))
        await asyncio.sleep(0)
        active.remove("baseline")
        return []

    async def pipeline(query, *, adapters, judge, **kwargs):
        assert not active, "another arm is active during pipeline"
        active.add("pipeline")
        events.append(("pipeline", query))
        # Three calls must reach their delegates together: the old external
        # two-slot semaphore would leave one queued behind an unrelated call.
        entered = 0
        all_entered = asyncio.Event()

        async def source():
            nonlocal entered
            entered += 1
            if entered == 3:
                all_entered.set()
            await asyncio.wait_for(all_entered.wait(), timeout=0.1)
            return []

        for adapter in adapters.values():
            adapter.delegate.search = source
        try:
            await asyncio.gather(*(adapter.search() for adapter in adapters.values()))
        finally:
            active.remove("pipeline")
        return SimpleNamespace(to_dict=lambda: {
            "results": [], "runs": [], "diagnostics": {"judge": {"failures": []}},
        })

    class Source:
        async def search(self):
            return []

    monkeypatch.setattr(run_eval, "prepare_environment", lambda: (None, None, {}))
    monkeypatch.setattr(run_eval, "build_inventory", lambda **kw: [])
    monkeypatch.setattr(run_eval, "build_adapters",
                        lambda *a, **kw: {str(i): Source() for i in range(3)})
    monkeypatch.setattr(run_eval, "resolve_judge", lambda **kw: SimpleNamespace(backend="offline"))
    monkeypatch.setattr(run_eval, "search", pipeline)
    monkeypatch.setattr(run_eval.ExaEngine, "search", baseline)
    args = SimpleNamespace(queries=queries, ids=None, run_dir=tmp_path / "run",
                           concurrency=1, latency_mode="isolated-paired", seed=20261020)
    return args, events


@pytest.mark.asyncio
async def test_isolated_pairs_remove_external_queue_and_never_overlap_arms(latency_runtime):
    args, events = latency_runtime
    await run_eval.run(args)
    records = [json.loads(path.read_text()) for path in (args.run_dir / "evidence").glob("*.json")]
    assert len(records) == 6
    assert all(record["pipeline_error"] is None for record in records)
    assert len(events) == 12
    for index in range(0, len(events), 2):
        assert events[index][1] == events[index + 1][1]
        assert {events[index][0], events[index + 1][0]} == {"pipeline", "baseline"}
    assert all(call["queue_s"] == 0 for record in records
               for call in record["latency"]["pipeline"]["calls"])


@pytest.mark.asyncio
async def test_isolated_orders_are_balanced_seeded_and_saved(latency_runtime):
    args, events = latency_runtime
    await run_eval.run(args)
    first_events = list(events)
    first_manifest = json.loads((args.run_dir / "manifest.json").read_text())
    orders = [pair["order"] for pair in first_manifest["pair_schedule"]]
    assert orders.count("AB") == orders.count("BA") == 3
    assert first_manifest["external_semaphore"] == "none"
    assert first_manifest["outbound_concurrency"] is None
    assert first_manifest["seed"] == 20261020
    assert first_manifest["harness_retries"] == 0
    for pair, index in zip(first_manifest["pair_schedule"], range(0, 12, 2)):
        assert first_events[index][0] == ("pipeline" if pair["order"] == "AB" else "baseline")
        record = json.loads((args.run_dir / "evidence" / f"{pair['id']}.json").read_text())
        assert record["pair_order"] == pair["order"]
    events.clear()
    args.run_dir = args.run_dir.parent / "same-seed"
    await run_eval.run(args)
    assert events == first_events
    events.clear()
    args.run_dir = args.run_dir.parent / "different-seed"
    args.seed = 7
    await run_eval.run(args)
    assert events != first_events


@pytest.mark.asyncio
async def test_failed_arms_still_persist_and_count_without_retry(latency_runtime, monkeypatch):
    args, events = latency_runtime

    async def failing_pipeline(query, **kwargs):
        events.append(("pipeline", query))
        raise TimeoutError("private provider error detail")

    async def failing_baseline(self, options):
        events.append(("baseline", options.query))
        raise RuntimeError("private provider error detail")

    monkeypatch.setattr(run_eval, "search", failing_pipeline)
    monkeypatch.setattr(run_eval.ExaEngine, "search", failing_baseline)
    await run_eval.run(args)
    records = [json.loads(path.read_text()) for path in (args.run_dir / "evidence").glob("*.json")]
    assert len(records) == 6
    assert len(events) == 12
    assert all(record["pipeline_error"] == "timeout" for record in records)
    assert all(record["baseline"]["status"] == "error" for record in records)
    assert len(list((args.run_dir / "blind").glob("*.json"))) == 6
    assert all("private provider error detail" not in json.dumps(record) for record in records)
    summary = json.loads((args.run_dir / "latency-summary.json").read_text())
    assert summary["expected_public_n"] == summary["pipeline_wall_n"] == summary["baseline_wall_n"] == 6
    assert summary["paired_latency_n"] == summary["failed_pair_n"] == 6
    assert summary["missing_pair_n"] == 0
    assert summary["increment_observed_p95_s"] is not None
    assert summary["increment_failure_penalized_p95_s"] == "failed_or_unknown"
    assert summary["performance_pass_claim"] is False
    # Resume is not an automatic retry of failed arms.
    await run_eval.run(args)
    assert len(events) == 12


def test_new_pairing_counts_timeout_but_preserves_legacy_scoring():
    record, label = public_record(scope="delivered", judged=True)
    record["baseline"] = {"status": "timeout", "wall_s": 17.0}
    legacy = run_eval.score([record], {"public": label})
    assert legacy["baseline_wall_n"] == 1
    assert legacy["paired_latency_n"] == 0
    record["latency_mode"] = "isolated-paired"
    isolated = run_eval.score([record], {"public": label})
    assert isolated["paired_latency_n"] == 1
    assert isolated["failures"]["baseline:timeout"] == 1


@pytest.mark.asyncio
async def test_isolated_rejects_cross_query_concurrency(latency_runtime):
    args, events = latency_runtime
    args.concurrency = 2
    with pytest.raises(ValueError, match="requires --concurrency 1"):
        await run_eval.run(args)
    assert not events
    assert not args.run_dir.exists()


def test_missing_pairs_and_dependency_timeouts_cannot_hide_in_tail():
    record, _ = public_record(scope="delivered", judged=True)
    record["latency"] = {"pipeline": {"calls": [{"status": "timeout"}]}}
    summary = run_eval.latency_summary([record], {"public_query_count": 2, "latency_mode": "isolated-paired"})
    assert summary["expected_public_n"] == 2
    assert summary["paired_latency_n"] == summary["missing_pair_n"] == summary["failed_pair_n"] == 1
    assert summary["pipeline_dependency_failure_query_n"] == 1
    assert summary["increment_failure_penalized_p95_s"] == "failed_or_unknown"


def test_negative_guards_do_not_unbalance_public_pair_schedule():
    rows = [{"id": str(i), "stratum": "overview"} for i in range(4)]
    rows.append({"id": "private", "stratum": "sensitive_negative"})
    schedule = run_eval.paired_schedule(rows, 20261020)
    assert [order for row, order in schedule if row["id"] != "private"].count("AB") == 2
    assert [order for row, order in schedule if row["id"] != "private"].count("BA") == 2
    assert schedule[-1][1] == "A"


@pytest.mark.asyncio
async def test_cancelled_queued_call_has_unknown_service_and_no_delegate():
    calls = []
    entered = False

    async def delegate():
        nonlocal entered
        entered = True

    queued = asyncio.create_task(run_eval.timed_call(calls, "offline", delegate, asyncio.Semaphore(0)))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert not entered
    assert calls[0]["status"] == "cancelled"
    assert calls[0]["delegate_started"] is False
    assert calls[0]["acquired_monotonic_s"] == calls[0]["service_s"] == "unknown"
    assert calls[0]["queue_s"] == calls[0]["wall_s"]


@pytest.mark.asyncio
async def test_legacy_run_keeps_fixed_ab_and_shared_outbound_limit(latency_runtime, monkeypatch):
    args, events = latency_runtime
    args.latency_mode, args.concurrency = "legacy", 2
    active = peak = 0

    async def source():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return []

    async def pipeline(query, *, adapters, **kwargs):
        events.append(("pipeline", query))
        for adapter in adapters.values():
            adapter.delegate.search = source
        await asyncio.gather(*(adapter.search() for adapter in adapters.values()))
        return SimpleNamespace(to_dict=lambda: {"results": []})

    async def baseline(self, options):
        events.append(("baseline", options.query))
        return await source()

    monkeypatch.setattr(run_eval, "search", pipeline)
    monkeypatch.setattr(run_eval.ExaEngine, "search", baseline)
    await run_eval.run(args)
    assert peak == 2
    manifest = json.loads((args.run_dir / "manifest.json").read_text())
    assert manifest["outbound_concurrency"] == 2
    assert all(pair["order"] == "AB" for pair in manifest["pair_schedule"])
    for row in run_eval.jsonl(args.queries):
        own_events = [arm for arm, query in events if query == row["query"]]
        assert own_events == ["pipeline", "baseline"]


def source_overlay(record, *, mode="preference", content=True, match=False):
    return {
        "id": record["sample"]["id"],
        "source_constraint": {
            "mode": mode, "target_domains": [] if mode == "none" else ["reddit.com"],
            "rationale": "Explicit platform preference, not a content relevance label.",
        },
        "candidates": [
            {"cid": c["cid"], "url": c["url"], "content_useful": content,
             "source_match": None if mode == "none" else match,
             "rationale": "Frozen title/snippet supports the topic; host is independent."}
            for c in record["selection_trace"][-1]["candidates"]
        ],
        "review": {"annotator": "assistant", "model": "unknown", "user_reviewed": False},
    }


@pytest.mark.parametrize("mode,expected_wrong", [("preference", 0), ("none", 0), ("hard", 1)])
def test_source_preference_never_vetoes_useful_content(mode, expected_wrong):
    record, label = public_record(scope="delivered", judged=True)
    before = deepcopy(label)
    overlay = source_overlay(record, mode=mode)
    result = run_eval.source_score([record], {"public": label}, {"public": overlay})
    assert result["content"]["false_keep_n"] == 0
    assert result["effective"]["false_keep_n"] == expected_wrong
    assert result["source_delivery"]["target_query_n"] == (mode != "none")
    if mode != "none":
        assert result["source_delivery"]["match_rate"] == 0
        assert result["source_delivery"]["query_coverage_rate"] == 0
    assert result["hard_effective"]["false_keep_n"] == (expected_wrong if mode == "hard" else 0)
    assert label == before
    assert run_eval.score([record], {"public": label})["false_keep_n"] == 0


def test_source_match_does_not_make_irrelevant_content_useful():
    record, label = public_record(scope="delivered", judged=True)
    overlay = source_overlay(record, mode="hard", content=False, match=True)
    result = run_eval.source_score([record], {"public": label}, {"public": overlay})
    assert result["content"]["false_keep_n"] == 1
    assert result["effective"]["false_keep_n"] == 1
    assert result["source_delivery"]["match_rate"] == 1
    assert result["legacy_useful_cross_tables"]["content"]["true"]["false"] == 1


@pytest.mark.parametrize("mode,content,match,effective", [
    ("hard", True, None, None), ("hard", None, True, None),
    ("hard", None, False, False), ("hard", False, None, False),
    ("preference", True, None, True), ("preference", None, False, None),
])
def test_source_unknowns_are_not_coerced_to_false(mode, content, match, effective):
    record, label = public_record(scope="delivered", judged=True)
    overlay = source_overlay(record, mode=mode, content=content, match=match)
    result = run_eval.source_score([record], {"public": label}, {"public": overlay})
    assert run_eval.effective_useful(content, match, mode) is effective
    assert result["content"]["unknown_delivered_n"] == (content is None)
    assert result["effective"]["unknown_delivered_n"] == (effective is None)
    assert result["effective"]["false_keep_n"] == (effective is False)
    if effective is None:
        assert result["effective"]["false_keep_rate"] is None
    if match is None:
        assert result["source_delivery"]["known_delivered_n"] == 0
        assert result["source_delivery"]["unknown_delivered_n"] == 1
        assert result["source_delivery"]["match_rate"] is None
        assert result["source_delivery"]["unknown_query_n"] == 1


def test_partial_source_overlay_reports_uncovered_candidates_and_queries():
    record, label = public_record(scope="delivered", judged=True)
    other = deepcopy(record)
    other["sample"]["id"] = "other"
    other_label = deepcopy(label)
    other_label["id"] = "other"
    overlay = source_overlay(record)
    overlay["candidates"] = []
    result = run_eval.source_score([record, other], {"public": label, "other": other_label},
                                   {"public": overlay})
    assert result["coverage"] == {
        "query_n": 2, "annotated_query_n": 1, "user_reviewed_query_n": 0,
        "candidate_n": 2, "annotated_candidate_n": 0,
    }
    assert result["content"]["unknown_candidate_n"] == 2
    assert result["content"]["known_delivered_n"] == 0
    assert result["content"]["false_keep_n"] == 0
    assert result["source_delivery"]["target_query_n"] == 1
    assert result["source_delivery"]["unknown_query_n"] == 1


def test_source_false_kills_only_count_final_round_relevance_drops():
    record, label = public_record(scope="delivered", judged=True)
    original = record["selection_trace"][-1]["candidates"][0]
    rejected = deepcopy(original)
    rejected.update(cid="c1", url="https://example.org/rejected", judgment={"rel": 0.49})
    truncated = deepcopy(original)
    truncated.update(cid="c2", url="https://example.org/truncated")
    record["selection_trace"] = [
        {"candidates": [dict(original, cid="c1")], "dropped_relevance": ["c1"]},
        {"candidates": [original, rejected, truncated], "dropped_relevance": ["c1"]},
    ]
    label["candidate_labels"] += [
        {"cid": c["cid"], "url": c["url"], "useful": True} for c in (rejected, truncated)
    ]
    overlay = source_overlay(record)
    result = run_eval.source_score([record], {"public": label}, {"public": overlay})
    assert result["content"]["useful_n"] == 3
    assert result["content"]["false_kill_n"] == 1
    assert result["content"]["false_kill_rate"] == 1 / 3
    assert result["effective"]["false_kill_n"] == 1


@pytest.mark.parametrize("mutation,error", [
    (lambda row: row.update(id="unknown"), "unknown source label id"),
    (lambda row: row["candidates"][0].update(cid="unknown"), "unknown source candidate id"),
    (lambda row: row["candidates"][0].update(url="https://wrong.org/a"), "URL mismatch"),
    (lambda row: row["candidates"].append(deepcopy(row["candidates"][0])), "duplicate source candidate"),
    (lambda row: row["source_constraint"].update(mode="soft"), "mode"),
    (lambda row: row["source_constraint"].update(mode=[]), "mode"),
    (lambda row: row["source_constraint"].update(target_domains=[]), "target_domains"),
    (lambda row: row["candidates"][0].update(content_useful=1), "content_useful"),
    (lambda row: row["candidates"][0].update(source_match="false"), "source_match"),
    (lambda row: row["review"].update(user_reviewed=0), "review metadata"),
    (lambda row: row["source_constraint"].update(mode="none", target_domains=[]), "must be null"),
])
def test_source_loader_rejects_invalid_annotations(tmp_path, mutation, error):
    record, _ = public_record(scope="delivered", judged=True)
    overlay = source_overlay(record)
    mutation(overlay)
    path = tmp_path / "source.jsonl"
    path.write_text(json.dumps(overlay) + "\n")
    with pytest.raises(ValueError, match=error):
        run_eval.load_source_labels(path, [record])


def test_source_loader_rejects_duplicate_query_rows(tmp_path):
    record, _ = public_record(scope="delivered", judged=True)
    overlay = source_overlay(record)
    path = tmp_path / "source.jsonl"
    path.write_text((json.dumps(overlay) + "\n") * 2)
    with pytest.raises(ValueError, match="duplicate source label id"):
        run_eval.load_source_labels(path, [record])


def test_source_loader_never_binds_by_cid_alone():
    record, _ = public_record(scope="delivered", judged=True)
    other = deepcopy(record)
    other["sample"]["id"] = "other"
    other["selection_trace"][-1]["candidates"][0]["url"] = "https://different.org/a"
    overlay = source_overlay(record)
    overlay["id"] = "other"
    with pytest.raises(ValueError, match="URL mismatch"):
        run_eval.validate_source_labels([overlay], [record, other])


def test_source_summary_is_offline_separate_and_never_overwrites_history(tmp_path, monkeypatch):
    record, label = public_record(scope="delivered", judged=True)
    record["sample"].update(lang="en", source="omp")
    run_dir = tmp_path / "run"
    (run_dir / "evidence").mkdir(parents=True)
    (run_dir / "blind").mkdir()
    blind = b'{"id":"public"}\n'
    (run_dir / "blind/public.json").write_bytes(blind)
    label["blind_sha256"] = hashlib.sha256(blind).hexdigest()
    labels_path, source_path = tmp_path / "labels.jsonl", tmp_path / "source.jsonl"
    labels_path.write_text(json.dumps(label) + "\n")
    source_path.write_text(json.dumps(source_overlay(record)) + "\n")
    (run_dir / "evidence/public.json").write_text(json.dumps(record))
    (run_dir / "manifest.json").write_text(json.dumps({
        "query_count": 1, "inventory": [{"id": "exa", "routable": True}],
    }))
    for name in ("metrics.json", "metrics-review.json"):
        (run_dir / name).write_text('{"historical":true}\n')

    def forbid(*args, **kwargs):
        pytest.fail("source summary must not invoke a provider or change legacy score")

    monkeypatch.setattr(run_eval, "prepare_environment", forbid)
    monkeypatch.setattr(run_eval, "search", forbid)
    monkeypatch.setattr(run_eval, "resolve_judge", forbid)
    monkeypatch.setattr(run_eval.ExaEngine, "search", forbid)
    monkeypatch.setattr(run_eval, "score", forbid)
    args = SimpleNamespace(run_dir=run_dir, labels=labels_path, source_labels=source_path, output=None)
    run_eval.summarize(args)
    result = json.loads((run_dir / "metrics-source-v2.json").read_text())
    assert result["source_metric_version"] == 2
    assert "overall" not in result
    assert result["source_scores_v2"]["overall"]["content"]["false_keep_n"] == 0
    assert result["source_scores_v2"]["overall"]["coverage"]["user_reviewed_query_n"] == 0
    for name in ("metrics.json", "metrics-review.json"):
        assert (run_dir / name).read_text() == '{"historical":true}\n'
        args.output = run_dir / name
        with pytest.raises(ValueError, match="new file"):
            run_eval.summarize(args)
    args.output = None
    with pytest.raises(ValueError, match="new file"):
        run_eval.summarize(args)
