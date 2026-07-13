"""WRR P1 3b.1: DecisionEvidence schema + privacy-bounded JSONL sinks.

RED-first contracts. No routing, no live engines. Pure schema + best-effort
append. Evidence carries only a whitelisted, privacy-safe projection of a Stage-S
routing decision — never queries, hashes, snippets, bodies, or secrets.
"""
from __future__ import annotations

import json
import multiprocessing
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from wrr.schemas import (
    DecisionEvidence,
    RouteTrace,
    ShadowComparison,
)
from wrr.runtime.decision_evidence import (
    DecisionEvidenceSink,
    JsonlDecisionEvidenceSink,
    NoopDecisionEvidenceSink,
    decision_evidence_path,
)


EVIDENCE_KEYS = {
    "schema_version",
    "request_key",
    "recorded_at",
    "stage",
    "mode",
    "terminal",
    "outcome",
    "actual_provider",
    "result_count",
    "quality_verdict",
    "route_elapsed_ms",
}


def _valid_uuid4() -> str:
    return str(uuid.uuid4())


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _mk_evidence(**overrides) -> DecisionEvidence:
    values = dict(
        request_key=_valid_uuid4(),
        recorded_at=_utc_now(),
        mode="research",
        terminal="routed",
        outcome="success",
        actual_provider="exa",
        result_count=7,
        quality_verdict="complete",
        route_elapsed_ms=42.5,
    )
    values.update(overrides)
    return DecisionEvidence(**values)


# ── RouteTrace back-compat ───────────────────────────────────────────
def test_old_route_trace_construction_and_to_dict_unchanged():
    trace = RouteTrace(mode="research", selected_engines=["exa"], elapsed_ms=12.3)
    d = trace.to_dict()
    assert d["mode"] == "research"
    assert d["selected_engines"] == ["exa"]
    assert d["elapsed_ms"] == 12.3
    assert "decision_evidence" not in d


def test_route_trace_exposes_decision_evidence_only_when_present():
    trace = RouteTrace(elapsed_ms=1.0)
    assert "decision_evidence" not in trace.to_dict()

    trace.decision_evidence = _mk_evidence()
    d = trace.to_dict()
    assert "decision_evidence" in d
    assert set(d["decision_evidence"]) == EVIDENCE_KEYS


# ── Evidence exact keys + sentinel privacy ───────────────────────────
def test_evidence_to_dict_has_exact_whitelist_keys():
    d = _mk_evidence().to_dict()
    assert set(d) == EVIDENCE_KEYS
    assert d["schema_version"] == 1
    assert d["stage"] == "S"
    assert d["outcome"] == "success"
    assert d["result_count"] == 7
    assert d["route_elapsed_ms"] == 42.5


def test_evidence_nests_shadow_comparison_fields_when_present():
    shadow = ShadowComparison(
        code="E0",
        safe=True,
        legacy_provider_ids=("exa", "brave"),
        descriptor_provider_ids=("exa", "brave"),
    )
    d = _mk_evidence(shadow_comparison=shadow).to_dict()
    assert set(d) == EVIDENCE_KEYS | {"shadow_comparison"}
    nested = d["shadow_comparison"]
    assert nested["code"] == "E0"
    assert nested["safe"] is True
    assert nested["legacy_provider_ids"] == ["exa", "brave"]
    assert nested["descriptor_provider_ids"] == ["exa", "brave"]


def test_evidence_never_carries_sensitive_fields():
    # Sentinel privacy: no query/hash/snippet/body/secret leaks into the record.
    import dataclasses

    d = _mk_evidence().to_dict()
    serialized = json.dumps(d)
    for forbidden in ("query", "hash", "snippet", "body", "secret", "token", "header"):
        assert forbidden not in serialized
    # DecisionEvidence has no field plumbing for such data.
    field_names = {f.name for f in dataclasses.fields(DecisionEvidence)}
    for forbidden in ("query", "query_hash", "snippet", "body", "secret", "headers", "token"):
        assert forbidden not in field_names


# ── Validation ───────────────────────────────────────────────────────
def test_invalid_uuid_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(request_key="not-a-uuid")


