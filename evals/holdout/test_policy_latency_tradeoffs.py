"""Offline diagnostic contracts: safety, all-stage losses and boundary accounting."""
import copy
import itertools
import json
import socket
import sys

import pytest

from evals.holdout import policy_latency_tradeoffs as diagnostic
from evals.holdout import prepare_annotations as packets


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline diagnostic attempted network")
    for name in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)


def candidate(i, rel=.7, useful=True, inj=.1, **kwargs):
    return {"cid": f"c{i}", "url": f"https://example.org/{i}", "title": str(i),
            "snippet": "frozen", "flags": [], "judgment": {"rel": rel, "inj": inj},
            "content_useful": useful, **kwargs}


def row(candidates, selected):
    return {"status": "ok", "frozen_candidate_n": len(candidates), "candidates": candidates, **selected}


def test_rank_zero_veto_does_not_hide_useful_topk_loss():
    cs = [candidate(i, rel=.9 - i * .05) for i in range(12)]
    original = copy.deepcopy(cs)
    baseline = diagnostic.summarize([row(cs, diagnostic.select(cs, diagnostic.POLICIES[0]))])
    rank = diagnostic.summarize([row(cs, diagnostic.select(cs, diagnostic.POLICIES[1]))])
    assert rank["losses"]["threshold_rejected"]["useful_n"] == 0
    assert rank["losses"]["topk_truncated"]["useful_n"] == 4
    assert rank["all_loss_useful_n"] == baseline["all_loss_useful_n"] == 4
    assert rank["top8"]["useful_n"] == baseline["top8"]["useful_n"] == 8
    assert rank["pool"]["recall"] == 1
    assert cs == original


@pytest.mark.parametrize("policy", diagnostic.POLICIES)
def test_safety_and_protected_passthrough_unchanged(policy):
    cs = [candidate(0, inj=.5), candidate(1, rel=.99),
          candidate(2, judgment={}, flags=["unjudged:sensitive"]),
          candidate(3, judgment={}, flags=["unjudged:private"], privacy_class="local"),
          candidate(4, inj=.3), candidate(5, judgment={}, flags=["unjudged:degraded"])]
    result = diagnostic.select(cs, policy, dropped_injection=["c1"])
    assert result["safety_rejected"] == ["c0", "c1"]
    assert result["pool"] == ["c4", "c2", "c3", "c5"]
    assert diagnostic.summarize([row(cs, result)])["all_loss_useful_n"] == 2
    with pytest.raises(ValueError, match="protected"):
        diagnostic.select([candidate(0, flags=["unjudged:sensitive"])], policy)
    with pytest.raises(ValueError, match="sensitive-query"):
        diagnostic.select(cs, policy, gate="sensitive_query")
    assert diagnostic.select(cs[2:4], policy, gate="local")["pool"] == ["c2", "c3"]


def test_uncertain_is_non_oracle_rerank_without_pre_topk_deletion():
    cs = [candidate(0, rel=.36, useful=False), candidate(1, rel=.64),
          candidate(2, rel=.65), candidate(3, rel=.34), candidate(4, rel=.35)]
    result = diagnostic.select(cs, diagnostic.POLICIES[2])
    assert result["pool"] == ["c2", "c0", "c1", "c4", "c3"]
    assert not result["threshold_rejected"]
    for c in cs:
        c["content_useful"] = not c["content_useful"]
    assert diagnostic.select(cs, diagnostic.POLICIES[2]) == result


@pytest.mark.parametrize("judgment", [{"rel": .7}, {"rel": float("nan"), "inj": .1},
                                     {"rel": .7, "inj": -1}])
def test_partial_or_invalid_judgment_cannot_infer_safety(judgment):
    with pytest.raises(ValueError, match="cannot infer safety"):
        diagnostic.select([candidate(0, judgment=judgment)], diagnostic.POLICIES[1])


