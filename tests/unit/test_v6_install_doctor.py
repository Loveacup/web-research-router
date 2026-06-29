"""P0-T5 install/doctor v6 CLI surface tests."""

from __future__ import annotations

import json
import subprocess
import sys

from wrr.cli.install import install
from wrr.doctor import doctor_v6


def test_install_dry_run_reports_codex_env_candidates_without_writes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    report = install(dry_run=True, runtime_hint="codex", cwd=tmp_path, env={})
    payload = report.to_dict()

    assert payload["dry_run"] is True
    assert payload["runtime"]["name"] == "codex"
    assert payload["planned_writes"] == []
    assert payload["summary"]["writes_performed"] == 0
    assert not (home / ".config" / "wrr" / "config.yaml").exists()
    assert any("codex.env" in item["path"] for item in payload["env_candidates"])


def test_install_dry_run_reports_hermes_env_candidates(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    payload = install(dry_run=True, runtime_hint="hermes", cwd=tmp_path, env={}).to_dict()

    assert payload["runtime"]["name"] == "hermes"
    assert any(".hermes/.env" in item["path"] for item in payload["env_candidates"])


def test_doctor_v6_json_shape_contains_control_plane_sections(tmp_path):
    payload = doctor_v6(runtime_hint="standalone", cwd=tmp_path, env={}).to_dict()

    assert {"runtime", "env", "discovered", "resolved", "health", "summary", "trust"} <= set(payload)
    assert payload["runtime"]["name"] == "standalone"
    assert payload["summary"]["discovered"] >= 5
    assert any(item["id"] == "exa" for item in payload["resolved"])


def test_doctor_v6_missing_required_env_is_unhealthy_and_not_routable(tmp_path):
    payload = doctor_v6(runtime_hint="standalone", cwd=tmp_path, env={}).to_dict()
    exa = next(item for item in payload["resolved"] if item["id"] == "exa")

    assert exa["configured"] is False
    assert exa["health"]["status"] == "unhealthy"
    assert exa["routable"] is False
    assert "EXA_API_KEY" not in payload["env"]["values"]


def test_doctor_v6_project_env_secret_is_ignored_by_default(tmp_path):
    (tmp_path / ".env").write_text("EXA_API_KEY=secret\n", encoding="utf-8")

    payload = doctor_v6(runtime_hint="standalone", cwd=tmp_path, env={}).to_dict()
    exa = next(item for item in payload["resolved"] if item["id"] == "exa")

    assert exa["configured"] is False
    assert payload["env"]["ignored_values"][0]["key"] == "EXA_API_KEY"
    assert payload["env"]["ignored_values"][0]["ignore_reason"] == "project_env_ignored_secret"


def test_legacy_cli_doctor_json_keeps_old_top_level_shape():
    completed = subprocess.run(
        [sys.executable, "wrr-cli.py", "doctor", "--json"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    payload = json.loads(completed.stdout)

    assert {"ok", "status", "summary", "engines"} <= set(payload)
    assert "discovered" not in payload
    assert "resolved" not in payload
    assert "health" not in payload
