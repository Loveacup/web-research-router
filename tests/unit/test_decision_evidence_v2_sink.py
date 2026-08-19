"""Decision Evidence v1/v2 sink-union contracts (V2-3b)."""
from __future__ import annotations

import json

from wrr.evidence_v2 import project_decision_evidence_v2
from wrr.runtime.decision_context_provider import CachedDecisionContextProvider
from wrr.runtime.decision_evidence import JsonlDecisionEvidenceSink, NoopDecisionEvidenceSink
from wrr.schemas import DecisionContext, DecisionEvidence, DecisionEvidenceV2, ShadowComparison


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


def _v1(*, request_key="00000000-0000-4000-8000-000000000001") -> DecisionEvidence:
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


def _v2():
    provider = CachedDecisionContextProvider(_context)
    provider.refresh()
    return project_decision_evidence_v2(
        _v1(request_key="00000000-0000-4000-8000-000000000002"),
        provider.observe(),
        comparison_status="compared",
        execution_protection="not_required",
    )


def test_jsonl_sink_appends_v1_and_v2_without_changing_v1_wire(tmp_path):
    path = tmp_path / "evidence.jsonl"
    sink = JsonlDecisionEvidenceSink(path)

    sink.record(_v1())
    sink.record(_v2())

    records = [json.loads(line) for line in path.read_text().splitlines()]

    assert [record["schema_version"] for record in records] == [1, 2]
    assert records[0]["actual_provider"] == "rrf:grounding"
    assert records[0]["shadow_comparison"]["legacy_provider_ids"] == ["exa"]
    assert "actual_provider" not in records[1]
    assert records[1]["shadow_comparison"] == {
        "code": "E0",
        "safe": True,
        "legacy_provider_count": 1,
        "descriptor_provider_count": 1,
        "omitted_provider_count": 0,
        "added_provider_count": 0,
        "reasons_complete": True,
    }
    rendered_v2 = json.dumps(records[1], sort_keys=True)
    assert "ctx-secret" not in rendered_v2
    assert "config-secret" not in rendered_v2


def test_v1_sink_payload_remains_exactly_to_dict_compatible(tmp_path):
    path = tmp_path / "evidence.jsonl"
    evidence = _v1()

    JsonlDecisionEvidenceSink(path).record(evidence)

    assert json.loads(path.read_text()) == evidence.to_dict()


def test_v2_sink_rejects_post_projection_tampering(tmp_path):
    path = tmp_path / "evidence.jsonl"
    evidence = _v2()
    object.__setattr__(evidence, "context_cohort_id", "not-a-uuid")

    JsonlDecisionEvidenceSink(path).record(evidence)

    assert not path.exists()


def test_v2_sink_rejects_overridden_outer_cross_field_validator(tmp_path):
    path = tmp_path / "evidence.jsonl"
    evidence = _v2()
    object.__setattr__(evidence, "comparison_status", "comparison_failed")
    object.__setattr__(evidence, "_validate_cross_fields", lambda: None)

    JsonlDecisionEvidenceSink(path).record(evidence)

    assert not path.exists()


def test_v2_sink_ignores_nested_instance_serializer_override(tmp_path):
    path = tmp_path / "evidence.jsonl"
    evidence = _v2()
    shadow = evidence.shadow_comparison
    assert shadow is not None
    object.__setattr__(shadow, "to_dict", lambda: {"secret": "credential"})

    JsonlDecisionEvidenceSink(path).record(evidence)
    payload = json.loads(path.read_text())

    assert payload["schema_version"] == 2
    assert "secret" not in json.dumps(payload)


def test_v2_sink_rejects_overridden_nested_semantic_validator(tmp_path):
    path = tmp_path / "evidence.jsonl"
    evidence = _v2()
    shadow = evidence.shadow_comparison
    assert shadow is not None
    object.__setattr__(shadow, "added_provider_count", 1)
    object.__setattr__(shadow, "_validate_semantics", lambda: None)

    JsonlDecisionEvidenceSink(path).record(evidence)

    assert not path.exists()


def test_v2_sink_rejects_exact_type_subclass_smuggling(tmp_path):
    class EvilV2(DecisionEvidenceV2):
        def to_dict(self):
            return {"secret": "credential"}

    legitimate = _v2()
    evil = EvilV2(**vars(legitimate))
    path = tmp_path / "evidence.jsonl"

    JsonlDecisionEvidenceSink(path).record(evil)

    assert not path.exists()


def test_noop_sink_accepts_v2_without_side_effect(tmp_path):
    NoopDecisionEvidenceSink().record(_v2())

    assert list(tmp_path.iterdir()) == []