def test_unknown_recall_bounds_match_exhaustive_independent_assignments():
    cs = [candidate(0, useful=True), candidate(1, useful=True),
          candidate(2, useful=None), candidate(3, useful=None), candidate(4, useful=None)]
    selected = {"pool": ["c0", "c2"], "top8": ["c0", "c2"],
                "safety_rejected": ["c1"], "threshold_rejected": [], "topk_truncated": ["c3", "c4"]}
    summary = diagnostic.summarize([row(cs, selected)])
    recalls = [(1 + a) / (2 + a + b + c) for a, b, c in itertools.product((0, 1), repeat=3)]
    assert summary["pool"]["recall_bounds"] == {"lower": min(recalls), "upper": max(recalls)}
    assert summary["top8"]["precision_bounds"] == {"lower": .5, "upper": 1.}
    assert summary["all_loss_rate_bounds"]["lower"] == pytest.approx(1 - max(recalls))
    assert summary["all_loss_rate_bounds"]["upper"] == pytest.approx(1 - min(recalls))


@pytest.mark.parametrize("kept,unknown,expected", [
    (0, 0, {"lower": None, "upper": None}),
    (0, 2, {"lower": 0., "upper": 0.}),
    (2, 2, {"lower": 1., "upper": 1.}),
    (1, 2, {"lower": 0., "upper": 1.}),
])
def test_all_unknown_bounds_condition_on_nonempty_useful_denominator(kept, unknown, expected):
    assert diagnostic.recall_bounds(0, 0, kept, unknown) == expected


def test_empty_failed_query_is_not_perfect_recall():
    selected = diagnostic.select([], diagnostic.POLICIES[0])
    empty = row([], selected)
    empty["status"] = "schema_error"
    summary = diagnostic.summarize([empty])
    assert summary["pool"]["recall"] is None
    assert summary["top8"]["precision_bounds"] == {"lower": None, "upper": None}
    assert summary["top8"]["useful_per_registered_query"] == 0
    assert summary["failed_or_missing_query_n"] == 1


def call(boundary, start, end, status="ok"):
    return {"boundary": boundary, "queue_start_monotonic_s": start,
            "service_end_monotonic_s": end, "queue_s": 0., "status": status}


def test_source_concurrency_and_failed_judge_decomposition():
    calls = [call("judge", 0, 1), call("source:fast", 1, 2),
             call("source:slow", 1.1, 5), call("judge", 5.2, 7),
             call("judge", 7.1, 9, "error")]
    record = {"sample": {"id": "q"}, "pipeline_wall_s": 10., "pipeline_error": None,
              "baseline": {"wall_s": 2.}, "latency": {"pipeline": {"calls": calls},
              "baseline": {"calls": [call("direct_exa", 10, 12)]}}}
    out = diagnostic.decompose(record)
    assert out["regular_three"] is True
    assert out["judge_serial_wall_s"] == pytest.approx(4.7)
    assert out["source_critical_path_s"] == 4
    assert out["source_adapter_wall_sum_s"] == pytest.approx(4.9)
    assert out["orchestration_residual_s"] == pytest.approx(1.3)
    assert out["paired_increment_s"] == 8
    assert out["judge_statuses"] == {"ok": 2, "error": 1}
    assert out["provider_queue_s"] == "unknown"
    assert out["source_waves"][0]["critical_tail_boundary"] == "source:slow"
    record["latency"]["pipeline"]["calls"] = list(reversed(calls))
    assert diagnostic.decompose(record) == out


def test_recovery_waves_sum_critical_paths_not_all_adapter_times():
    calls = [call("judge", 0, 1), call("source:a", 1, 2), call("source:b", 1, 3),
             call("judge", 3, 4), call("judge", 4, 5),
             call("source:c", 5, 7), call("judge", 7, 8)]
    result = diagnostic.decompose({"sample": {"id": "q"}, "pipeline_wall_s": 8,
                                   "latency": {"pipeline": {"calls": calls}}})
    assert not result["regular_three"]
    assert result["source_wave_n"] == 2
    assert result["source_critical_path_s"] == 4
    assert result["judge_serial_wall_s"] == 4
    assert result["orchestration_residual_s"] == 0
    assert diagnostic.decompose({"sample": {"id": "missing"}})["status"] == "unknown_boundaries"


