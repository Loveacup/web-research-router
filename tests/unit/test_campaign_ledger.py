"""EV-D6 Slice 1 — SQLite campaign ledger contracts.

The ledger is an admission-control record bound to a single campaign and a
single owning process epoch. It commits monotonic seq/token on ``begin``,
atomically persists the exact DecisionEvidenceV2 whitelist on ``finish``, and
closes ``clean`` only when nothing is left pending, dropped, or faulted. A
reopened OPEN/dirty campaign is inspect/export only and can never be made
clean. Duplicate request_key/token and capacity exhaustion fail closed.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from wrr.evidence_v2 import project_decision_evidence_v2
from wrr.runtime.campaign_ledger import (
    CampaignClosed,
    CampaignDeclaration,
    CampaignFacts,
    CampaignLedger,
    CampaignMismatch,
    CampaignReopened,
    CapacityExhausted,
    DuplicateRequestKey,
    InvalidEvidence,
    RequestKeyMismatch,
    UnknownToken,
)
from wrr.runtime.decision_context_provider import CachedDecisionContextProvider
from wrr.schemas import DecisionContext, DecisionEvidence, ShadowComparison


def _context() -> DecisionContext:
    return DecisionContext(
        snapshot_version="ctx-secret",
        built_at=1.0,
        expires_at=2.0,
        runtime="standalone",
        profile="default",
        registry_source="test",
        routable_descriptor_ids=("exa",),
        bridged_provider_ids=("exa",),
        missing_provider_ids=(),
        adapter_errors=(),
        descriptor_reasons=(),
        descriptor_provider_aliases=(),
        config_fingerprint="config-secret",
    )


def _v1(*, request_key: str) -> DecisionEvidence:
    return DecisionEvidence(
        request_key=request_key,
        recorded_at="2026-08-19T00:00:00.000000Z",
        mode="grounding",
        terminal="routed",
        outcome="success",
        actual_provider="rrf:grounding",
        result_count=2,
        quality_verdict="complete",
        route_elapsed_ms=5.0,
        shadow_comparison=ShadowComparison(
            code="E0",
            safe=True,
            legacy_provider_ids=("exa",),
            descriptor_provider_ids=("exa",),
            context_snapshot_version="ctx-secret",
            config_fingerprint="config-secret",
        ),
    )


def _v2(request_key: str = "00000000-0000-4000-8000-000000000002"):
    provider = CachedDecisionContextProvider(_context)
    provider.refresh()
    return project_decision_evidence_v2(
        _v1(request_key=request_key),
        provider.observe(),
        comparison_status="compared",
        execution_protection="not_required",
    )


CID = "campaign-alpha"


def _open(tmp_path, **kw) -> CampaignLedger:
    return CampaignLedger.open(tmp_path / "ledger.db", campaign_id=CID, **kw)


def _storage_snapshot(path: Path):
    with sqlite3.connect(path) as conn:
        meta = conn.execute(
            "SELECT campaign_id, capacity, declaration, status, fault_count FROM meta WHERE id = 1"
        ).fetchone()
        admissions = conn.execute("SELECT COUNT(*) FROM admissions").fetchone()[0]
    return meta, admissions


# ── lifecycle basics ────────────────────────────────────────────────────


def test_fresh_open_creates_open_writable_campaign(tmp_path):
    ledger = _open(tmp_path)
    try:
        assert ledger.reopened is False
        assert ledger.closed is False
        snap = ledger.inspect()
        assert snap.campaign_id == CID
        assert snap.status == "open"
        assert snap.epoch  # non-empty owning-epoch marker
        assert (snap.pending, snap.finished, snap.dropped, snap.faults) == (0, 0, 0, 0)
    finally:
        ledger.close()


def test_pragmas_are_wal_and_synchronous_full(tmp_path):
    ledger = _open(tmp_path)
    try:
        pragmas = ledger.sqlite_pragmas()
        assert pragmas["journal_mode"].lower() == "wal"
        assert str(pragmas["synchronous"]) in ("2", "FULL")
    finally:
        ledger.close()
    # journal_mode is persisted in the file header.
    conn = sqlite3.connect(tmp_path / "ledger.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_begin_commits_monotonic_seq_and_unique_tokens(tmp_path):
    ledger = _open(tmp_path)
    try:
        a = ledger.begin("11111111-1111-4111-8111-111111111111")
        b = ledger.begin("22222222-2222-4222-8222-222222222222")
        assert a.seq == 1
        assert b.seq == 2
        assert a.token != b.token
        assert ledger.inspect().pending == 2
    finally:
        ledger.close()


# ── finish / evidence contract ──────────────────────────────────────────


def test_finish_persists_exact_v2_whitelist_atomically(tmp_path):
    ledger = _open(tmp_path)
    try:
        admission = ledger.begin("33333333-3333-4333-8333-333333333333")
        evidence = _v2("33333333-3333-4333-8333-333333333333")
        ledger.finish(admission.token, evidence)

        snap = ledger.inspect()
        assert (snap.pending, snap.finished) == (0, 1)

        exported = ledger.export()
        assert len(exported) == 1
        assert exported[0]["evidence"] == evidence.to_dict()
        assert exported[0]["seq"] == admission.seq
    finally:
        ledger.close()


def test_export_leaks_no_context_or_config_secrets(tmp_path):
    ledger = _open(tmp_path)
    try:
        admission = ledger.begin("44444444-4444-4444-8444-444444444444")
        ledger.finish(admission.token, _v2("44444444-4444-4444-8444-444444444444"))
        import json

        blob = json.dumps(ledger.export())
        assert "ctx-secret" not in blob
        assert "config-secret" not in blob
    finally:
        ledger.close()


def test_finish_requires_exact_v2_and_leaves_pending(tmp_path):
    ledger = _open(tmp_path)
    try:
        admission = ledger.begin("55555555-5555-4555-8555-555555555555")
        with pytest.raises(InvalidEvidence):
            ledger.finish(admission.token, _v1(request_key="55555555-5555-4555-8555-555555555555"))
        with pytest.raises(InvalidEvidence):
            ledger.finish(admission.token, {"schema_version": 2})
        assert ledger.inspect().pending == 1
        # The still-pending admission may be finished for real afterwards.
        ledger.finish(admission.token, _v2("55555555-5555-4555-8555-555555555555"))
        assert ledger.inspect().finished == 1
    finally:
        ledger.close()


# ── fail-closed anomalies ───────────────────────────────────────────────


def test_duplicate_request_key_fails_closed_and_poisons_clean(tmp_path):
    ledger = _open(tmp_path)
    try:
        key = "66666666-6666-4666-8666-666666666666"
        first = ledger.begin(key)
        with pytest.raises(DuplicateRequestKey):
            ledger.begin(key)
        ledger.finish(first.token, _v2(key))
        assert ledger.inspect().faults >= 1
        assert ledger.close() == "dirty"
    finally:
        if not ledger.closed:
            ledger.close()


def test_double_finish_fails_closed_and_poisons(tmp_path):
    ledger = _open(tmp_path)
    try:
        admission = ledger.begin("77777777-7777-4777-8777-777777777777")
        ledger.finish(admission.token, _v2("77777777-7777-4777-8777-777777777777"))
        with pytest.raises(UnknownToken):
            # A dead token is rejected before any request_key comparison.
            ledger.finish(admission.token, _v2("77777777-7777-4777-8777-777777777777"))
        assert ledger.inspect().faults >= 1
        assert ledger.close() == "dirty"
    finally:
        if not ledger.closed:
            ledger.close()


def test_finish_unknown_token_fails_closed(tmp_path):
    ledger = _open(tmp_path)
    try:
        with pytest.raises(UnknownToken):
            ledger.finish("no-such-token", _v2())
    finally:
        ledger.close()


def test_finish_request_key_mismatch_fails_closed_and_poisons(tmp_path):
    ledger = _open(tmp_path)
    try:
        admission = ledger.begin("12121212-1212-4121-8121-121212121212")
        # Evidence for a *different* request than the one this token admitted:
        # persisting it would mislabel the record, so finish must refuse it.
        mismatched = _v2("00000000-0000-4000-8000-000000000002")
        assert mismatched.request_key != admission.request_key
        with pytest.raises(RequestKeyMismatch):
            ledger.finish(admission.token, mismatched)
        # The admission stays pending (nothing was written) and the mismatch is
        # an integrity fault that poisons the campaign.
        snap = ledger.inspect()
        assert (snap.finished, snap.pending) == (0, 1)
        assert snap.faults >= 1
        # Even finishing the token with its correct evidence cannot un-poison it.
        ledger.finish(admission.token, _v2("12121212-1212-4121-8121-121212121212"))
        assert ledger.inspect().finished == 1
        assert ledger.close() == "dirty"
    finally:
        if not ledger.closed:
            ledger.close()


def test_drop_marks_dirty(tmp_path):
    ledger = _open(tmp_path)
    try:
        admission = ledger.begin("88888888-8888-4888-8888-888888888888")
        ledger.drop(admission.token)
        snap = ledger.inspect()
        assert snap.dropped == 1
        assert snap.pending == 0
        assert ledger.close() == "dirty"
    finally:
        if not ledger.closed:
            ledger.close()


def test_capacity_exhausted_fails_closed_and_poisons(tmp_path):
    ledger = _open(tmp_path, capacity=1)
    try:
        admission = ledger.begin("99999999-9999-4999-8999-999999999999")
        with pytest.raises(CapacityExhausted):
            ledger.begin("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        ledger.finish(admission.token, _v2("99999999-9999-4999-8999-999999999999"))
        # A refused admission means the campaign is no longer a complete record
        # of what was asked of it: capacity exhaustion poisons and can never
        # close clean, even once every admitted request is finished.
        assert ledger.inspect().faults >= 1
        assert ledger.close() == "dirty"
    finally:
        if not ledger.closed:
            ledger.close()


def test_explicit_runtime_fault_is_durable_and_forces_dirty_close(tmp_path):
    ledger = _open(tmp_path)
    ledger.record_fault("identity_mint_failed")

    assert ledger.inspect().faults == 1
    assert ledger.facts().fault_count == 1
    assert ledger.close() == "dirty"


def test_unpersisted_runtime_fault_still_prevents_clean_close(tmp_path, monkeypatch):
    ledger = _open(tmp_path)

    def fail_persistence(_reason):
        raise sqlite3.OperationalError("injected fault persistence failure")

    monkeypatch.setattr(ledger, "_record_fault", fail_persistence)
    with pytest.raises(sqlite3.OperationalError):
        ledger.record_fault("admission_failed")

    assert ledger.close() == "dirty"


def test_shared_ledger_serializes_cross_thread_admissions(tmp_path):
    ledger = _open(tmp_path, capacity=2)
    keys = (
        "31313131-3131-4131-8131-313131313131",
        "32323232-3232-4232-8232-323232323232",
    )

    def admit_and_finish(key):
        admission = ledger.begin(key)
        ledger.finish(admission.token, _v2(key))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(admit_and_finish, keys))
        snapshot = ledger.inspect()
        assert (snapshot.pending, snapshot.finished, snapshot.faults) == (0, 2, 0)
        assert ledger.close() == "clean"
    finally:
        if not ledger.closed:
            ledger.close()


def test_begin_or_fault_blocks_close_until_failure_is_poisoned(
    tmp_path, monkeypatch,
):
    ledger = _open(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    close_result = []

    def fail_begin(_request_key):
        entered.set()
        assert release.wait(5)
        raise sqlite3.OperationalError("injected admission failure")

    monkeypatch.setattr(ledger, "begin", fail_begin)

    def attempt():
        with pytest.raises(sqlite3.OperationalError):
            ledger.begin_or_fault("41414141-4141-4141-8141-414141414141")

    worker = threading.Thread(target=attempt)
    closer = threading.Thread(target=lambda: close_result.append(ledger.close()))
    worker.start()
    assert entered.wait(5)
    closer.start()
    assert closer.is_alive()  # close cannot certify while admission is unresolved
    release.set()
    worker.join(5)
    closer.join(5)

    assert close_result == ["dirty"]
    reopened = _open(tmp_path)
    try:
        reopened_facts = reopened.facts()
        assert reopened_facts.fault_count == 1
        assert reopened_facts.session_closed_cleanly is False
        assert reopened.close() == "dirty"
    finally:
        if not reopened.closed:
            reopened.close()


def test_terminal_sink_failure_blocks_close_then_forces_dirty(tmp_path):
    plugin_path = Path(__file__).resolve().parents[2] / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "wrr_plugin_terminal_guard_test",
        plugin_path,
        submodule_search_locations=[str(plugin_path.parent)],
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    ledger = _open(tmp_path)
    key = "51515151-5151-4151-8151-515151515151"
    admission = ledger.begin(key)
    downstream_entered = threading.Event()
    release_downstream = threading.Event()
    close_result: list[str] = []
    writer_errors: list[Exception] = []

    class FailingDownstream:
        def record(self, evidence):
            downstream_entered.set()
            assert release_downstream.wait(5)
            return False

    sink = plugin._CampaignEvidenceSink(ledger, admission, FailingDownstream())

    def write_terminal_evidence():
        try:
            sink.record(_v2(key))
        except Exception as exc:  # expected: downstream persistence reported false
            writer_errors.append(exc)

    writer = threading.Thread(target=write_terminal_evidence)
    writer.start()
    assert downstream_entered.wait(5)

    closer = threading.Thread(target=lambda: close_result.append(ledger.close()))
    closer.start()
    closer.join(0.1)
    assert closer.is_alive(), "close must wait for terminal evidence persistence"

    release_downstream.set()
    writer.join(5)
    closer.join(5)

    assert len(writer_errors) == 1
    assert close_result == ["dirty"]
    reopened = _open(tmp_path)
    try:
        facts = reopened.facts()
        assert facts.fault_count == 1
        assert facts.session_closed_cleanly is False
    finally:
        reopened.close()


# ── clean vs dirty close ────────────────────────────────────────────────


def test_close_clean_when_all_finished(tmp_path):
    ledger = _open(tmp_path)
    admission = ledger.begin("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    ledger.finish(admission.token, _v2("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))
    assert ledger.close() == "clean"
    # Idempotent.
    assert ledger.close() == "clean"


def test_close_dirty_when_pending_remains(tmp_path):
    ledger = _open(tmp_path)
    ledger.begin("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert ledger.close() == "dirty"


def test_operations_after_close_fail(tmp_path):
    ledger = _open(tmp_path)
    ledger.close()
    with pytest.raises(CampaignClosed):
        ledger.begin("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


# ── reopen semantics ────────────────────────────────────────────────────


def test_begin_or_fault_preserves_clean_reopened_ledger_read_only(tmp_path):
    owner = _open(tmp_path)
    assert owner.close() == "clean"

    reopened = _open(tmp_path)
    before = reopened.facts()
    try:
        with pytest.raises(CampaignReopened):
            reopened.begin_or_fault("51515151-5151-4151-8151-515151515151")
        after = reopened.facts()
        assert after == before
        assert reopened.close() == "clean"
    finally:
        if not reopened.closed:
            reopened.close()


def test_reopen_open_campaign_is_inspect_export_only(tmp_path):
    primary = _open(tmp_path)
    admission = primary.begin("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    primary.finish(admission.token, _v2("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"))
    # A second admission is left pending, and the owner never closes cleanly.
    primary.begin("ffffffff-ffff-4fff-8fff-ffffffffffff")

    reopened = _open(tmp_path)
    try:
        assert reopened.reopened is True
        # Read paths work and see the prior process's records.
        assert reopened.inspect().finished == 1
        assert len(reopened.export()) == 1
        # Admission is forbidden.
        with pytest.raises(CampaignReopened):
            reopened.begin("00000000-0000-4000-8000-0000000000aa")
        with pytest.raises(CampaignReopened):
            reopened.finish(admission.token, _v2())
        with pytest.raises(CampaignReopened):
            reopened.drop(admission.token)
        # It can never be turned clean.
        assert reopened.close() != "clean"
    finally:
        if not reopened.closed:
            reopened.close()
        primary.close()


def test_reopen_campaign_id_mismatch_fails_closed(tmp_path):
    primary = _open(tmp_path)
    path = tmp_path / "ledger.db"
    before = _storage_snapshot(path)
    try:
        with pytest.raises(CampaignMismatch) as raised:
            CampaignLedger.open(path, campaign_id="other-campaign")
        error = raised.value
        assert error.reason == "campaign_id_mismatch"
        assert error.diagnostic["stored_campaign_id"] == CID
        assert error.diagnostic["requested_campaign_id"] == "other-campaign"
        assert "Preserve the existing ledger" in error.diagnostic["remediation"]
        assert _storage_snapshot(path) == before
    finally:
        primary.close()


# ── durable campaign facts (EV-D6 S2) ───────────────────────────────────


def test_facts_is_frozen(tmp_path):
    ledger = _open(tmp_path)
    try:
        facts = ledger.facts()
        assert isinstance(facts, CampaignFacts)
        with pytest.raises((AttributeError, TypeError)):
            facts.campaign_id = "mutated"  # type: ignore[misc]
    finally:
        ledger.close()


def test_facts_reports_durable_counters_for_mixed_run(tmp_path):
    ledger = _open(tmp_path)
    keys = [
        "10000000-0000-4000-8000-000000000001",
        "10000000-0000-4000-8000-000000000002",
        "10000000-0000-4000-8000-000000000003",
    ]
    a = ledger.begin(keys[0])
    b = ledger.begin(keys[1])
    c = ledger.begin(keys[2])
    ledger.finish(a.token, _v2(keys[0]))
    ledger.finish(c.token, _v2(keys[2]))
    ledger.drop(b.token)
    facts = ledger.facts()
    assert facts.campaign_id == CID
    assert facts.epoch  # non-empty owning-epoch marker
    # A drop leaves nothing faulted; the live status stays "open" and only the
    # close() verdict turns dirty (see the close assertion below).
    assert facts.status == "open"
    assert facts.capacity is None
    assert facts.start_sequence == 1
    assert facts.end_sequence == 3
    assert facts.attempts_started == 3
    assert facts.persisted_evidence_count == 2
    assert facts.dropped_count == 1
    assert facts.unresolved_count == 0
    assert facts.terminal_count == 3
    assert facts.duplicate_count == 0
    assert facts.sequence_gaps == 0
    assert facts.fault_count == 0
    assert facts.request_keys == (keys[0], keys[1], keys[2])
    assert facts.session_closed_cleanly is False
    assert ledger.close() == "dirty"  # the surviving drop keeps it non-clean


def test_facts_is_deterministic_across_repeated_reads(tmp_path):
    ledger = _open(tmp_path)
    try:
        key = "15000000-0000-4000-8000-000000000001"
        a = ledger.begin(key)
        ledger.finish(a.token, _v2(key))
        first = ledger.facts()
        second = ledger.facts()
        assert first == second
    finally:
        ledger.close()


def test_facts_fault_events_reconcile_duplicate_count(tmp_path):
    ledger = _open(tmp_path)
    try:
        key = "20000000-0000-4000-8000-000000000001"
        ledger.begin(key)
        with pytest.raises(DuplicateRequestKey):
            ledger.begin(key)
        with pytest.raises(DuplicateRequestKey):
            ledger.begin(key)
        facts = ledger.facts()
        # duplicate_count is the reconcilable subset of fault_count that the
        # fault_events table attributes to duplicate request_keys.
        assert facts.duplicate_count == 2
        assert facts.fault_count == 2
        assert facts.fault_count >= facts.duplicate_count
    finally:
        ledger.close()


def test_facts_session_closed_cleanly_only_after_clean_reopen(tmp_path):
    ledger = _open(tmp_path)
    key = "30000000-0000-4000-8000-000000000001"
    a = ledger.begin(key)
    # An OPEN campaign is not a cleanly-closed session.
    assert ledger.facts().session_closed_cleanly is False
    ledger.finish(a.token, _v2(key))
    assert ledger.close() == "clean"
    reopened = _open(tmp_path)
    try:
        facts = reopened.facts()
        assert facts.status == "clean"
        assert facts.session_closed_cleanly is True
    finally:
        reopened.close()


def test_facts_session_not_clean_when_dirty_close_reopened(tmp_path):
    ledger = _open(tmp_path)
    ledger.begin("40000000-0000-4000-8000-000000000001")  # left pending → dirty
    assert ledger.close() == "dirty"
    reopened = _open(tmp_path)
    try:
        assert reopened.facts().session_closed_cleanly is False
    finally:
        reopened.close()


def test_sequence_gap_is_detected_and_poisons_clean_close(tmp_path):
    ledger = _open(tmp_path)
    keys = [
        "50000000-0000-4000-8000-000000000001",
        "50000000-0000-4000-8000-000000000002",
        "50000000-0000-4000-8000-000000000003",
    ]
    admissions = [ledger.begin(k) for k in keys]
    for k, adm in zip(keys, admissions):
        ledger.finish(adm.token, _v2(k))
    # Simulate durable-record corruption: a committed admission goes missing.
    raw = sqlite3.connect(tmp_path / "ledger.db")
    try:
        raw.execute("DELETE FROM admissions WHERE seq = 2")
        raw.commit()
    finally:
        raw.close()
    facts = ledger.facts()
    assert facts.attempts_started == 2
    assert facts.start_sequence == 1
    assert facts.end_sequence == 3
    assert facts.sequence_gaps == 1
    # A gap means the ledger is no longer a complete record; it cannot close clean.
    assert ledger.close() == "dirty"


# ── bound pre-declared fixed campaign policy (EV-D6 S3) ──────────────────


def _decl(
    *,
    policy: str = "policy-v1",
    modes: tuple = ("grounding", "deep_research"),
    cohort: str = "cohort-A",
) -> CampaignDeclaration:
    return CampaignDeclaration(
        policy_version=policy,
        requested_modes=modes,
        accepted_context_cohort_id=cohort,
    )


def test_declaration_is_frozen():
    decl = _decl()
    with pytest.raises((AttributeError, TypeError)):
        decl.policy_version = "mutated"  # type: ignore[misc]


def test_declaration_is_canonical_and_value_equal():
    # Two declarations built from the same values compare equal; the modes are a
    # canonical tuple so ordering/type is fixed and hashable.
    assert _decl() == _decl()
    assert _decl().requested_modes == ("grounding", "deep_research")
    assert isinstance(_decl().requested_modes, tuple)


def test_declaration_rejects_empty_policy_and_cohort():
    with pytest.raises(ValueError):
        CampaignDeclaration(
            policy_version="",
            requested_modes=("grounding",),
            accepted_context_cohort_id="cohort-A",
        )
    with pytest.raises(ValueError):
        CampaignDeclaration(
            policy_version="policy-v1",
            requested_modes=("grounding",),
            accepted_context_cohort_id="",
        )


def test_declaration_rejects_empty_duplicate_and_non_string_modes():
    # Empty tuple of modes is not a valid declaration.
    with pytest.raises(ValueError):
        CampaignDeclaration(
            policy_version="policy-v1",
            requested_modes=(),
            accepted_context_cohort_id="cohort-A",
        )
    # Duplicate modes are rejected (canonical set of requested modes).
    with pytest.raises(ValueError):
        CampaignDeclaration(
            policy_version="policy-v1",
            requested_modes=("grounding", "grounding"),
            accepted_context_cohort_id="cohort-A",
        )
    # Empty-string and non-string entries are rejected; mode *validity* itself
    # (is it a real routing mode?) is deferred to the D5 evaluator, so we only
    # enforce the shape here and never re-enumerate the mode set.
    with pytest.raises(ValueError):
        CampaignDeclaration(
            policy_version="policy-v1",
            requested_modes=("grounding", ""),
            accepted_context_cohort_id="cohort-A",
        )
    with pytest.raises((ValueError, TypeError)):
        CampaignDeclaration(
            policy_version="policy-v1",
            requested_modes=("grounding", 5),  # type: ignore[arg-type]
            accepted_context_cohort_id="cohort-A",
        )


def test_open_with_declaration_requires_positive_capacity(tmp_path):
    # A declared campaign is a *fixed* campaign: it must bound its capacity.
    with pytest.raises(ValueError):
        _open(tmp_path, declaration=_decl())
    with pytest.raises(ValueError):
        _open(tmp_path, declaration=_decl(), capacity=0)


def test_fresh_declared_facts_carry_declaration_and_open_lifecycle(tmp_path):
    decl = _decl()
    ledger = _open(tmp_path, declaration=decl, capacity=4)
    try:
        facts = ledger.facts()
        assert facts.declaration == decl
        assert facts.capacity == 4
        assert facts.opened_at  # non-empty owning-open timestamp
        assert facts.closed_at is None
        assert facts.status == "open"
    finally:
        ledger.close()


def test_undeclared_campaign_has_no_declaration_but_has_open_lifecycle(tmp_path):
    # Existing (un-declared) campaigns stay compatible: declaration is None,
    # opened_at is present, closed_at is None while open.
    ledger = _open(tmp_path)
    try:
        facts = ledger.facts()
        assert facts.declaration is None
        assert facts.opened_at
        assert facts.closed_at is None
    finally:
        ledger.close()


def test_clean_close_then_reopen_preserves_declaration_and_opened_at(tmp_path):
    decl = _decl()
    ledger = _open(tmp_path, declaration=decl, capacity=2)
    key = "60000000-0000-4000-8000-000000000001"
    admission = ledger.begin(key)
    ledger.finish(admission.token, _v2(key))
    opened_at = ledger.facts().opened_at
    assert ledger.close() == "clean"

    reopened = _open(tmp_path)
    try:
        facts = reopened.facts()
        # The declaration and open timestamp are canonical across the reopen.
        assert facts.declaration == decl
        assert facts.opened_at == opened_at
        # The owner's close stamped a closed_at in the same transaction.
        assert facts.closed_at is not None
        assert facts.status == "clean"
    finally:
        reopened.close()


def test_reopen_with_matching_declaration_and_capacity_is_allowed(tmp_path):
    decl = _decl()
    ledger = _open(tmp_path, declaration=decl, capacity=3)
    ledger.close()
    reopened = _open(tmp_path, declaration=decl, capacity=3)
    try:
        assert reopened.reopened is True
        assert reopened.facts().declaration == decl
    finally:
        reopened.close()


def test_reopen_with_conflicting_declaration_fails_closed(tmp_path):
    ledger = _open(tmp_path, declaration=_decl(), capacity=2)
    ledger.close()
    path = tmp_path / "ledger.db"
    before = _storage_snapshot(path)
    with pytest.raises(CampaignMismatch) as raised:
        _open(tmp_path, declaration=_decl(policy="policy-v2"), capacity=2)
    error = raised.value
    assert error.reason == "declaration_mismatch"
    assert error.diagnostic["stored_declaration"]["policy_version"] == "policy-v1"
    assert error.diagnostic["requested_declaration"]["policy_version"] == "policy-v2"
    assert "changed semantic context or policy" in error.diagnostic["remediation"]
    assert _storage_snapshot(path) == before


def test_reopen_with_conflicting_capacity_fails_closed(tmp_path):
    ledger = _open(tmp_path, declaration=_decl(), capacity=2)
    ledger.close()
    path = tmp_path / "ledger.db"
    before = _storage_snapshot(path)
    with pytest.raises(CampaignMismatch) as raised:
        _open(tmp_path, declaration=_decl(), capacity=5)
    error = raised.value
    assert error.reason == "capacity_mismatch"
    assert (error.diagnostic["stored_capacity"], error.diagnostic["requested_capacity"]) == (2, 5)
    assert "Preserve the existing ledger" in error.diagnostic["remediation"]
    assert _storage_snapshot(path) == before


def test_reopen_with_conflicting_capacity_only_fails_closed(tmp_path):
    # Even without a declaration, explicitly reopening with a different capacity
    # than the one the campaign was created with fails closed.
    ledger = _open(tmp_path, capacity=2)
    ledger.close()
    with pytest.raises(CampaignMismatch):
        _open(tmp_path, capacity=5)


def test_dirty_close_stamps_closed_at(tmp_path):
    ledger = _open(tmp_path, declaration=_decl(), capacity=2)
    ledger.begin("70000000-0000-4000-8000-000000000001")  # left pending → dirty
    assert ledger.close() == "dirty"
    reopened = _open(tmp_path)
    try:
        facts = reopened.facts()
        assert facts.status == "dirty"
        assert facts.closed_at is not None
    finally:
        reopened.close()