def test_non_v4_uuid_rejected():
    v1 = "00000000-0000-1000-8000-000000000000"  # version nibble = 1
    with pytest.raises(ValueError):
        _mk_evidence(request_key=v1)


def test_invalid_outcome_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(outcome="partial")


def test_invalid_stage_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(stage="T")


def test_negative_count_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(result_count=-1)


def test_boolean_count_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(result_count=True)


def test_negative_latency_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(route_elapsed_ms=-0.1)


def test_non_finite_latency_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(route_elapsed_ms=float("inf"))
    with pytest.raises(ValueError):
        _mk_evidence(route_elapsed_ms=float("nan"))


def test_wrong_schema_version_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(schema_version=2)


def test_valid_outcomes_accepted():
    for outcome in ("success", "empty", "error"):
        assert _mk_evidence(outcome=outcome).outcome == outcome


# ── recorded_at: RFC3339 UTC 'Z' ─────────────────────────────────────
def test_recorded_at_utc_z_accepted():
    assert _mk_evidence(recorded_at="2026-07-14T03:49:22.123456Z").recorded_at.endswith("Z")
    assert _mk_evidence(recorded_at="2026-07-14T03:49:22Z").recorded_at.endswith("Z")


def test_recorded_at_empty_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(recorded_at="")


def test_recorded_at_non_date_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(recorded_at="not-a-timestamp")
    with pytest.raises(ValueError):
        _mk_evidence(recorded_at="2026-07-14Z")


def test_recorded_at_missing_z_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(recorded_at="2026-07-14T03:49:22")


def test_recorded_at_non_utc_offset_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(recorded_at="2026-07-14T03:49:22+08:00")
    with pytest.raises(ValueError):
        # A numeric offset masquerading behind a trailing 'Z' is still non-UTC.
        _mk_evidence(recorded_at="2026-07-14T03:49:22+08:00Z")


# ── mode / terminal / quality_verdict vocabularies ───────────────────
def test_mode_none_and_valid_accepted():
    assert _mk_evidence(mode=None).mode is None
    for mode in (
        "discovery", "broad", "grounding", "research", "academic",
        "platform", "recovery", "local",
    ):
        assert _mk_evidence(mode=mode).mode == mode


def test_invalid_mode_rejected():
    for mode in ("hermes", "made up", ""):
        with pytest.raises(ValueError):
            _mk_evidence(mode=mode)


def test_terminal_none_and_valid_accepted():
    assert _mk_evidence(terminal=None).terminal is None
    for terminal in (
        "routed", "explicit_provider", "recovery", "recovery_blocked", "all_engines_failed",
    ):
        assert _mk_evidence(terminal=terminal).terminal == terminal


def test_invalid_terminal_rejected():
    for terminal in ("hermes", "done", "unknown", ""):
        with pytest.raises(ValueError):
            _mk_evidence(terminal=terminal)


def test_quality_verdict_none_and_valid_accepted():
    assert _mk_evidence(quality_verdict=None).quality_verdict is None
    for verdict in ("complete", "insufficient", "failed"):
        assert _mk_evidence(quality_verdict=verdict).quality_verdict == verdict


def test_invalid_quality_verdict_rejected():
    for verdict in ("pass", "fail", "ok", ""):
        with pytest.raises(ValueError):
            _mk_evidence(quality_verdict=verdict)


# ── Bounded machine tokens: actual_provider + nested shadow ──────────
def test_actual_provider_none_and_token_accepted():
    assert _mk_evidence(actual_provider=None).actual_provider is None
    assert _mk_evidence(actual_provider="exa.web:v2-eu").actual_provider == "exa.web:v2-eu"


def test_actual_provider_prose_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(actual_provider="exa provider chosen because it is best")
    with pytest.raises(ValueError):
        _mk_evidence(actual_provider="has whitespace")


def test_actual_provider_over_128_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(actual_provider="a" * 129)