@pytest.fixture
def frozen_input(tmp_path):
    """Small real score_frozen packet, including opaque mapping/hash freeze."""
    def save(path, value):
        path.write_text(json.dumps(value))
    q = {"id": "wrr7-holdout-001", "query": "offline query", "lang": "en", "stratum": "academic"}
    query_path = tmp_path / "queries.jsonl"
    packets.save_rows(query_path, [q])
    cs = [candidate(0, rel=.4), candidate(1, rel=.8)]
    for c in cs:
        del c["content_useful"]
    record = {"sample": q, "pipeline_wall_s": 1., "pipeline_error": None,
              "selection_trace": [{"candidates": cs, "dropped_relevance": ["c0"], "dropped_injection": []}],
              "evidence": {"results": cs[1:], "plan": {"sources": ["exa"], "gate": None},
                           "quality": {"recovered": False}, "runs": [],
                           "diagnostics": {"judge": {"posts": 0, "failures": []}}},
              "baseline": {"wall_s": .5, "status": "ok"}, "latency": {"pipeline": {"calls": []}}}
    run = tmp_path / "run"
    (run / "evidence").mkdir(parents=True)
    save(run / "evidence" / (q["id"] + ".json"), record)
    protocol, registration = tmp_path / "protocol.txt", tmp_path / "registration.json"
    protocol.write_text("frozen")
    save(registration, {"gates": packets.GATES, "queries_sha256": packets.sha256(query_path),
                        "protocol_sha256": packets.sha256(protocol)})
    save(run / "manifest.json", {"queries_sha256": packets.sha256(query_path), "query_count": 1,
          "public_query_count": 1, "selected_ids": [q["id"]], "protocol_sha256": packets.sha256(protocol),
          "preregistration_sha256": packets.sha256(registration), "acceptance_gates": packets.GATES,
          "inventory": [{"id": "exa", "routable": True}]})
    content, private = tmp_path / "content", tmp_path / "private"
    packets.prepare_content(query_path, run, content, private)
    labels = packets.jsonl(content / "labels.template.jsonl")
    for label in labels:
        label["annotation_status"] = "complete"
        for c in label["candidate_labels"]:
            c.update(content_useful=True, rationale="Frozen fixture.")
    labels_path = content / "labels.complete.jsonl"
    packets.save_rows(labels_path, labels)
    packets.freeze_content(private, labels_path)
    gold = tmp_path / "gold.jsonl"
    packets.save_rows(gold, [{**q, "expected_sources_status": "known", "expected_sources": ["exa"],
        "expected_sources_semantics": "useful candidate set; alternatives, not mandatory fan-out",
        "gate_status": "known", "expected_gate": "allow"}])
    return {"queries": query_path, "run_dir": run, "private_dir": private, "protocol": protocol,
            "preregistration": registration, "routing_gold": gold,
            "content_tasks": content / "tasks.jsonl", "content_labels": labels_path}


def test_analysis_validates_real_opaque_mapping_and_keeps_formal_failure(frozen_input):
    result = diagnostic.analyze(**frozen_input)
    assert result["final_pass"] is False
    assert result["posthoc"] is True
    assert result["acceptance_gates_unchanged"] == packets.GATES
    assert result["frozen_official_rel_metric"]["false_kill_n"] == 1
    assert result["policies"][diagnostic.POLICIES[0]]["overall"]["all_loss_useful_n"] == 1
    assert result["policies"][diagnostic.POLICIES[1]]["overall"]["top8"]["useful_n"] == 2
    frozen_input["content_labels"].write_text(frozen_input["content_labels"].read_text() + "\n")
    with pytest.raises(ValueError, match="content labels hash"):
        diagnostic.analyze(**frozen_input)


def test_cli_outputs_new_directory_only(frozen_input, tmp_path, monkeypatch):
    output = tmp_path / "diagnostic"
    argv = ["diagnostic"]
    for key, value in {**frozen_input, "output_dir": output}.items():
        argv.extend(["--" + key.replace("_", "-"), str(value)])
    monkeypatch.setattr(sys, "argv", argv)
    loaded_before = set(sys.modules)
    diagnostic.main()
    data = json.loads((output / "diagnostics.json").read_text())
    assert data["diagnostic_only"] is True
    report = (output / "POLICY-LATENCY-TRADEOFFS.md").read_text()
    assert "不是数学不可能证明" in report
    assert "不得把 rel 拒绝改名 top-k" in report
    assert "not_established_for_new_rankings" == data["sufficiency_status"]
    with pytest.raises(ValueError, match="never overwrite"):
        diagnostic.main()
    assert not any(name == "wrr" or name.startswith("wrr.") for name in set(sys.modules) - loaded_before)
