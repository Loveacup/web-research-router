"""Offline consumer contract: frozen identities, tri-state metrics and exact scopes."""
import json
from pathlib import Path
import socket
import sys

import pytest

from evals.holdout import prepare_annotations as packets
from evals.holdout import score_frozen as scoring


def save(path, value):
    path.write_text(json.dumps(value))


def rows(path, values):
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("frozen scoring attempted network access")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    queries = [{"id": f"wrr7-holdout-{i:03}", "query": f"query {i}", "lang": "en",
                "stratum": "academic", "source": "omp", "synthetic": False} for i in range(1, 3)]
    candidates = [{"cid": f"c{i}", "url": f"https://example.org/{i}", "title": f"title {i}",
                   "snippet": "frozen", "flags": [], "judgment": {"rel": .2 if i < 2 else .9}}
                  for i in range(4)]
    record = {"sample": queries[0], "pipeline_error": None, "pipeline_wall_s": 2.,
              "selection_trace": [{"candidates": [dict(candidates[2], cid="c0")],
                                   "dropped_relevance": ["c0"]},
                                  {"candidates": candidates, "dropped_relevance": ["c0", "c1"],
                                   "dropped_injection": []}],
              "evidence": {"results": candidates[2:], "plan": {"sources": ["exa"], "gate": None},
                           "quality": {"recovered": False, "sufficiency": {"scope": "public_only", "enough": .8}},
                           "diagnostics": {"judge": {"posts": 2, "failures": []}}, "runs": []},
              "latency": {"pipeline": {"calls": [{"boundary": "judge", "status": "ok", "delegate_started": True}]}},
              "baseline": {"status": "ok", "wall_s": 1.}}
    failed = {"sample": queries[1], "pipeline_error": "schema_error", "pipeline_wall_s": .1,
              "selection_trace": [], "evidence": None, "baseline": {"status": "ok", "wall_s": 1.},
              "latency": {"pipeline": {"calls": []}}}
    run = tmp_path / "run"
    (run / "evidence").mkdir(parents=True)
    for value in (record, failed):
        save(run / "evidence" / (value["sample"]["id"] + ".json"), value)
    query_path = tmp_path / "queries.jsonl"
    rows(query_path, queries)
    protocol = tmp_path / "protocol.txt"
    protocol.write_text("frozen protocol")
    registration = tmp_path / "registration.json"
    save(registration, {"gates": packets.GATES, "queries_sha256": packets.sha256(query_path),
                        "protocol_sha256": packets.sha256(protocol)})
    save(run / "manifest.json", {"queries_sha256": packets.sha256(query_path),
         "query_count": 2, "public_query_count": 2, "selected_ids": [q["id"] for q in queries],
         "protocol_sha256": packets.sha256(protocol), "preregistration_sha256": packets.sha256(registration),
         "acceptance_gates": packets.GATES, "latency_mode": "isolated-paired", "user_audit_status": "pending",
         "inventory": [{"id": "exa", "routable": True}, {"id": "academic", "routable": True}]})
    content, private = tmp_path / "content", tmp_path / "coordinator"
    packets.prepare_content(query_path, run, content, private)
    index = json.loads((private / "index.json").read_text())
    labels = packets.jsonl(content / "labels.template.jsonl")
    values = {"c0": True, "c1": None, "c2": True, "c3": False}
    for label in labels:
        label["annotation_status"] = "complete"
        mapping = next(q["mapping"] for q in index["queries"] if q["task_id"] == label["task_id"])
        for item in label["candidate_labels"]:
            cid = next(m["cid"] for m in mapping if m["item_id"] == item["item_id"])
            item.update(content_useful=values[cid], rationale="Frozen evidence judgment.")
    labels_path = content / "labels.complete.jsonl"
    rows(labels_path, labels)
    packets.freeze_content(private, labels_path)
    gold_path = tmp_path / "routing.jsonl"
    rows(gold_path, [{**q, "expected_sources_status": "known", "expected_sources": ["academic", "exa"],
         "expected_sources_semantics": "useful candidate set; alternatives, not mandatory fan-out",
         "gate_status": "known", "expected_gate": "allow"} for q in queries])
    return {"queries": query_path, "run_dir": run, "private_dir": private,
            "routing_gold": gold_path, "protocol": protocol, "preregistration": registration}