def _shadow(**overrides) -> ShadowComparison:
    values = dict(
        code="E0",
        safe=True,
        legacy_provider_ids=("exa", "brave"),
        descriptor_provider_ids=("exa", "brave"),
    )
    values.update(overrides)
    return ShadowComparison(**values)


def test_shadow_valid_codes_and_reason_tokens_accepted():
    for code in ("E0", "E1", "E2", "E3", "U1", "U2", "U3", "U4"):
        ev = _mk_evidence(
            shadow_comparison=_shadow(
                code=code,
                reasons=("exa:descriptor_blocked", "order_only_mismatch"),
                context_snapshot_version="ctx-2026.07",
                config_fingerprint="fp:abc123",
            )
        )
        assert ev.shadow_comparison.code == code


def test_shadow_invalid_code_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(shadow_comparison=_shadow(code="E9"))
    with pytest.raises(ValueError):
        _mk_evidence(shadow_comparison=_shadow(code="ok"))


def test_shadow_safe_must_be_bool():
    with pytest.raises(ValueError):
        _mk_evidence(shadow_comparison=_shadow(safe="secret prose"))


@pytest.mark.parametrize(
    "field_name",
    (
        "legacy_provider_ids",
        "descriptor_provider_ids",
        "omitted_provider_ids",
        "added_provider_ids",
        "reasons",
    ),
)
def test_shadow_collections_must_be_immutable_tuples(field_name):
    with pytest.raises(ValueError):
        _mk_evidence(shadow_comparison=_shadow(**{field_name: ["exa"]}))


def test_shadow_rejects_tuple_subclasses():
    class MutableTuple(tuple):
        pass

    with pytest.raises(ValueError):
        _mk_evidence(
            shadow_comparison=_shadow(legacy_provider_ids=MutableTuple(("exa",)))
        )
    with pytest.raises(ValueError):
        _mk_evidence(shadow_comparison=_shadow(reasons=MutableTuple(("reason_code",))))


def test_shadow_provider_prose_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(shadow_comparison=_shadow(legacy_provider_ids=("exa search engine",)))
    with pytest.raises(ValueError):
        _mk_evidence(shadow_comparison=_shadow(descriptor_provider_ids=("a" * 129,)))


def test_shadow_reason_prose_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(shadow_comparison=_shadow(reasons=("omitted because it was slow",)))


def test_shadow_snapshot_and_fingerprint_prose_rejected():
    with pytest.raises(ValueError):
        _mk_evidence(shadow_comparison=_shadow(context_snapshot_version="a real prose sentence"))
    with pytest.raises(ValueError):
        _mk_evidence(shadow_comparison=_shadow(config_fingerprint="fingerprint with spaces"))


def test_shadow_empty_snapshot_and_fingerprint_allowed():
    # Defaults are empty strings; those must remain acceptable.
    ev = _mk_evidence(shadow_comparison=_shadow())
    assert ev.shadow_comparison.context_snapshot_version == ""


# ── _append zero-progress guard ──────────────────────────────────────
def test_append_zero_progress_raises_but_record_swallows(tmp_path, monkeypatch):
    import wrr.runtime.decision_evidence as de

    path = tmp_path / "evi.jsonl"
    sink = JsonlDecisionEvidenceSink(path)

    monkeypatch.setattr(de.os, "write", lambda fd, data: 0)

    # Internal append must detect no forward progress and raise instead of hang.
    with pytest.raises(Exception):
        sink._append(b"payload\n")

    # Public record() still swallows the failure and returns without hanging.
    sink.record(_mk_evidence())


# ── Path resolution ──────────────────────────────────────────────────
def test_default_path_uses_last_data_root(tmp_path):
    runtime = SimpleNamespace(data_roots=[tmp_path / "a", tmp_path / "b"])
    path = decision_evidence_path(runtime=runtime, env={})
    assert path == tmp_path / "b" / "decision-evidence.jsonl"


def test_override_path_env(tmp_path):
    target = tmp_path / "custom" / "evi.jsonl"
    path = decision_evidence_path(env={"WRR_DECISION_EVIDENCE_PATH": str(target)})
    assert path == target


