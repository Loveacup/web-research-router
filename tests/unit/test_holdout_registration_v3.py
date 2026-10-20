"""V3 local-only registration fixtures are not formal samples or human gold."""
from copy import deepcopy
import json
import socket
from types import SimpleNamespace

import pytest

from evals.holdout import prepare_annotations as legacy
from evals.holdout import registration_v3 as v3


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


@pytest.fixture
def registered(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("v3 registration must not use network")

    monkeypatch.setattr(socket, "socket", no_network)
    protocol = tmp_path / "PROTOCOL-v3.md"
    protocol.write_text("# WRR7 正式新留出集协议（v3）\nFrozen offline fixture only.\n")
    frozen = tmp_path / "freeze.json"
    v3.freeze(protocol, frozen)
    rows = [{"id": f"v3-fixture-{i:03d}",
             "query": f"独立本地夹具问题 {i}" if i < 30 else f"independent local v3 fixture question {i}",
             "lang": "zh" if i < 30 else "en", "stratum": "overview", "source": "omp",
             "synthetic": False} for i in range(60)]
    rows[0]["count"] = 4
    queries = tmp_path / "queries.jsonl"
    write_rows(queries, rows)
    audit = tmp_path / "sampling.json"
    # Non-canonical formatting must retain its original byte hash as well.
    audit.write_text(json.dumps({
        "protocol_freeze_sha256": v3.sha256(frozen), "queries_sha256": v3.sha256(queries),
        "rule": v3.SAMPLING["rule"], "selection_status": "complete",
        "independence_status": "audited", "outcome_accessed": False,
        "selected_real_count": 60, "source_snapshot_sha256": "a" * 64,
        "screening_decisions_sha256": "b" * 64, "exposed_task_families_sha256": "c" * 64,
        "exclusion_reason_counts": {"privacy": 2, "prior_exposure": 5},
    }, separators=(",", ":")))
    registration = tmp_path / "registration.json"
    result = v3.preregister(queries, protocol, frozen, audit, registration, seed=19)
    args = SimpleNamespace(queries=queries, protocol=protocol, preregistration=registration,
                           ids=None, seed=19, latency_mode="isolated-paired", concurrency=1)
    return args, rows, result, frozen, audit


def test_v3_is_independent_of_legacy_gates_and_version(registered):
    args, rows, registration, frozen, audit = registered
    manifest = v3.holdout_manifest(args, rows, registration)
    assert registration["holdout_protocol_version"] == manifest["holdout_protocol_version"] == 3
    assert legacy.GATES["isolated_paired_increment_p95_s_max"] == 1.5
    assert legacy.GATES["rel_false_kill_rate_max"] == .10
    assert manifest["acceptance_gates"]["isolated_paired_increment_p95_s_max"] == 8
    assert manifest["acceptance_gates"]["capacity_normalized_useful_loss_max"] == .10
    assert manifest["acceptance_gates"]["delivery_precision_min"] == .85
    assert "rel_false_kill_rate_max" not in manifest["acceptance_gates"]
    assert manifest["request_counts"][rows[0]["id"]] == 4
    assert manifest["request_counts"][rows[1]["id"]] == 8
    assert manifest["protocol_freeze_sha256"] == v3.sha256(frozen)
    assert manifest["sampling_audit_sha256"] == v3.sha256(audit)
    assert manifest["final_pass"] is False
    assert manifest["user_audit_status"] == "pending"


@pytest.mark.parametrize("mutation", ["protocol", "queries", "gates", "version", "counts", "metrics",
                                      "partial", "mode", "seed", "freeze", "audit", "audit_hash"])
def test_v3_preflight_rejects_changes(registered, mutation):
    args, rows, registration, _, _ = registered
    if mutation == "protocol":
        args.protocol.write_text("changed protocol")
    elif mutation == "queries":
        args.queries.write_text(args.queries.read_text() + "\n")
    elif mutation == "gates":
        registration["gates"]["delivery_precision_min"] = .1
    elif mutation == "version":
        registration["holdout_protocol_version"] = 1
    elif mutation == "counts":
        registration["request_counts"][rows[0]["id"]] = 8
    elif mutation == "metrics":
        registration["metrics"]["primary_aggregation"] = "pooled"
    elif mutation == "partial":
        args.ids = rows[0]["id"]
    elif mutation == "mode":
        args.latency_mode = "legacy"
    elif mutation == "seed":
        args.seed += 1
    elif mutation == "freeze":
        registration["protocol_freeze_json"] = "{}"
    elif mutation == "audit":
        registration["sampling_audit_json"] = "{}"
    else:
        registration["sampling_audit_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        v3.holdout_manifest(args, rows, registration)


@pytest.mark.parametrize("mutation", ["short", "language_short", "duplicate", "old", "synthetic", "count"])
def test_v3_refuses_invalid_or_exposed_queries(registered, mutation):
    _, rows, _, _, _ = registered
    rows = deepcopy(rows)
    if mutation == "short":
        rows.pop()
    elif mutation == "language_short":
        rows[0]["lang"] = "en"
    elif mutation == "duplicate":
        rows[-1] = deepcopy(rows[0])
    elif mutation == "old":
        rows[0]["query"] = v3.jsonl(v3.HISTORICAL_QUERIES[1])[0]["query"]
    elif mutation == "synthetic":
        rows[0]["synthetic"] = True
    else:
        rows[0]["count"] = True
    with pytest.raises(ValueError):
        v3.validate_queries(rows)


def test_v3_freeze_and_registration_are_new_files_only(registered, tmp_path):
    args, _, _, frozen, audit = registered
    with pytest.raises(FileExistsError):
        v3.freeze(args.protocol, frozen)
    with pytest.raises(FileExistsError):
        v3.preregister(args.queries, args.protocol, frozen, audit, args.preregistration, seed=19)
    legacy_protocol = tmp_path / "legacy.txt"
    legacy_protocol.write_text("# old protocol\n")
    with pytest.raises(ValueError, match="own PROTOCOL-v3"):
        v3.freeze(legacy_protocol, tmp_path / "not-v3.json")


@pytest.mark.parametrize("key,value", [("outcome_accessed", True), ("independence_status", "unknown"),
                                       ("screening_decisions_sha256", "missing"),
                                       ("exclusion_reason_counts", {"privacy": -1}),
                                       ("selection_status", "incomplete")])
def test_v3_sampling_audit_cannot_be_omitted_or_score_driven(registered, tmp_path, key, value):
    args, _, _, frozen, audit = registered
    data = json.loads(audit.read_text())
    data[key] = value
    audit.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        v3.preregister(args.queries, args.protocol, frozen, audit, tmp_path / "invalid.json", seed=19)


def test_live_harness_dispatches_v3_without_relabeling_it(registered):
    from evals import run_eval

    args, rows, _, _, _ = registered
    manifest = run_eval.holdout_manifest(args, rows)
    assert manifest["holdout_protocol_version"] == 3
    assert manifest["acceptance_gates"] == v3.GATES_V3
