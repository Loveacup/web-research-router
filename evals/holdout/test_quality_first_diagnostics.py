"""Offline consumer contract: frozen CLI reports, gross swaps and rel-band edges."""
import json
import socket
import sys

import pytest

from evals.holdout import prepare_annotations as packets
from evals.holdout import quality_first_diagnostics as diagnostic


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline diagnostic attempted network")
    for name in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)


def run_report(tmp_path, monkeypatch, candidates, labels, expected_top):
    """Build a real opaque/hash-frozen packet and consume only CLI artifacts."""
    def save(path, value):
        path.write_text(json.dumps(value))
    q = {"id": "wrr7-holdout-001", "query": "offline fixture", "lang": "en", "stratum": "academic"}
    queries = tmp_path / "queries.jsonl"
    packets.save_rows(queries, [q])
    run = tmp_path / "run"
    (run / "evidence").mkdir(parents=True)
    dropped = [c["cid"] for c in candidates if c["judgment"] and c["judgment"]["rel"] < .5]
    record = {"sample": q, "pipeline_wall_s": 1., "pipeline_error": None,
              "selection_trace": [{"candidates": candidates, "dropped_relevance": dropped, "dropped_injection": []}],
              "evidence": {"results": [candidates[i] for i in expected_top],
                           "plan": {"sources": ["exa"], "gate": None},
                           "quality": {"recovered": False}, "runs": [],
                           "diagnostics": {"judge": {"posts": 0, "failures": []}}},
              "baseline": {"wall_s": .5, "status": "ok"}, "latency": {"pipeline": {"calls": []}}}
    save(run / "evidence" / (q["id"] + ".json"), record)
    protocol, registration = tmp_path / "protocol.txt", tmp_path / "registration.json"
    protocol.write_text("frozen")
    save(registration, {"gates": packets.GATES, "queries_sha256": packets.sha256(queries),
                        "protocol_sha256": packets.sha256(protocol)})
    save(run / "manifest.json", {"queries_sha256": packets.sha256(queries), "query_count": 1,
         "public_query_count": 1, "selected_ids": [q["id"]], "protocol_sha256": packets.sha256(protocol),
         "preregistration_sha256": packets.sha256(registration), "acceptance_gates": packets.GATES,
         "inventory": [{"id": "exa", "routable": True}]})
    content, private = tmp_path / "content", tmp_path / "private"
    packets.prepare_content(queries, run, content, private)
    rows = packets.jsonl(content / "labels.template.jsonl")
    rows[0]["annotation_status"] = "complete"
    task = packets.jsonl(content / "tasks.jsonl")[0]
    labels_by_url = {c["url"]: label for c, label in zip(candidates, labels)}
    urls_by_item = {c["item_id"]: c["url"] for c in task["candidates"]}
    for c in rows[0]["candidate_labels"]:
        c.update(content_useful=labels_by_url[urls_by_item[c["item_id"]]],
                 rationale="Frozen consumer fixture; null stays unknown.")
    labels_path = content / "labels.complete.jsonl"
    packets.save_rows(labels_path, rows)
    packets.freeze_content(private, labels_path)
    gold = tmp_path / "gold.jsonl"
    packets.save_rows(gold, [{**q, "expected_sources_status": "known", "expected_sources": ["exa"],
         "expected_sources_semantics": "useful candidate set; alternatives, not mandatory fan-out",
         "gate_status": "known", "expected_gate": "allow"}])
    output = tmp_path / "output"
    args = {"queries": queries, "run_dir": run, "private_dir": private,
            "routing_gold": gold, "protocol": protocol, "preregistration": registration,
            "content_tasks": content / "tasks.jsonl", "content_labels": labels_path, "output_dir": output}
    argv = ["quality-first"]
    for key, value in args.items():
        argv.extend(["--" + key.replace("_", "-"), str(value)])
    monkeypatch.setattr(sys, "argv", argv)
    diagnostic.main()
    return output, labels_path


def candidate(i, rel):
    return {"cid": f"c{i}", "url": f"https://example.org/{i}", "title": str(i), "snippet": "frozen",
            "flags": [], "judgment": {"rel": rel, "inj": .1} if rel is not None else {}}


def test_cli_reports_rel_band_boundaries_tail_and_gross_useful_swaps(tmp_path, monkeypatch):
    # Six high-rel items, then uncertain-band useful items in fused order. Pure
    # rel admits c8/c7; uncertain admits c6/c7, replacing one already-useful item.
    rels = [.9] * 6 + [.35, .4, .5, .349999, 0., .499999]
    cs = [candidate(i, rel) for i, rel in enumerate(rels)]
    output, labels = run_report(tmp_path, monkeypatch, cs,
                               [True] * 9 + [None, False, True], list(range(6)) + [8])
    data = json.loads((output / "quality-first-diagnostics.json").read_text())
    rank = data["comparisons"]["rel_rank_top8"]["overall"]
    uncertain = data["comparisons"]["uncertain_rerank_top8"]["overall"]
    assert rank["useful_rel_rejected"] == {"n": 3, "entered_top8_n": 1, "tail_n": 2, "other_rejected_n": 0}
    assert uncertain["useful_rel_rejected"] == {"n": 3, "entered_top8_n": 2, "tail_n": 1, "other_rejected_n": 0}
    assert uncertain["rel_bands"]["[0,0.35)"]["rel_rejected"] == {"n": 2, "useful_n": 0, "not_useful_n": 1, "unknown_n": 1}
    assert uncertain["rel_bands"]["[0.35,0.4)"]["useful_rel_rejected"]["n"] == 1
    assert uncertain["rel_bands"]["[0.4,0.5)"]["useful_rel_rejected"]["n"] == 2
    assert uncertain["delivered"]["added"]["useful_n"] == 2
    assert uncertain["delivered"]["removed"]["useful_n"] == 1
    assert uncertain["delivered"]["net_useful_n"] == 1
    assert data["decision"]["new_latency_budget_s"] is None
    assert data["acceptance_gates_unchanged"] == packets.GATES
    assert data["final_pass"] is False
    report = (output / "QUALITY-FIRST-DECISION.md").read_text()
    assert "+6" in report and "+8" in report and "新的 query cohort" in report
    assert "反事实" in report and "事实无害" in report and "oldlabel" in report
    with pytest.raises(ValueError, match="never overwrite"):
        diagnostic.main()
    # A second output must still validate labels against their frozen hash.
    monkeypatch.setattr(sys, "argv", sys.argv[:-1] + [str(tmp_path / "fresh")])
    labels.write_text(labels.read_text() + "\n")
    with pytest.raises(ValueError, match="content labels hash"):
        diagnostic.main()


def test_cli_preserves_unknown_passthrough_replacement_and_empty_band(tmp_path, monkeypatch):
    output, _ = run_report(tmp_path, monkeypatch,
                           [candidate(0, .4), candidate(1, .9), candidate(2, None)],
                           [None, True, None], [1, 2])
    data = json.loads((output / "quality-first-diagnostics.json").read_text())
    summary = data["comparisons"]["rel_rank_top8"]["overall"]
    assert summary["useful_rel_rejected"]["n"] == 0
    assert summary["delivered"]["added"]["unknown_n"] == 1
    assert summary["delivered"]["after"]["unknown_n"] == 2
    assert summary["rel_bands"]["[0,0.35)"]["rel_rejected"]["n"] == 0
    assert summary["delivered"]["net_useful_n"] == 0
    assert data["comparisons"]["rel_rank_top8"]["groups"]["stratum"]["academic"] == summary
    query = data["comparisons"]["rel_rank_top8"]["queries"][0]
    assert query["added"][0]["content_useful"] is None
