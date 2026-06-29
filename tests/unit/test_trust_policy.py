"""P1-T3 R4 trust boundary hardening tests."""

from __future__ import annotations

import json

from wrr.cli.install import install
from wrr.doctor import doctor_v6
from wrr.engines.loader import discover_engine_plugins
from wrr.engines.registry import EngineRegistry
from wrr.runtime.detect import detect_runtime
from wrr.runtime.env import EnvFileCandidate, load_env


def _manifest(engine_id: str = "project_engine", **overrides):
    payload = {
        "schema_version": 1,
        "id": engine_id,
        "name": engine_id.replace("_", " ").title(),
        "kind": "web_api",
        "adapter": "./adapter.py",
        "capabilities": {"actions": ["search"]},
        "routing": {"modes": ["auto"], "weight": 1.0},
        "requirements": {"env": [], "binaries": [], "repos": []},
        "health": {"checks": []},
        "requires_capabilities": {},
    }
    payload.update(overrides)
    return payload


def _write_plugin(tmp_path, payload=None):
    plugin_dir = tmp_path / "plugins" / "engines" / "project_engine"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "adapter.py").write_text("raise AssertionError('must not import')\n", encoding="utf-8")
    (plugin_dir / "engine.yaml").write_text(json.dumps(payload or _manifest()), encoding="utf-8")
    return plugin_dir


def _runtime(tmp_path):
    return detect_runtime(explicit="standalone", cwd=tmp_path, env={})


def test_project_plugin_adapter_is_discovered_but_not_resolved_by_default(tmp_path):
    _write_plugin(tmp_path)
    runtime = _runtime(tmp_path)
    snapshot = load_env(runtime, env_files=[])

    discoveries = discover_engine_plugins([tmp_path / "plugins" / "engines"], include_builtin=False)
    registry = EngineRegistry(
        runtime=runtime,
        env=snapshot,
        plugin_paths=[tmp_path / "plugins" / "engines"],
        include_builtin=False,
    )
    descriptor = registry.report().resolved[0]

    assert discoveries[0].valid is True
    assert discoveries[0].trust_level == "project"
    assert "untrusted_project_plugin" in discoveries[0].blocked_reasons
    assert "untrusted_project_adapter_path" in discoveries[0].blocked_reasons
    assert descriptor.resolved is False
    assert descriptor.adapter_load_allowed is False
    assert descriptor.adapter_imported is False
    assert "untrusted_project_plugin" in descriptor.resolve_reasons


def test_trust_project_allows_project_adapter_resolution_without_import(tmp_path):
    _write_plugin(tmp_path)
    runtime = _runtime(tmp_path)
    snapshot = load_env(runtime, env_files=[])

    discoveries = discover_engine_plugins(
        [tmp_path / "plugins" / "engines"],
        include_builtin=False,
        trust_project=True,
    )
    descriptor = EngineRegistry(
        runtime=runtime,
        env=snapshot,
        plugin_paths=[tmp_path / "plugins" / "engines"],
        include_builtin=False,
        trust_project=True,
    ).report().resolved[0]

    assert discoveries[0].valid is True
    assert discoveries[0].blocked_reasons == ()
    assert descriptor.resolved is True
    assert descriptor.adapter_load_allowed is True
    assert descriptor.adapter_imported is False


def test_project_env_does_not_satisfy_required_secret_unless_trusted(tmp_path):
    (tmp_path / ".env").write_text("EXA_API_KEY=project-secret\n", encoding="utf-8")

    untrusted = doctor_v6(runtime_hint="standalone", cwd=tmp_path, env={}).to_dict()
    trusted = doctor_v6(runtime_hint="standalone", cwd=tmp_path, env={}, trust_project=True).to_dict()

    untrusted_exa = next(item for item in untrusted["resolved"] if item["id"] == "exa")
    trusted_exa = next(item for item in trusted["resolved"] if item["id"] == "exa")

    assert untrusted_exa["configured"] is False
    assert "EXA_API_KEY" not in untrusted["env"]["values"]
    assert untrusted["env"]["ignored_values"][0]["ignore_reason"] == "project_env_ignored_secret"
    assert any(item["code"] == "project_env_ignored_secret" for item in untrusted["findings"])
    assert trusted_exa["configured"] is True
    assert trusted["env"]["values"]["EXA_API_KEY"]["secret_allowed"] is True
    assert trusted["env"]["ignored_values"] == []
    assert any(item["code"] == "trust_project_enabled" for item in trusted["findings"])


