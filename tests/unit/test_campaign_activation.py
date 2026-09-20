"""D7-S4 production campaign activation contracts."""

import threading

import pytest
from wrr.evidence_v2 import project_decision_evidence_v2
from wrr.runtime.campaign_activation import (
    CampaignActivationConfig,
    CampaignController,
    parse_campaign_config,
)
from wrr.runtime.campaign_ledger import CampaignDeclaration, CampaignLedger
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


def _v2(request_key: str):
    provider = CachedDecisionContextProvider(_context)
    provider.refresh()
    base = DecisionEvidence(
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
    return project_decision_evidence_v2(
        base,
        provider.observe(),
        comparison_status="compared",
        execution_protection="not_required",
    )


def test_campaign_activation_is_opt_in_and_exact_bool():
    assert parse_campaign_config(None) is None
    assert parse_campaign_config({}) is None
    assert parse_campaign_config({"enabled": False}) is None
    assert parse_campaign_config({"enabled": "true"}) is None


def _config(tmp_path, capacity=3):
    # Controller logic is generic over capacity; build the frozen config directly
    # so small-N drain/close behavior is testable without minting 50 admissions.
    from pathlib import Path

    return CampaignActivationConfig(
        campaign_id="d7-s4-grounding-001",
        mode="grounding",
        capacity=capacity,
        policy_version="EV-D5-v1",
        build_manifest_id="a95fac3",
        ledger_path=Path(tmp_path / "campaign.sqlite"),
        context_ttl_sec=93600.0,
    )


def test_campaign_activation_freezes_one_strict_fixed_policy(tmp_path):
    ledger_path = tmp_path / "campaign.sqlite"
    config = parse_campaign_config({
        "enabled": True,
        "id": "d7-s4-grounding-001",
        "mode": "grounding",
        "capacity": 50,
        "policy_version": "EV-D5-v1",
        "build_manifest_id": "a95fac3",
        "ledger_path": str(ledger_path),
        "context_ttl_sec": 93600,
    })

    assert config.campaign_id == "d7-s4-grounding-001"
    assert config.mode == "grounding"
    assert config.capacity == 50
    assert config.policy_version == "EV-D5-v1"
    assert config.build_manifest_id == "a95fac3"
    assert config.ledger_path == ledger_path
    assert config.context_ttl_sec == 93600.0

    invalid = {
        "enabled": True,
        "id": "d7-s4-grounding-001",
        "mode": "grounding",
        "capacity": 50,
        "policy_version": "EV-D5-v1",
        "build_manifest_id": "a95fac3",
        "ledger_path": str(ledger_path),
        "context_ttl_sec": 93600,
    }
    for key, value in (
        ("capacity", True),
        ("capacity", 49),
        ("mode", "research"),
        ("policy_version", "future"),
        ("build_manifest_id", ""),
        ("context_ttl_sec", 86400),
    ):
        candidate = dict(invalid)
        candidate[key] = value
        with pytest.raises(ValueError):
            parse_campaign_config(candidate)

    with pytest.raises(ValueError):
        parse_campaign_config({**invalid, "unknown": "forbidden"})


def _controller(tmp_path, capacity=3):
    config = _config(tmp_path, capacity)
    cohort_id = "c0ffee00-0000-4000-8000-000000000001"
    ledger = CampaignLedger.open(
        config.ledger_path,
        campaign_id=config.campaign_id,
        capacity=config.capacity,
        declaration=CampaignDeclaration(
            policy_version=config.policy_version,
            requested_modes=(config.mode,),
            accepted_context_cohort_id=cohort_id,
            build_manifest_id=config.build_manifest_id,
        ),
    )
    return config, CampaignController(
        config,
        ledger,
        cohort_id,
        minimum_observation_seconds=0,
    ), ledger


def _rk(i):
    return f"11111111-1111-4111-8111-{i:012d}"


def test_controller_admits_exactly_capacity_then_closes(tmp_path):
    config, controller, ledger = _controller(tmp_path, capacity=3)

    a1 = controller.admit(_rk(1), campaign_id=config.campaign_id, mode="grounding", provider=None)
    assert a1 is not None
    assert controller.state == "active"
    a2 = controller.admit(_rk(2), campaign_id=config.campaign_id, mode="grounding", provider=None)
    assert a2 is not None
    a3 = controller.admit(_rk(3), campaign_id=config.campaign_id, mode="grounding", provider=None)
    assert a3 is not None
    assert controller.state == "draining"
    assert controller.admit(_rk(4), campaign_id=config.campaign_id, mode="grounding", provider=None) is None

    for admission, key in ((a1, _rk(1)), (a2, _rk(2)), (a3, _rk(3))):
        controller.finish(admission.token, _v2(key))
        controller.on_terminal()

    assert controller.close() == "clean"
    assert controller.state == "closed"
    assert controller.admit(_rk(5), campaign_id=config.campaign_id, mode="grounding", provider=None) is None

    reopened = CampaignLedger.open(config.ledger_path, campaign_id=config.campaign_id)
    facts = reopened.facts()
    assert facts.attempts_started == 3
    assert facts.status == "clean"
    assert facts.session_closed_cleanly is True
    assert facts.declaration.build_manifest_id == "a95fac3"


def test_controller_rejects_wrong_tag_without_touching_ledger(tmp_path):
    _config_obj, controller, ledger = _controller(tmp_path, capacity=3)

    assert controller.admit(_rk(1), campaign_id=None, mode=None, provider=None) is None
    assert controller.admit(_rk(2), campaign_id="wrong", mode="grounding", provider=None) is None
    assert controller.admit(_rk(3), campaign_id="d7-s4-grounding-001", mode="research", provider=None) is None
    assert controller.admit(_rk(4), campaign_id="d7-s4-grounding-001", mode="grounding", provider="exa") is None
    assert ledger.inspect().total == 0


def test_controller_disables_reopened_ledger_without_breaking_search(tmp_path):
    config = _config(tmp_path, capacity=3)
    cohort_id = "c0ffee00-0000-4000-8000-000000000001"
    CampaignLedger.open(
        config.ledger_path,
        campaign_id=config.campaign_id,
        capacity=config.capacity,
    )
    reopened = CampaignLedger.open(
        config.ledger_path,
        campaign_id=config.campaign_id,
        capacity=config.capacity,
    )

    controller = CampaignController(config, reopened, cohort_id)

    assert controller.state == "disabled"
    assert controller.admit(
        _rk(1), campaign_id=config.campaign_id, mode=config.mode, provider=None,
    ) is None
    assert reopened.inspect().total == 0


def test_controller_disables_campaign_when_admission_storage_fails(tmp_path, monkeypatch):
    _config_obj, controller, ledger = _controller(tmp_path, capacity=3)

    def fail_admission(_request_key):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(ledger, "begin", fail_admission)

    assert controller.admit(
        _rk(1), campaign_id="d7-s4-grounding-001", mode="grounding", provider=None,
    ) is None
    assert controller.state == "disabled"
    assert ledger.facts().fault_count == 1


def test_controller_disables_campaign_on_context_cohort_change(tmp_path):
    _config_obj, controller, ledger = _controller(tmp_path, capacity=3)

    assert controller.admit(
        _rk(1),
        campaign_id="d7-s4-grounding-001",
        mode="grounding",
        provider=None,
        context_status="available",
        context_cohort_id="different-cohort",
    ) is None
    assert controller.state == "disabled"
    assert ledger.facts().fault_count == 1


def test_controller_close_waits_for_last_finish(tmp_path):
    _config_obj, controller, _ledger = _controller(tmp_path, capacity=1)
    admission = controller.admit(
        _rk(1), campaign_id="d7-s4-grounding-001", mode="grounding", provider=None,
    )
    assert admission is not None
    assert controller.state == "draining"

    entered = threading.Event()
    release = threading.Event()
    close_status = {}

    def finisher():
        with controller.terminal_guard():
            controller.finish(admission.token, _v2(_rk(1)))
            entered.set()
            release.wait(timeout=5)
            controller.on_terminal()

    def closer():
        entered.wait(timeout=5)
        close_status["status"] = controller.close()

    t = threading.Thread(target=finisher)
    t.start()
    c = threading.Thread(target=closer)
    c.start()
    entered.wait(timeout=5)
    assert "status" not in close_status
    release.set()
    t.join(timeout=5)
    c.join(timeout=5)
    assert close_status["status"] == "clean"
    assert controller.state == "closed"


def test_controller_auto_closes_after_all_terminal(tmp_path):
    config, controller, ledger = _controller(tmp_path, capacity=2)

    a1 = controller.admit(_rk(1), campaign_id=config.campaign_id, mode="grounding", provider=None)
    a2 = controller.admit(_rk(2), campaign_id=config.campaign_id, mode="grounding", provider=None)
    assert a1 is not None and a2 is not None
    assert controller.state == "draining"

    controller.finish(a1.token, _v2(_rk(1)))
    controller.on_terminal()
    assert controller.state == "draining"

    controller.finish(a2.token, _v2(_rk(2)))
    controller.on_terminal()
    assert controller.state == "closed"

    reopened = CampaignLedger.open(config.ledger_path, campaign_id=config.campaign_id)
    assert reopened.facts().status == "clean"
    assert reopened.facts().session_closed_cleanly is True


def test_controller_waits_24h_before_clean_close(tmp_path):
    config = _config(tmp_path, capacity=1)
    cohort_id = "c0ffee00-0000-4000-8000-000000000001"
    ledger = CampaignLedger.open(
        config.ledger_path,
        campaign_id=config.campaign_id,
        capacity=config.capacity,
        declaration=CampaignDeclaration(
            policy_version=config.policy_version,
            requested_modes=(config.mode,),
            accepted_context_cohort_id=cohort_id,
            build_manifest_id=config.build_manifest_id,
        ),
    )
    now = [100.0]
    controller = CampaignController(
        config,
        ledger,
        cohort_id,
        clock=lambda: now[0],
        minimum_observation_seconds=86400,
    )
    admission = controller.admit(
        _rk(1),
        campaign_id=config.campaign_id,
        mode=config.mode,
        provider=None,
        context_status="available",
        context_cohort_id=cohort_id,
    )
    controller.finish(admission.token, _v2(_rk(1)))
    controller.on_terminal()

    assert controller.state == "draining"
    assert ledger.inspect().status == "open"

    now[0] += 86400
    controller.tick(context_status="available", context_cohort_id=cohort_id)
    assert controller.state == "closed"
    reopened = CampaignLedger.open(config.ledger_path, campaign_id=config.campaign_id)
    assert reopened.facts().status == "clean"


def test_controller_terminal_close_failure_never_breaks_user_path(tmp_path, monkeypatch):
    _config_obj, controller, ledger = _controller(tmp_path, capacity=1)
    admission = controller.admit(
        _rk(1), campaign_id="d7-s4-grounding-001", mode="grounding", provider=None,
    )
    controller.finish(admission.token, _v2(_rk(1)))

    def fail_close():
        raise OSError("close failed")

    monkeypatch.setattr(ledger, "close", fail_close)
    controller.on_terminal()

    assert controller.state == "disabled"


def test_controller_close_refuses_before_capacity_or_while_in_flight(tmp_path):
    config, controller, _ledger = _controller(tmp_path, capacity=2)
    a1 = controller.admit(
        _rk(1), campaign_id=config.campaign_id, mode=config.mode, provider=None,
    )
    controller.finish(a1.token, _v2(_rk(1)))

    assert controller.close() == "open"
    assert controller.state == "active"

    controller.on_terminal()
    a2 = controller.admit(
        _rk(2), campaign_id=config.campaign_id, mode=config.mode, provider=None,
    )
    assert controller.state == "draining"
    assert controller.close() == "open"

    controller.finish(a2.token, _v2(_rk(2)))
    controller.on_terminal()
    assert controller.state == "closed"
    reopened = CampaignLedger.open(config.ledger_path, campaign_id=config.campaign_id)
    assert reopened.facts().status == "clean"


def test_controller_rejects_first_admission_without_full_context_window(tmp_path):
    config = _config(tmp_path, capacity=1)
    cohort_id = "c0ffee00-0000-4000-8000-000000000001"
    ledger = CampaignLedger.open(
        config.ledger_path,
        campaign_id=config.campaign_id,
        capacity=config.capacity,
        declaration=CampaignDeclaration(
            policy_version=config.policy_version,
            requested_modes=(config.mode,),
            accepted_context_cohort_id=cohort_id,
            build_manifest_id=config.build_manifest_id,
        ),
    )
    controller = CampaignController(
        config,
        ledger,
        cohort_id,
        minimum_observation_seconds=86400,
    )

    assert controller.admit(
        _rk(1),
        campaign_id=config.campaign_id,
        mode=config.mode,
        provider=None,
        context_status="available",
        context_cohort_id=cohort_id,
        context_expires_at=186399.0,
        context_now=100000.0,
    ) is None
    assert controller.state == "disabled"
    assert ledger.facts().fault_count == 1
