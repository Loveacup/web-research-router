"""P1-T1 R2 dependency lifecycle tests."""

from __future__ import annotations

import json
from dataclasses import replace

from wrr.cli.install import install
from wrr.cli.update import update
from wrr.doctor import doctor_v6
from wrr.engines.loader import (
    TRUST_BUILTIN,
    TRUST_PROJECT,
    EngineDiscovery,
    RepoRequirement,
    load_engine_manifest,
    verify_repo_pin,
)
from wrr.engines.registry import EngineRegistry
from wrr.runtime.detect import detect_runtime
from wrr.runtime.env import load_env


def _manifest(engine_id="repo_engine", *, required=True, pin="abc123", default_path=None):
    return {
        "schema_version": 1,
        "id": engine_id,
        "name": "Repo Engine",
        "kind": "local_cli",
        "adapter": "wrr.engines.repo:RepoEngine",
        "capabilities": {"actions": ["search"]},
        "routing": {"modes": ["auto"]},
        "requirements": {
            "env": [],
            "binaries": [],
            "repos": [
                {
                    "name": "sample",
                    "env": "WRR_SAMPLE_REPO",
                    "default_path": default_path or "~/.cache/wrr/deps/sample",
                    "remote": "https://example.invalid/sample.git",
                    "pin": pin,
                    "required": required,
                    "trust_required": "user_or_builtin",
                }
            ],
        },
        "health": {"checks": [{"type": "repo_revision", "repo": "sample"}]},
        "requires_capabilities": {},
    }


def _discovery(tmp_path, payload, *, trust_level=TRUST_PROJECT):
    plugin_dir = tmp_path / "plugins" / "engines" / payload["id"]
    plugin_dir.mkdir(parents=True)
    path = plugin_dir / "engine.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_engine_manifest(path, source=trust_level, trust_level=trust_level)


def _registry(tmp_path, discovery, *, revision):
    runtime = detect_runtime(explicit="standalone", cwd=tmp_path, env={})
    env = load_env(runtime, overrides={}, env_files=[])
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    def revision_resolver(path, ref="HEAD"):
        assert path == repo_path
        return revision if ref == "HEAD" else ref

    manifest = discovery.manifest
    repo = manifest.repo_requirements[0]
    rewritten = _manifest(
        manifest.id,
        required=repo.required,
        pin=repo.pin,
        default_path=str(repo_path),
    )
    rewritten_discovery = EngineDiscovery(
        path=discovery.path,
        source=discovery.source,
        trust_level=discovery.trust_level,
        valid=True,
        manifest=replace(
            manifest,
            requirements=rewritten["requirements"],
            health=rewritten["health"],
            repo_requirements=(
                RepoRequirement(
                    name="sample",
                    env="WRR_SAMPLE_REPO",
                    default_path=str(repo_path),
                    remote=repo.remote,
                    pin=repo.pin,
                    required=repo.required,
                    trust_required=repo.trust_required,
                ),
            ),
        ),
    )
    return EngineRegistry(
        runtime=runtime,
        env=env,
        discoveries=[rewritten_discovery],
        include_builtin=False,
        trust_project=True,
        revision_resolver=revision_resolver,
    )


def test_loader_parses_repo_requirement_and_verify_repo_pin_uses_injected_resolver(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    discovery = _discovery(tmp_path, _manifest(default_path=str(repo_path)))

    assert discovery.valid is True
    repo = discovery.manifest.repo_requirements[0]
    assert repo.name == "sample"
    assert repo.remote == "https://example.invalid/sample.git"
    assert repo.pin == "abc123"
    assert repo.default_path == str(repo_path)

    status = verify_repo_pin(
        repo_path,
        "abc123",
        revision_resolver=lambda path, ref="HEAD": "abc123" if ref == "HEAD" else ref,
    )

    assert status.status == "match"
    assert status.current_revision == "abc123"
    assert status.drift is False


def test_repo_pin_mismatch_is_unhealthy_for_required_dependency(tmp_path):
    discovery = _discovery(tmp_path, _manifest(required=True, pin="expected"))
    descriptor = _registry(tmp_path, discovery, revision="actual").get("repo_engine")

    check = descriptor.health.checks[0]
    assert descriptor.health.status == "unhealthy"
    assert check.status == "unhealthy"
    assert check.message == "repo_pin_mismatch"
    assert check.details["revision"] == "actual"
    assert check.details["pin"] == "expected"
    assert check.details["drift"] is True


def test_repo_pin_mismatch_is_degraded_for_optional_dependency(tmp_path):
    discovery = _discovery(tmp_path, _manifest(required=False, pin="expected"))
    descriptor = _registry(tmp_path, discovery, revision="actual").get("repo_engine")

    check = descriptor.health.checks[0]
    assert descriptor.health.status == "degraded"
    assert check.status == "degraded"
    assert check.required is False
    assert descriptor.routable is True


def test_project_remote_clone_is_refused_by_default_and_allowed_when_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("WRR_CACHE_DIR", str(tmp_path / "cache"))
    discovery = _discovery(tmp_path, _manifest())

    default_report = update(
        dry_run=True,
        cwd=tmp_path,
        env={"WRR_CACHE_DIR": str(tmp_path / "cache")},
        discoveries=[discovery],
    ).to_dict()
    trusted_report = update(
        dry_run=True,
        trust_project=True,
        cwd=tmp_path,
        env={"WRR_CACHE_DIR": str(tmp_path / "cache")},
        discoveries=[discovery],
    ).to_dict()

    assert default_report["repos"][0]["status"] == "refused"
    assert default_report["repos"][0]["message"] == "project_remote_clone_refused"
    assert trusted_report["repos"][0]["status"] == "planned"
    assert trusted_report["repos"][0]["path"] == str(tmp_path / "cache" / "deps" / "sample")


def test_builtin_repo_refresh_is_planned_without_project_trust(tmp_path):
    discovery = _discovery(tmp_path, _manifest(), trust_level=TRUST_BUILTIN)

    report = update(dry_run=True, cwd=tmp_path, discoveries=[discovery]).to_dict()

    assert report["repos"][0]["trust_level"] == "builtin"
    assert report["repos"][0]["status"] == "planned"
    assert report["summary"]["planned"] == 1


def test_install_refresh_deps_includes_dependency_update_report(tmp_path):
    discovery = _discovery(tmp_path, _manifest())

    discovered_report = install(
        dry_run=True,
        cwd=tmp_path,
        env={},
        plugin_paths=[tmp_path / "plugins" / "engines"],
        refresh_deps=True,
    ).to_dict()

    assert discovered_report["dependency_updates"][0]["engine_id"] == discovery.engine_id


def test_doctor_v6_reports_repo_commit_revision_and_drift(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _discovery(tmp_path, _manifest(default_path=str(repo_path), pin="expected"))

    payload = doctor_v6(
        runtime_hint="standalone",
        cwd=tmp_path,
        env={},
        plugin_paths=[tmp_path / "plugins" / "engines"],
    ).to_dict()
    repo_engine = next(item for item in payload["resolved"] if item["id"] == "repo_engine")
    check = repo_engine["health"]["checks"][0]

    assert check["type"] == "repo_revision"
    assert {"commit", "revision", "drift"} <= set(check["details"])
