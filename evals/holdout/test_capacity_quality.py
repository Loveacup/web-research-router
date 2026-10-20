"""Consumer examples for the offline v3 capacity-quality contract."""
import itertools
import json
import socket

import pytest

from evals.holdout import capacity_quality as quality


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("capacity quality attempted network")
    for name in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)


def row(labels, delivered=8, status="ok", qid="q"):
    return {"id": qid, "status": status, "candidates": [
        {"cid": f"c{i}", "url": f"https://example.test/{i}",
         "content_useful": label, "delivered": i < delivered, "rel_killed": False}
        for i, label in enumerate(labels)]}


def report(*rows, capacity=8):
    return quality.score_capacity({"queries": list(rows)}, capacity)


@pytest.mark.parametrize("labels,delivered,loss,precision", [
    ([True] * 16, 8, 0, 1),
    ([True] * 4 + [False] * 4 + [True] * 4, 8, .5, .5),
    ([True] * 3, 3, 0, 1),
    ([True] * 8, 4, .5, 1),
])
def test_consumer_capacity_examples(labels, delivered, loss, precision):
    result = report(row(labels, delivered))
    assert result["macro_loss_bounds"]["lower"] == loss
    assert result["macro_loss_bounds"]["upper"] == loss
    assert result["delivery_precision"] == precision
    assert result["capacity"] == 8
    assert result["final_pass"] is False


def test_unknown_extrema_are_not_uniform_assignments():
    result = report(row([True] * 4 + [None] * 8, delivered=8))
    assert result["macro_loss_bounds"]["lower"] == 0
    assert result["macro_loss_bounds"]["upper"] == .5
    assert result["macro_precision_bounds"] == {"lower": .5, "upper": 1}
    assert result["metric_status"] == "blocked_not_applicable"


@pytest.mark.parametrize("labels", [[], [False] * 3, [None] * 3])
def test_zero_useful_is_explicit_coverage(labels):
    result = report(row(labels, delivered=0))
    assert result["coverage"]["possibly_zero_useful_query_n"] == 1
    assert result["delivery_precision"] is None
    assert result["metric_status"] == "blocked_not_applicable"
    if not any(label is None for label in labels):
        assert result["macro_loss_bounds"]["lower"] is None


@pytest.mark.parametrize("status", ["missing_evidence", "timeout", "judge_failure"])
def test_failed_queries_remain_in_coverage(status):
    result = report(row([True] * 8), row([], status=status, qid="failed"))
    assert len(result["queries"]) == 2
    assert result["coverage"]["failed_or_missing_query_n"] == 1
    assert result["metric_status"] == "blocked_not_applicable"


@pytest.mark.parametrize("key", ["cid", "url"])
def test_duplicate_candidate_identity_rejected(key):
    sample = row([True, True])
    sample["candidates"][1][key] = sample["candidates"][0][key]
    with pytest.raises(ValueError, match="duplicate"):
        report(sample)


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
def test_invalid_capacity_rejected(capacity):
    with pytest.raises(ValueError, match="capacity"):
        report(row([]), capacity=capacity)


def test_over_capacity_rejected():
    with pytest.raises(ValueError, match="exceeds"):
        report(row([True] * 9, delivered=9))


def test_macro_is_primary_pooled_is_extra():
    result = report(row([True] * 8, delivered=4), row([True], delivered=1, qid="small"))
    assert result["macro_loss_bounds"]["lower"] == .25
    assert result["pooled_loss_bounds"]["lower"] == pytest.approx(4 / 9)


def test_precision_macro_not_pooled():
    result = report(row([True], delivered=1), row([False] * 8, qid="noise"))
    assert result["macro_precision_bounds"]["lower"] == .5
    assert result["pooled_precision_bounds"]["lower"] == pytest.approx(1 / 9)


def test_bounds_match_exhaustive_small_unknown_assignments():
    templates = [row([True, None, None], delivered=1), row([None, False, None], delivered=2, qid="b")]
    actual = report(*templates, capacity=2)
    macro_losses, pooled_losses = [], []
    for bits in itertools.product((False, True), repeat=4):
        assigned = json.loads(json.dumps(templates))
        choices = iter(bits)
        for sample in assigned:
            for candidate in sample["candidates"]:
                if candidate["content_useful"] is None:
                    candidate["content_useful"] = next(choices)
        concrete = report(*assigned, capacity=2)
        macro_losses.append(concrete["macro_loss_bounds"]["lower"])
        pooled_losses.append(concrete["pooled_loss_bounds"]["lower"])
    assert actual["macro_loss_bounds"]["lower"] == min(macro_losses)
    assert actual["macro_loss_bounds"]["upper"] == max(macro_losses)
    assert actual["pooled_loss_bounds"]["lower"] == min(pooled_losses)
    assert actual["pooled_loss_bounds"]["upper"] == max(pooled_losses)


@pytest.mark.parametrize("identity", ["url", "cid"])
def test_raw_delivery_duplicates_rejected(tmp_path, monkeypatch, identity):
    sample = row([True, True], delivered=2)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    delivered = [{"url": c["url"], "cid": c["cid"]} for c in sample["candidates"]]
    delivered[1][identity] = delivered[0][identity]
    (evidence_dir / "q.json").write_text(json.dumps({"evidence": {"results": delivered}}))
    monkeypatch.setattr(quality.score_frozen, "score_run", lambda **kwargs: ({"queries": [sample]}, {}))
    with pytest.raises(ValueError, match="duplicate"):
        quality.score_run(run_dir=tmp_path)