# ── Noop sink ────────────────────────────────────────────────────────
def test_noop_sink_writes_nothing(tmp_path):
    sink = NoopDecisionEvidenceSink()
    assert isinstance(sink, DecisionEvidenceSink)
    sink.record(_mk_evidence())
    # No file, no error.
    assert list(tmp_path.iterdir()) == []


# ── JSONL append ─────────────────────────────────────────────────────
def test_jsonl_single_line_append(tmp_path):
    path = tmp_path / "evi.jsonl"
    sink = JsonlDecisionEvidenceSink(path)
    assert isinstance(sink, DecisionEvidenceSink)
    sink.record(_mk_evidence(result_count=3))
    sink.record(_mk_evidence(result_count=5))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert set(first) == EVIDENCE_KEYS
    assert first["result_count"] == 3
    assert json.loads(lines[1])["result_count"] == 5


def test_jsonl_rejects_serializer_smuggling(tmp_path):
    class Duck:
        def to_dict(self):
            return {"query": "private request"}

    class EvilEvidence(DecisionEvidence):
        def to_dict(self):
            return {"secret": "credential"}

    path = tmp_path / "evi.jsonl"
    sink = JsonlDecisionEvidenceSink(path)
    sink.record(cast(DecisionEvidence, Duck()))
    base = _mk_evidence()
    sink.record(EvilEvidence(**vars(base)))
    assert not path.exists()


def test_jsonl_creates_parent_dirs(tmp_path):
    path = tmp_path / "deep" / "nested" / "evi.jsonl"
    sink = JsonlDecisionEvidenceSink(path)
    sink.record(_mk_evidence())
    assert path.exists()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_jsonl_file_mode_is_0600(tmp_path):
    path = tmp_path / "evi.jsonl"
    JsonlDecisionEvidenceSink(path).record(_mk_evidence())
    assert (path.stat().st_mode & 0o777) == 0o600


def test_jsonl_write_failure_is_swallowed(tmp_path):
    # Parent is a regular file -> mkdir/open must fail, but never raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    path = blocker / "sub" / "evi.jsonl"
    sink = JsonlDecisionEvidenceSink(path)
    sink.record(_mk_evidence())  # must not raise
    assert not path.exists()


# ── Concurrency: complete valid lines only ───────────────────────────
def _assert_all_lines_valid(path: Path, expected: int) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == expected
    for line in lines:
        record = json.loads(line)  # raises if any line is torn
        assert set(record) >= EVIDENCE_KEYS
        assert record["outcome"] in ("success", "empty", "error")


def test_threaded_writers_produce_complete_lines(tmp_path):
    path = tmp_path / "evi.jsonl"
    sink = JsonlDecisionEvidenceSink(path)
    workers, per_worker = 8, 25

    def run():
        for _ in range(per_worker):
            sink.record(_mk_evidence())

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    _assert_all_lines_valid(path, workers * per_worker)


def _mp_write_worker(args):
    path_str, count = args
    from wrr.runtime.decision_evidence import JsonlDecisionEvidenceSink as Sink
    from wrr.schemas import DecisionEvidence as Ev
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz

    sink = Sink(path_str)
    for _ in range(count):
        sink.record(
            Ev(
                request_key=str(_uuid.uuid4()),
                recorded_at=_dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                outcome="success",
                result_count=1,
                route_elapsed_ms=1.0,
            )
        )
    return count


def test_multiprocessing_writers_produce_complete_lines(tmp_path):
    path = tmp_path / "evi.jsonl"
    procs, per_proc = 4, 50
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(procs) as pool:
        pool.map(_mp_write_worker, [(str(path), per_proc)] * procs)

    _assert_all_lines_valid(path, procs * per_proc)


# ── Source guard ─────────────────────────────────────────────────────
def test_source_forbids_sensitive_fields_and_fsync():
    source = (
        Path(__file__).parents[2]
        .joinpath("wrr", "runtime", "decision_evidence.py")
        .read_text(encoding="utf-8")
    )
    for forbidden in ("query", "hash", "snippet", "body", "secret", "fsync"):
        assert forbidden not in source, f"forbidden token leaked: {forbidden}"