def test_consumer_maps_opaque_ids_and_preserves_unknown(frozen):
    content, routing = scoring.score_run(**frozen)
    metric = content["overall"]
    assert (metric["false_kill_n"], metric["useful_n"], metric["unknown_content_n"]) == (1, 2, 1)
    assert metric["false_kill_rate"] == .5
    assert metric["false_kill_bounds"] == {"lower": .5, "upper": pytest.approx(2 / 3)}
    assert (metric["false_keep_n"], metric["delivered_n"], metric["false_keep_rate"]) == (1, 2, .5)
    item = next(c for q in content["queries"] for c in q["candidates"] if c["cid"] == "c0")
    assert item["url"] == "https://example.org/0" and item["rel_killed"] is True
    assert content["overall"]["failed_or_missing_query_n"] == 1
    assert content["sufficiency"]["status"] == "not_established"
    assert content["final_pass"] is False
    assert routing["overall"]["useful_source_hit_rate"] == .5
    assert routing["overall"]["strict_old_formula_candidate_coverage"] == .25
    assert routing["overall"]["known_routing_n"] == 2
    assert routing["latency_summary"]["increment_failure_penalized_p95_s"] == "failed_or_unknown"
    assert routing["posts"]["unknown_query_n"] == 1


@pytest.mark.parametrize("target", ["labels", "tasks", "evidence", "queries", "protocol", "preregistration"])
def test_consumer_refuses_changed_frozen_bytes(frozen, target):
    private = frozen["private_dir"]
    index = json.loads((private / "index.json").read_text())
    paths = {"labels": Path(json.loads((private / "content-freeze.json").read_text())["labels_path"]),
             "tasks": Path(index["content_tasks_path"]),
             "evidence": frozen["run_dir"] / "evidence" / "wrr7-holdout-001.json",
             **{key: frozen[key] for key in ("queries", "protocol", "preregistration")}}
    with paths[target].open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="changed|hash"):
        scoring.score_run(**frozen)


def test_missing_evidence_is_unknown_not_zero_error(frozen):
    (frozen["run_dir"] / "evidence" / "wrr7-holdout-001.json").unlink()
    content, routing = scoring.score_run(**frozen)
    assert content["overall"]["false_kill_rate"] is None
    assert content["overall"]["failed_or_missing_query_n"] == 2
    assert routing["latency_summary"]["missing_pair_n"] == 1
    assert routing["overall"]["known_routing_n"] == 2
    assert routing["overall"]["useful_source_hit_rate"] == 0


def test_sufficiency_requires_hash_and_exact_reconstructible_scope(frozen, tmp_path):
    packet = tmp_path / "enough"
    packets.prepare_sufficiency(frozen["private_dir"], packet)
    labels = packets.jsonl(packet / "labels.template.jsonl")
    for row in labels:
        row.update(annotation_status="complete", enough=True, rationale="Set independently sufficient.")
    labels_path = packet / "complete.jsonl"
    rows(labels_path, labels)
    kwargs = dict(sufficiency_labels=labels_path, sufficiency_tasks=packet / "tasks.jsonl")
    with pytest.raises(ValueError, match="labels.*hash"):
        scoring.score_run(**frozen, **kwargs)
    kwargs["sufficiency_labels_sha256"] = packets.sha256(labels_path)
    content, _ = scoring.score_run(**frozen, **kwargs)
    metric = content["sufficiency"]
    assert metric["brier_n"] == 1
    assert metric["brier"] == pytest.approx(.04)
    assert metric["by_scope"]["full_delivery"]["eligible_n"] == 1
    assert metric["by_scope"]["full_delivery"]["all_public_n"] == 2
    assert content["final_pass"] is False
    with (packet / "tasks.jsonl").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="sufficiency tasks.*changed"):
        scoring.score_run(**frozen, **kwargs)


def test_relocated_identical_packets_do_not_depend_on_tmp_paths(frozen, tmp_path):
    index = json.loads((frozen["private_dir"] / "index.json").read_text())
    freeze = json.loads((frozen["private_dir"] / "content-freeze.json").read_text())
    tasks, labels = tmp_path / "persisted-tasks.jsonl", tmp_path / "persisted-labels.jsonl"
    tasks.write_bytes(Path(index["content_tasks_path"]).read_bytes())
    labels.write_bytes(Path(freeze["labels_path"]).read_bytes())
    Path(index["content_tasks_path"]).unlink()
    Path(freeze["labels_path"]).unlink()
    content, _ = scoring.score_run(**frozen, content_tasks=tasks, content_labels=labels)
    assert content["overall"]["false_kill_n"] == 1


def test_cli_is_offline_and_never_overwrites_results(frozen, tmp_path, monkeypatch):
    output = tmp_path / "scored"
    argv = ["score_frozen.py"]
    for name, value in frozen.items():
        argv += ["--" + name.replace("_", "-"), str(value)]
    argv += ["--output-dir", str(output)]
    monkeypatch.setattr(sys, "argv", argv)
    scoring.main()
    original = (output / "content.json").read_bytes()
    assert json.loads(original)["final_pass"] is False
    with pytest.raises(ValueError, match="overwrite"):
        scoring.main()
    assert (output / "content.json").read_bytes() == original