def test_explicit_env_path_secret_is_untrusted_by_default(tmp_path):
    env_file = tmp_path.parent / "explicit.env"
    env_file.write_text("EXA_API_KEY=explicit-secret\n", encoding="utf-8")
    runtime = _runtime(tmp_path)

    snapshot = load_env(runtime, env_files=[env_file])

    assert "EXA_API_KEY" not in snapshot.values
    assert snapshot.candidates[0].trust_level == "env_path"
    assert snapshot.ignored_values[0].ignore_reason == "untrusted_env_ignored_secret"


def test_explicit_env_file_candidate_can_still_mark_user_trusted(tmp_path):
    env_file = tmp_path.parent / "user.env"
    env_file.write_text("EXA_API_KEY=user-secret\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    candidate = EnvFileCandidate(
        path=env_file,
        source="runtime_env",
        trust_level="user",
        priority=1,
        exists=True,
    )

    snapshot = load_env(runtime, env_files=[candidate])

    assert "EXA_API_KEY" in snapshot.values
    assert snapshot.values["EXA_API_KEY"].secret_allowed is True


def test_doctor_highlights_untrusted_project_plugin_and_trust_flip(tmp_path):
    _write_plugin(tmp_path)

    untrusted = doctor_v6(runtime_hint="standalone", cwd=tmp_path, env={}).to_dict()
    trusted = doctor_v6(
        runtime_hint="standalone",
        cwd=tmp_path,
        env={},
        trust_project=True,
    ).to_dict()

    project = next(item for item in untrusted["resolved"] if item["id"] == "project_engine")
    trusted_project = next(item for item in trusted["resolved"] if item["id"] == "project_engine")

    assert project["resolved"] is False
    assert any(item["code"] == "untrusted_project_plugin" for item in untrusted["findings"])
    assert trusted_project["resolved"] is True
    assert trusted["trust"]["project"] is True
    assert any(item["code"] == "trust_project_enabled" for item in trusted["findings"])
    assert any(item["code"] == "non_builtin_adapter" for item in trusted["findings"])


def test_install_refuses_project_level_remote_clone_by_default(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write_plugin(
        tmp_path,
        _manifest(
            requirements={
                "env": [],
                "binaries": [],
                "repos": [
                    {
                        "name": "sample",
                        "default_path": "~/.cache/wrr/deps/sample",
                        "remote": "https://example.invalid/sample.git",
                        "pin": "abc123",
                        "required": True,
                    }
                ],
            },
            health={"checks": [{"type": "repo_revision", "repo": "sample"}]},
        ),
    )

    refused = install(
        dry_run=True,
        runtime_hint="standalone",
        cwd=tmp_path,
        env={},
        refresh_deps=True,
    ).to_dict()
    allowed = install(
        dry_run=True,
        runtime_hint="standalone",
        cwd=tmp_path,
        env={},
        refresh_deps=True,
        trust_project=True,
    ).to_dict()

    project_update = next(
        item for item in refused["dependency_updates"] if item["engine_id"] == "project_engine"
    )
    trusted_update = next(
        item for item in allowed["dependency_updates"] if item["engine_id"] == "project_engine"
    )

    assert project_update["status"] == "refused"
    assert project_update["message"] == "project_remote_clone_refused"
    assert refused["summary"]["dependency_updates"]["refused"] == 1
    assert trusted_update["status"] == "planned"
    assert trusted_update["message"] == "would_clone"
