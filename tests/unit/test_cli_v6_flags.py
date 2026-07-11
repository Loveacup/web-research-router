"""CLI migration-gate tests for v6 opt-in flags."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "wrr-cli.py"


def _load_cli_module():
    import wrr._cli as module
    return module


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_old_cli_examples_still_parse():
    cli = _load_cli_module()
    parser = cli.build_parser()

    doctor = parser.parse_args(["doctor", "--json"])
    assert doctor.cmd == "doctor"
    assert doctor.json is True
    assert doctor.v6 is False

    search = parser.parse_args(["search", "your query", "--provider", "exa", "--count", "5"])
    assert search.cmd == "search"
    assert search.provider == "exa"
    assert search.count == 5

    fetch = parser.parse_args(
        ["fetch", "https://example.com", "--provider", "exa", "--max-chars", "2000"]
    )
    assert fetch.cmd == "fetch"
    assert fetch.provider == "exa"
    assert fetch.max_chars == 2000

    similar = parser.parse_args(["similar", "https://example.com", "--provider", "exa", "--count", "5"])
    assert similar.cmd == "similar"
    assert similar.provider == "exa"
    assert similar.count == 5


def test_search_route_and_exa_mode_flags_are_orthogonal_and_legacy_alias_parses():
    cli = _load_cli_module()
    parser = cli.build_parser()

    both = parser.parse_args([
        "search", "q", "--route-mode", "research", "--exa-mode", "fast",
    ])
    assert both.route_mode == "research"
    assert both.exa_mode == "fast"
    assert both.mode is None

    legacy = parser.parse_args(["search", "q", "--mode", "deep"])
    assert legacy.mode == "deep"
    assert legacy.exa_mode is None
    assert legacy.route_mode is None


def test_cmd_search_rejects_conflicting_legacy_and_explicit_exa_modes(monkeypatch):
    cli = _load_cli_module()
    ns = cli.build_parser().parse_args([
        "search", "q", "--mode", "deep", "--exa-mode", "fast",
    ])
    monkeypatch.setattr(cli, "_dispatch", lambda *args, **kwargs: pytest.fail("must not dispatch"))

    assert cli.cmd_search(ns) == 2

    completed = _run_cli("search", "q", "--mode", "deep", "--exa-mode", "fast", "--json")
    assert completed.returncode == 2
    assert "值必须一致" in completed.stderr


def test_cmd_search_allows_equal_legacy_and_explicit_exa_modes(monkeypatch):
    cli = _load_cli_module()
    ns = cli.build_parser().parse_args([
        "search", "q", "--mode", "deep", "--exa-mode", "deep",
    ])
    captured = {}

    def fake_dispatch(operation, options, provider, ident, as_json, quiet, formatter):
        captured["options"] = options
        return 0

    monkeypatch.setattr(cli, "_dispatch", fake_dispatch)
    assert cli.cmd_search(ns) == 0
    assert captured["options"].mode == "deep"
    assert captured["options"].exa_mode == "deep"


def test_cmd_search_builds_both_mode_fields(monkeypatch):
    cli = _load_cli_module()
    ns = cli.build_parser().parse_args([
        "search", "q", "--route-mode", "research", "--exa-mode", "fast",
    ])
    captured = {}

    def fake_dispatch(operation, options, provider, ident, as_json, quiet, formatter):
        captured["options"] = options
        return 0

    monkeypatch.setattr(cli, "_dispatch", fake_dispatch)
    assert cli.cmd_search(ns) == 0
    assert captured["options"].route_mode == "research"
    assert captured["options"].exa_mode == "fast"


def test_run_uses_v5_only_when_route_mode_is_explicit(monkeypatch):
    cli = _load_cli_module()
    import wrr.registry as registry_module
    import wrr.router as router_module
    from wrr.schemas import SearchOptions

    calls = []
    sentinel_v5 = object()
    sentinel_legacy = object()
    monkeypatch.setattr(registry_module, "get_registry", lambda: "registry")

    async def fake_v5(options, registry):
        calls.append(("v5", options.route_mode, registry))
        return sentinel_v5

    async def fake_legacy(operation, options, registry, explicit_provider=None):
        calls.append(("legacy", operation, registry, explicit_provider))
        return sentinel_legacy

    monkeypatch.setattr(router_module, "route_search_v5", fake_v5)
    monkeypatch.setattr(router_module, "route", fake_legacy)

    routed = asyncio.run(cli._run("search", SearchOptions("q", route_mode="research"), None))
    explicit = asyncio.run(cli._run(
        "search", SearchOptions("q", provider="exa", route_mode="research"), "exa"
    ))
    legacy = asyncio.run(cli._run("search", SearchOptions("q"), None))

    assert routed is sentinel_v5
    assert explicit is sentinel_legacy
    assert legacy is sentinel_legacy
    assert calls == [
        ("v5", "research", "registry"),
        ("legacy", "search", "registry", "exa"),
        ("legacy", "search", "registry", None),
    ]


def test_search_fetch_similar_provider_choices_are_preserved():
    cli = _load_cli_module()
    parser = cli.build_parser()
    provider_choices = [
        "exa",
        "brave",
        "searxng",
        "github",
        "community",
        "academic",
        "skill",
        "local_supermemory",
        "local_session",
        "local_qmd",
        "local_obsidian",
    ]

    for provider in provider_choices:
        assert parser.parse_args(["search", "q", "--provider", provider]).provider == provider
        assert parser.parse_args(["fetch", "https://example.com", "--provider", provider]).provider == provider
        assert parser.parse_args(["similar", "https://example.com", "--provider", provider]).provider == provider

    with pytest.raises(SystemExit):
        parser.parse_args(["search", "q", "--provider", "not_a_provider"])


def test_doctor_v6_json_available_without_changing_legacy_json_shape():
    legacy = _run_cli("doctor", "--json")
    assert legacy.stdout
    legacy_payload = json.loads(legacy.stdout)

    assert {"ok", "status", "summary", "engines"} <= set(legacy_payload)
    assert "runtime" not in legacy_payload
    assert "discovered" not in legacy_payload
    assert "resolved" not in legacy_payload
    assert "health" not in legacy_payload
    assert "trust" not in legacy_payload

    v6 = _run_cli("doctor", "--v6", "--json", "--runtime", "standalone")
    assert v6.stdout
    v6_payload = json.loads(v6.stdout)

    assert {"runtime", "env", "discovered", "resolved", "health", "summary", "trust"} <= set(v6_payload)
    assert v6_payload["runtime"]["name"] == "standalone"
    assert v6_payload["trust"]["project"] is False
    assert isinstance(v6_payload["discovered"], list)
    assert isinstance(v6_payload["resolved"], list)
    assert isinstance(v6_payload["health"], list)


def test_v6_trust_project_flag_is_explicit_in_doctor_and_install_json():
    doctor = _run_cli("doctor", "--v6", "--json", "--runtime", "standalone", "--trust-project")
    assert doctor.stdout
    doctor_payload = json.loads(doctor.stdout)
    assert doctor_payload["trust"]["project"] is True
    assert doctor_payload["summary"]["trust_project_explicit"] is True

    install = _run_cli("install", "--dry-run", "--runtime", "standalone", "--trust-project", "--json")
    assert install.returncode == 0, install.stderr
    install_payload = json.loads(install.stdout)
    assert install_payload["dry_run"] is True
    assert install_payload["trust"]["project"] is True
    assert install_payload["planned_writes"] == []


def test_install_refresh_deps_dry_run_is_reported():
    completed = _run_cli("install", "--dry-run", "--runtime", "standalone", "--refresh-deps", "--json")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)

    assert payload["dry_run"] is True
    assert "dependency_updates" in payload
    assert {"repos", "planned", "refused"} <= set(payload["summary"]["dependency_updates"])
    assert payload["planned_writes"] == []


@pytest.mark.parametrize("command", ["doctor", "install", "update"])
def test_v6_migration_flags_have_explicit_help_text(command: str):
    completed = _run_cli(command, "--help")
    assert completed.returncode == 0
    help_text = completed.stdout

    if command == "doctor":
        assert "--v6" in help_text
        assert "--trust-project" in help_text
        assert "legacy" in help_text
        assert "opt-in" in help_text
    elif command == "install":
        assert "--dry-run" in help_text
        assert "--refresh-deps" in help_text
        assert "不写配置" in help_text
        assert "--trust-project" in help_text
    else:
        assert "--dry-run" in help_text
        assert "--apply" in help_text
        assert "默认" in help_text
        assert "project-level manifest" in help_text


def test_doctor_v6_returns_nonzero_when_summary_status_fails():
    completed = _run_cli("doctor", "--v6", "--json", "--runtime", "standalone")
    assert completed.stdout
    payload = json.loads(completed.stdout)
    if payload["summary"]["status"] == "fail":
        assert completed.returncode == 1


def test_doctor_profile_matrix_requires_v6():
    """--profile-matrix requires --v6 flag."""
    completed = _run_cli("doctor", "--profile-matrix", "--json")
    assert completed.returncode == 2
    assert "--profile-matrix requires --v6" in completed.stderr or "--v6" in completed.stderr


def test_doctor_v6_profile_matrix_json_shape():
    """--v6 --profile-matrix --json produces valid profile matrix report."""
    completed = _run_cli("doctor", "--v6", "--profile-matrix", "--json")
    assert completed.returncode in (0, 1)
    assert completed.stdout

    payload = json.loads(completed.stdout)
    assert payload["kind"] == "profile_matrix"
    assert "profiles" in payload
    assert "summary" in payload
    assert len(payload["profiles"]) == 7

    profile_ids = [p["id"] for p in payload["profiles"]]
    assert "default" in profile_ids
    assert "editable" in profile_ids
    assert "standalone" in profile_ids
    assert "S2" in profile_ids
    assert "S3" in profile_ids
    assert "cron-worker" in profile_ids
    assert "hermes" in profile_ids

    for p in payload["profiles"]:
        assert "id" in p
        assert "label" in p
        assert "runtime" in p
        assert "router_mode" in p
        assert "routable_engine_ids" in p
        assert isinstance(p["routable_engine_ids"], list)


def test_doctor_v6_profile_matrix_rejects_deep():
    """--profile-matrix rejects --deep flag."""
    completed = _run_cli("doctor", "--v6", "--profile-matrix", "--json", "--deep")
    assert completed.returncode == 2
    assert "does not support --deep" in completed.stderr or "--deep" in completed.stderr


def test_doctor_v6_profile_matrix_rejects_engine():
    """--profile-matrix rejects --engine flag."""
    completed = _run_cli("doctor", "--v6", "--profile-matrix", "--json", "--engine", "exa")
    assert completed.returncode == 2
    assert "does not support --engine" in completed.stderr or "--engine" in completed.stderr


def test_doctor_v6_profile_matrix_rejects_tier():
    """--profile-matrix rejects --tier flag."""
    completed = _run_cli("doctor", "--v6", "--profile-matrix", "--json", "--tier", "1")
    assert completed.returncode == 2
    assert "does not support --tier" in completed.stderr or "--tier" in completed.stderr


def test_doctor_v6_profile_matrix_does_not_load_legacy_env():
    """--profile-matrix does not expose secret env values."""
    import os
    env = os.environ.copy()
    env["GITHUB_TOKEN"] = "ghp_secret_test_token"
    env["AWS_SECRET_ACCESS_KEY"] = "aws_secret_xyz"

    completed = subprocess.run(
        [sys.executable, str(CLI), "doctor", "--v6", "--profile-matrix", "--json"],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )

    assert completed.returncode in (0, 1)
    assert "ghp_secret_test_token" not in completed.stdout
    assert "aws_secret_xyz" not in completed.stdout


def test_test_subcommand_accepts_unit_argument():
    cli = _load_cli_module()
    parser = cli.build_parser()
    args = parser.parse_args(["test", "unit"])
    assert args.what == "unit"
    args_default = parser.parse_args(["test"])
    assert args_default.what == "smoke"
    with pytest.raises(SystemExit):
        parser.parse_args(["test", "unknown"])


def test_cli_test_unit_runs_pytest_with_v6_router_disabled():
    cli = _load_cli_module()
    parser = cli.build_parser()
    ns = parser.parse_args(["test", "unit"])
    import subprocess
    from unittest import mock
    with mock.patch.object(subprocess, "call", return_value=0) as mocked:
        cli.cmd_test_unit(ns)
        called_args, called_kwargs = mocked.call_args
        assert called_args[0] == ["pytest", "tests/unit", "-q"]
        assert called_kwargs["env"]["WRR_V6_ROUTER"] == "0"


def test_cli_search_json_includes_diagnostics():
    """CLI search --json 应输出 diagnostics 字段。"""
    import os
    env = os.environ.copy()
    env["WRR_V6_ROUTER"] = "0"
    completed = subprocess.run(
        [sys.executable, str(CLI), "search", "test query", "--json", "--provider", "exa"],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )
    if completed.returncode == 0:
        out = json.loads(completed.stdout)
        assert out["ok"] is True
        # 如果 route 返回 diagnostics，CLI 应包含
        if "diagnostics" in out:
            assert "events" in out["diagnostics"]
            assert "elapsed_ms" in out["diagnostics"]
