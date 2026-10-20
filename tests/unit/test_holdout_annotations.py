"""Small offline holdout fixtures; no live search, judge, or credentials."""
from copy import deepcopy
import asyncio
import hashlib
import json
import socket

from types import SimpleNamespace
import pytest

from evals.holdout import prepare_annotations as annotations


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def frozen_run(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("annotation preparation must not use the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    rows = [
        {"id": "wrr7-holdout-001", "query": "official widget pricing site.reddit.com",
         "lang": "en", "stratum": "fact_lookup", "source": "omp", "synthetic": False},
        {"id": "wrr7-holdout-002", "query": "public timeout example",
         "lang": "en", "stratum": "overview", "source": "hermes", "synthetic": False},
    ]
    candidates = [
        {"cid": "c0", "title": "Widget prices", "url": "https://example.org/prices",
         "snippet": "A frozen price table.", "judgment": {"rel": 0.99}, "flags": [],
         "contributions": [{"source_id": "DO_NOT_EXPORT_SOURCE"}]},
        {"cid": "c1", "title": "Background", "url": "https://example.org/background",
         "snippet": "A frozen background excerpt.", "judgment": {}, "flags": ["unjudged:sensitive"]},
    ]
    records = [
        {"sample": rows[0], "selection_trace": [{"candidates": candidates,
          "dropped_relevance": ["c1"], "dropped_injection": []}],
         "evidence": {"results": candidates, "plan": {"sources": ["DO_NOT_EXPORT_PLAN"]},
          "quality": {"sufficiency": {"scope": "public_only", "enough": 0.87}}},
         "pipeline_error": None, "old_labels": "DO_NOT_EXPORT_LABELS",
         "jev_answer": "DO_NOT_EXPORT_JEV", "delivered_ids": ["c0", "c1"]},
        {"sample": rows[1], "selection_trace": [], "evidence": None,
         "pipeline_error": "timeout", "baseline": {"status": "timeout"}},
    ]
    queries = tmp_path / "queries.jsonl"
    queries.write_text("".join(json.dumps(row) + "\n" for row in rows))
    run_dir = tmp_path / "run"
    (run_dir / "evidence").mkdir(parents=True)
    for record in records:
        (run_dir / "evidence" / f"{record['sample']['id']}.json").write_text(json.dumps(record))
    public = tmp_path / "content"
    private = tmp_path / "coordinator"
    annotations.prepare_content(queries, run_dir, public, private, seed=19)
    return queries, run_dir, public, private


def complete_content(public, private):
    labels_path = public / "labels.template.jsonl"
    labels = read_rows(labels_path)
    for row in labels:
        row["annotation_status"] = "complete"
        for candidate in row["candidate_labels"]:
            candidate["rationale"] = "Frozen snippet does not establish this; unknown retained."
    labels_path.write_text("".join(json.dumps(row) + "\n" for row in labels))
    annotations.freeze_content(private, labels_path)
    return labels_path


def test_content_blind_packet_whitelists_text_and_retains_failures(frozen_run):
    _, _, public, private = frozen_run
    tasks = read_rows(public / "tasks.jsonl")
    assert len(tasks) == 2
    assert any(task["candidates"] == [] for task in tasks)
    for task in tasks:
        assert set(task) == {"task_id", "query", "candidates"}
        for item in task["candidates"]:
            assert set(item) == {"item_id", "title", "url", "snippet"}
    exported = "".join(path.read_text() for path in public.iterdir())
    for forbidden in ("jev_answer", "judgment", "dropped_", "delivered_ids", "old_labels",
                      "DO_NOT_EXPORT", '"rel"', '"enough"', '"cid"', "source_id"):
        assert forbidden not in exported
    index = json.loads((private / "index.json").read_text())
    assert index["queries_sha256"] == hashlib.sha256(frozen_run[0].read_bytes()).hexdigest()
    assert len(index["queries"]) == 2
    assert {row["status"] for row in index["queries"]} == {"ok", "timeout"}
    assert all(row["review"] == {"annotator": "assistant", "model": "unknown", "user_reviewed": False}
               for row in read_rows(public / "labels.template.jsonl"))


def test_sufficiency_is_separate_and_requires_frozen_content(frozen_run, tmp_path):
    _, _, public, private = frozen_run
    enough_dir = tmp_path / "sufficiency"
    with pytest.raises(ValueError, match="freeze content"):
        annotations.prepare_sufficiency(private, enough_dir)
    assert not enough_dir.exists()
    complete_content(public, private)
    annotations.prepare_sufficiency(private, enough_dir)
    tasks = read_rows(enough_dir / "tasks.jsonl")
    sizes = sorted(len(task["candidates"]) for task in tasks)
    assert sizes == [0, 1, 2]  # failed full delivery, assessed subset, complete delivery
    for task in tasks:
        assert set(task) == {"task_id", "query", "candidates"}
        assert all(set(item) == {"title", "url", "snippet"} for item in task["candidates"])
    text = "".join(path.read_text() for path in enough_dir.iterdir())
    for forbidden in ("0.87", "public_only", "delivered", "content_useful", "judgment", "source_match"):
        assert forbidden not in text
    assert all(row["enough"] is None for row in read_rows(enough_dir / "labels.template.jsonl"))


def test_source_axis_does_not_infer_official_domain_or_nonstandard_site_hardness(frozen_run, tmp_path):
    _, _, public, private = frozen_run
    complete_content(public, private)
    source_dir = tmp_path / "source"
    annotations.prepare_source(private, source_dir)
    rows = read_rows(source_dir / "labels.template.jsonl")
    constraints = {row["task_id"]: row["source_constraint"] for row in rows}
    assert constraints["q000001"] == {
        "mode": "ambiguous", "target_domains": [],
        "rationale": "Nonstandard site spelling; not a hard site: constraint.",
        "unadjudicated_requirements": [
            {"requirement": "official", "status": "unadjudicated", "target_domains": []}
        ],
    }
    assert constraints["q000002"]["mode"] is None
    assert all(item["source_match"] is None for row in rows for item in row["candidate_labels"])
    assert "content_useful" not in (source_dir / "tasks.jsonl").read_text()


def test_frozen_content_hash_prevents_posthoc_relabeling(frozen_run, tmp_path):
    _, _, public, private = frozen_run
    labels_path = complete_content(public, private)
    labels_path.write_text(labels_path.read_text() + "\n")
    with pytest.raises(ValueError, match="changed"):
        annotations.prepare_sufficiency(private, tmp_path / "sufficiency")


def test_raw_recovery_scope_is_unknown_not_fabricated(frozen_run, tmp_path):
    queries, run_dir, _, _ = frozen_run
    record_path = run_dir / "evidence/wrr7-holdout-001.json"
    record = json.loads(record_path.read_text())
    record["evidence"]["quality"]["sufficiency"]["scope"] = "after_recovery:raw_scope"
    record_path.write_text(json.dumps(record))
    content, private = tmp_path / "raw-content", tmp_path / "raw-private"
    annotations.prepare_content(queries, run_dir, content, private)
    complete_content(content, private)
    annotations.prepare_sufficiency(private, tmp_path / "raw-sufficiency")
    scope_index = json.loads((private / "sufficiency-index.json").read_text())
    assert scope_index["unknown_predictions"] == [
        {"id": "wrr7-holdout-001", "scope": "after_recovery:raw_scope",
         "reason": "exact assessed input not reconstructible from saved evidence"},
        {"id": "wrr7-holdout-002", "scope": None, "reason": "missing prediction"},
    ]


@pytest.mark.parametrize("mutation", ["pending", "missing_item", "integer_label", "duplicate_task"])
def test_freeze_rejects_incomplete_or_invalid_labels(frozen_run, mutation):
    _, _, public, private = frozen_run
    labels_path = public / "labels.template.jsonl"
    rows = read_rows(labels_path)
    for row in rows:
        row["annotation_status"] = "complete"
        for item in row["candidate_labels"]:
            item["rationale"] = "Explicitly unknown."
    nonempty = next(row for row in rows if row["candidate_labels"])
    if mutation == "pending":
        nonempty["annotation_status"] = "pending"
    elif mutation == "missing_item":
        nonempty["candidate_labels"].pop()
    elif mutation == "integer_label":
        nonempty["candidate_labels"][0]["content_useful"] = 1
    else:
        rows.append(deepcopy(rows[0]))
    labels_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError):
        annotations.freeze_content(private, labels_path)


