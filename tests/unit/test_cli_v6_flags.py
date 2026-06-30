"""CLI migration-gate tests for v6 opt-in flags."""

from __future__ import annotations

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