def test_unknown_routing_is_excluded_and_failed_known_plan_is_miss(frozen):
    gold = packets.jsonl(frozen["routing_gold"])
    gold[0].update(expected_sources_status="unknown", expected_sources=None)
    rows(frozen["routing_gold"], gold)
    _, routing = scoring.score_run(**frozen)
    assert routing["overall"]["known_routing_n"] == 1
    assert routing["overall"]["unknown_routing_n"] == 1
    assert routing["overall"]["useful_source_hit_rate"] == 0
    assert routing["overall"]["strict_old_formula_candidate_coverage"] == 0


def test_mislabeled_scope_binding_is_rejected(frozen, tmp_path):
    packet = tmp_path / "sufficiency"
    packets.prepare_sufficiency(frozen["private_dir"], packet)
    labels = packets.jsonl(packet / "labels.template.jsonl")
    for row in labels:
        row.update(annotation_status="complete", enough=None, rationale="Unknown remains unknown.")
    label_path = packet / "complete.jsonl"
    rows(label_path, labels)
    index_path = frozen["private_dir"] / "sufficiency-index.json"
    index = json.loads(index_path.read_text())
    index["bindings"][0]["matches_prediction"] = False
    save(index_path, index)
    with pytest.raises(ValueError, match="scope match"):
        scoring.score_run(**frozen, sufficiency_labels=label_path,
                          sufficiency_tasks=packet / "tasks.jsonl",
                          sufficiency_labels_sha256=packets.sha256(label_path))


def test_empty_and_unknown_only_content_do_not_imply_zero_error():
    detail = {"status": "missing_evidence", "frozen_candidate_n": 0, "candidates": []}
    assert scoring.content_summary([detail])["false_kill_rate"] is None
    assert scoring.content_summary([detail])["false_keep_rate"] is None
    item = {"content_useful": None, "rel_killed": True, "delivered": False}
    detail.update(status="ok", candidates=[item])
    metric = scoring.content_summary([detail])
    assert metric["false_kill_rate"] is None
    assert metric["false_kill_bounds"] == {"lower": 1., "upper": 1.}


def test_sufficiency_scores_distinct_targets_separately(frozen, tmp_path):
    private = frozen["private_dir"]
    index_path = private / "index.json"
    index = json.loads(index_path.read_text())
    record = index["queries"][0]["record"]
    record["evidence"]["results"][0]["flags"] = ["inj:suspect"]
    index["queries"][0]["record"] = record
    save(index_path, index)
    packet = tmp_path / "enough-distinct"
    packets.prepare_sufficiency(private, packet)
    bindings = json.loads((private / "sufficiency-index.json").read_text())["bindings"]
    labels = packets.jsonl(packet / "labels.template.jsonl")
    for label in labels:
        binding = next(b for b in bindings if b["task_id"] == label["task_id"])
        label.update(annotation_status="complete", enough=binding["scope_key"] == "full_delivery",
                     rationale="Independent target-specific decision.")
    label_path = packet / "complete.jsonl"
    rows(label_path, labels)
    records = {entry["id"]: entry["record"] for entry in index["queries"]}
    result = scoring.sufficiency_summary(private, packets.jsonl(frozen["queries"]), records,
        label_path, packet / "tasks.jsonl", packets.sha256(label_path))
    assert result["brier_target"] == "full_delivery"
    assert result["brier"] is None and result["brier_n"] == 0
    assert result["by_scope"]["public_assessed"]["brier"] == pytest.approx(.64)
    assert result["by_scope"]["public_assessed"]["n"] == 1


def test_missing_raw_evidence_remains_unscored_with_frozen_sufficiency(frozen, tmp_path):
    packet = tmp_path / "enough-missing"
    packets.prepare_sufficiency(frozen["private_dir"], packet)
    labels = packets.jsonl(packet / "labels.template.jsonl")
    for label in labels:
        label.update(annotation_status="complete", enough=True, rationale="Independent set decision.")
    labels_path = packet / "complete.jsonl"
    rows(labels_path, labels)
    (frozen["run_dir"] / "evidence" / "wrr7-holdout-001.json").unlink()
    content, _ = scoring.score_run(**frozen, sufficiency_labels=labels_path,
        sufficiency_tasks=packet / "tasks.jsonl", sufficiency_labels_sha256=packets.sha256(labels_path))
    assert content["sufficiency"]["brier"] is None
    assert content["sufficiency"]["brier_n"] == 0
    assert content["sufficiency"]["by_scope"]["full_delivery"]["missing_reasons"]["missing_evidence"] == 1