def test_generator_refuses_overwrite_and_private_inside_blind_packet(frozen_run, tmp_path):
    queries, run_dir, public, private = frozen_run
    with pytest.raises(ValueError, match="new"):
        annotations.prepare_content(queries, run_dir, public, tmp_path / "other-private")
    with pytest.raises(ValueError, match="outside"):
        annotations.prepare_content(queries, run_dir, tmp_path / "leaky", tmp_path / "leaky/private")


def test_missing_evidence_is_retained_but_mismatched_query_is_rejected(frozen_run, tmp_path):
    queries, run_dir, _, _ = frozen_run
    (run_dir / "evidence/wrr7-holdout-002.json").unlink()
    annotations.prepare_content(queries, run_dir, tmp_path / "missing", tmp_path / "missing-private")
    index = json.loads((tmp_path / "missing-private/index.json").read_text())
    assert len(index["queries"]) == 2
    assert index["queries"][1]["status"] == "missing_evidence"
    path = run_dir / "evidence/wrr7-holdout-001.json"
    record = json.loads(path.read_text())
    record["sample"]["query"] = "different query"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="query mismatch"):
        annotations.prepare_content(queries, run_dir, tmp_path / "bad", tmp_path / "bad-private")


@pytest.fixture
def formal_registration(tmp_path):
    rows = [
        {"id": f"wrr7-holdout-{i:03d}", "query": f"distinct public query {i}",
         "lang": "en", "stratum": "overview", "source": "omp", "synthetic": False}
        for i in range(1, 61)
    ]
    queries = tmp_path / "formal-queries.jsonl"
    queries.write_text("".join(json.dumps(row) + "\n" for row in rows))
    protocol = tmp_path / "protocol.txt"
    protocol.write_text("Frozen test protocol.\n")
    registration = tmp_path / "preregistration.json"
    annotations.preregister(queries, protocol, registration, seed=19)
    args = SimpleNamespace(
        queries=queries, protocol=protocol, preregistration=registration,
        ids=None, seed=19, latency_mode="isolated-paired", concurrency=1,
        run_dir=tmp_path / "formal-run",
    )
    return args, rows


