"""P1-T4 manifest routing schema completeness tests."""

from pathlib import Path

from wrr import config
from wrr.engines.loader import (
    EngineDiscovery,
    discover_engine_plugins,
    merge_routing_config,
    parse_engine_manifest,
    trigger_matches,
)
from wrr.engines.registry import EngineRegistry
from wrr.runtime.detect import detect_runtime
from wrr.runtime.env import load_env


def _builtin_by_id():
    return {
        item.manifest.id: item.manifest
        for item in discover_engine_plugins()
        if item.valid and item.manifest is not None
    }


def _manifest(engine_id, *, actions=None, domains=None, adapter=None):
    manifest, errors = parse_engine_manifest(
        {
            "schema_version": 1,
            "id": engine_id,
            "name": engine_id.title(),
            "kind": "web_api",
            "adapter": adapter or f"missing.{engine_id}:{engine_id.title()}Engine",
            "capabilities": {
                "actions": actions or ["search"],
                "domains": domains or ["web"],
            },
            "routing": {
                "modes": ["auto"],
                "weight": 1.0,
                "triggers": [
                    {
                        "name": "code",
                        "match": {"keywords": ["site:github.com"]},
                    }
                ],
            },
            "requirements": {"env": [], "binaries": [], "repos": []},
            "health": {"checks": []},
            "requires_capabilities": {},
        }
    )
    assert not errors
    assert manifest is not None
    return manifest


def test_builtin_manifest_schema_has_actions_domains_triggers_and_fusion_defaults():
    manifests = _builtin_by_id()

    assert {"github", "community", "academic", "skill"} <= set(manifests)
    for manifest in manifests.values():
        assert manifest.capabilities["actions"]
        assert manifest.capabilities["domains"]
        assert "triggers" in manifest.routing
        assert manifest.fusion["rrf_k"] == config.RRF_K
        assert manifest.fusion == config.FUSION_DEFAULTS


def test_manifest_trigger_rules_reproduce_current_trigger_behaviour():
    manifests = _builtin_by_id()
    samples = {
        "github": [
            "foo site:github.com",
            "SITE:GITHUB.COM project",
            "plain query",
        ],
        "community": [
            "foo site:reddit.com",
            "Windows Terminal 操作指南和快捷键怎么用",
            "python 3.14 release date",
        ],
        "academic": [
            "graph neural network survey paper",
            "arxiv retrieval methodology",
            "plain query",
        ],
        "skill": [
            "有没有 data viz 的 skill",
            "skill 推荐",
            "plain query",
        ],
    }
    expected = {
        "github": config.github_triggered,
        "community": config.community_triggered,
        "academic": config.academic_triggered,
        "skill": config.skill_triggered,
    }

    for engine_id, queries in samples.items():
        for query in queries:
            assert trigger_matches(manifests[engine_id], query) is expected[engine_id](query)


def test_manifest_parser_rejects_bad_domains_and_bad_trigger_match():
    bad_domains = {
        "schema_version": 1,
        "id": "broken",
        "name": "Broken",
        "kind": "web_api",
        "adapter": "wrr.engines.broken:BrokenEngine",
        "capabilities": {"actions": ["search"], "domains": "web"},
        "routing": {"modes": ["auto"], "triggers": [{"match": {"regex": "["}}]},
        "requirements": {},
        "health": {"checks": []},
        "requires_capabilities": {},
    }

    manifest, errors = parse_engine_manifest(bad_domains)

    assert manifest is None
    assert "invalid:capabilities.domains" in errors
    assert "invalid:routing.triggers[0].match.regex" in errors


def test_merge_routing_config_documents_p1_merge_rules():
    manifest = _manifest("merge")

    merged = merge_routing_config(
        manifest,
        {
            "routing": {
                "modes": ["github", "auto"],
                "weight": 0.5,
            },
            "fusion": {
                "rrf_k": 42,
            },
        },
    )
    replaced = merge_routing_config(
        manifest,
        {
            "routing": {
                "replace": True,
                "weight": 0.5,
            }
        },
    )

    assert merged["routing"]["modes"] == ["auto", "github"]
    assert merged["routing"]["weight"] == 0.5
    assert merged["fusion"]["rrf_k"] == 42
    assert merged["fusion"]["dedup_key"] == "canonical_url"
    assert replaced["routing"]["weight"] == 0.5


def test_registry_resolve_filters_only_by_actions_and_domains_without_adapter_import(tmp_path):
    runtime = detect_runtime(explicit="standalone", cwd=tmp_path, env={})
    env = load_env(runtime, overrides={}, env_files=[])
    web = _manifest(
        "web_search",
        actions=["search", "extract"],
        domains=["web"],
        adapter="not_a_real_module:MissingEngine",
    )
    code = _manifest(
        "code_search",
        actions=["search"],
        domains=["code", "repositories"],
        adapter="also_missing:MissingEngine",
    )
    discoveries = [
        EngineDiscovery(
            path=Path(f"/tmp/{manifest.id}/engine.yaml"),
            source="builtin",
            trust_level="builtin",
            valid=True,
            manifest=manifest,
        )
        for manifest in (web, code)
    ]
    registry = EngineRegistry(
        runtime=runtime,
        env=env,
        discoveries=discoveries,
        include_builtin=False,
    )

    extract_web = registry.resolve(action="extract", domain="web")
    search_code = registry.resolve(action="search", domain="repositories")

    assert [item.id for item in extract_web] == ["web_search"]
    assert [item.id for item in search_code] == ["code_search"]
    assert all(item.adapter_imported is False for item in extract_web + search_code)