def test_preregistration_refuses_insufficient_real_or_duplicate_queries(frozen_run, tmp_path):
    queries, _, _, _ = frozen_run
    protocol = tmp_path / "protocol.txt"
    protocol.write_text("Frozen protocol.\n")
    with pytest.raises(ValueError, match="at least 60"):
        annotations.preregister(queries, protocol, tmp_path / "registration.json")
    rows = read_rows(queries)
    rows.append(deepcopy(rows[0]))
    queries.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="duplicate"):
        annotations.preregister(queries, protocol, tmp_path / "registration.json")


@pytest.mark.parametrize("mutation", ["protocol", "queries", "gates", "partial", "mode", "seed"])
def test_formal_preflight_rejects_changed_registration(formal_registration, mutation):
    from evals import run_eval

    args, rows = formal_registration
    if mutation == "protocol":
        args.protocol.write_text("changed protocol")
    elif mutation == "queries":
        args.queries.write_text(args.queries.read_text() + "\n")
    elif mutation == "partial":
        args.ids = rows[0]["id"]
    elif mutation == "mode":
        args.latency_mode = "legacy"
    elif mutation == "seed":
        args.seed += 1
    else:
        registration = json.loads(args.preregistration.read_text())
        registration["gates"]["rel_false_kill_rate_max"] = 0.30
        args.preregistration.write_text(json.dumps(registration))
    with pytest.raises(ValueError):
        run_eval.holdout_manifest(args, rows)


def test_manifest_is_persisted_before_first_delegate_and_resume_rejects_changes(
        formal_registration, monkeypatch):
    from evals import run_eval

    args, rows = formal_registration
    calls = []

    def observe_manifest():
        manifest = json.loads((args.run_dir / "manifest.json").read_text())
        assert manifest["protocol_sha256"] == annotations.sha256(args.protocol)
        assert manifest["preregistration_sha256"] == annotations.sha256(args.preregistration)
        assert manifest["acceptance_gates"] == annotations.GATES
        assert manifest["final_pass"] is False
        calls.append("delegate")

    async def pipeline(query, **kwargs):
        observe_manifest()
        return SimpleNamespace(to_dict=lambda: {"results": []})

    async def baseline(self, options):
        observe_manifest()
        return []

    monkeypatch.setattr(run_eval, "prepare_environment", lambda: (None, None, {}))
    monkeypatch.setattr(run_eval, "build_inventory", lambda **kwargs: [])
    monkeypatch.setattr(run_eval, "build_adapters", lambda *args, **kwargs: {})
    monkeypatch.setattr(run_eval, "resolve_judge", lambda **kwargs: SimpleNamespace(backend="offline"))
    monkeypatch.setattr(run_eval, "search", pipeline)
    monkeypatch.setattr(run_eval.ExaEngine, "search", baseline)
    asyncio.run(run_eval.run(args))
    assert len(calls) == 2 * len(rows)
    registration = json.loads(args.preregistration.read_text())
    registration["created_at"] = "changed after first execution"
    args.preregistration.write_text(json.dumps(registration))
    with pytest.raises(ValueError, match="preregistration_sha256"):
        asyncio.run(run_eval.run(args))
    assert len(calls) == 2 * len(rows)
    # Omitting the formal flags must not silently resume a registered run.
    args.protocol = args.preregistration = None
    with pytest.raises(ValueError, match="protocol_sha256"):
        asyncio.run(run_eval.run(args))
