"""P0-T2 environment snapshot tests."""

import json
from dataclasses import dataclass

from wrr.runtime.env import (
    EnvFileCandidate,
    EnvSnapshot,
    EnvValue,
    env_file_candidates,
    load_env,
)


@dataclass(frozen=True)
class RuntimeStub:
    cwd: object
    env_files: list[object]


def test_env_file_candidates_reports_project_and_user_files(tmp_path):
    project_env = tmp_path / ".env"
    user_env = tmp_path.parent / "user.env"
    project_env.write_text("WRR_MODE=dev\n", encoding="utf-8")

    runtime = RuntimeStub(cwd=tmp_path, env_files=[project_env, user_env])

    candidates = env_file_candidates(runtime, tmp_path)

    assert [candidate.path for candidate in candidates] == [project_env, user_env]
    assert candidates[0].trust_level == "project"
    assert candidates[0].source == "project_env"
    assert candidates[0].exists is True
    assert candidates[1].trust_level == "user"
    assert candidates[1].exists is False
    assert candidates[1].reason == "missing"


def test_load_env_parses_simple_dotenv_without_mutating_os_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("WRR_TEST_VALUE", raising=False)
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "export WRR_TEST_VALUE='from-file'",
                "WRR_OTHER=\"quoted\"",
                "BROKEN",
            ]
        ),
        encoding="utf-8",
    )
    runtime = RuntimeStub(cwd=tmp_path / "project", env_files=[env_file])

    snapshot = load_env(runtime)

    assert snapshot.values["WRR_TEST_VALUE"].value == "from-file"
    assert snapshot.values["WRR_OTHER"].value == "quoted"
    assert "WRR_TEST_VALUE" not in __import__("os").environ
    assert snapshot.candidates[0].loaded is True


def test_higher_priority_values_override_lower_priority_files(tmp_path):
    low = tmp_path.parent / "low.env"
    high = tmp_path / ".env"
    low.write_text("WRR_MODE=low\n", encoding="utf-8")
    high.write_text("WRR_MODE=high\n", encoding="utf-8")
    runtime = RuntimeStub(cwd=tmp_path, env_files=[high, low])

    snapshot = load_env(runtime)

    assert snapshot.values["WRR_MODE"].value == "high"
    assert snapshot.values["WRR_MODE"].source_path == high
    assert len(snapshot.conflicts) == 1
    assert snapshot.conflicts[0].key == "WRR_MODE"
    assert snapshot.conflicts[0].winner_path == high
    assert snapshot.conflicts[0].loser_path == low


def test_overrides_have_highest_priority(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("WRR_MODE=file\n", encoding="utf-8")
    runtime = RuntimeStub(cwd=tmp_path, env_files=[env_file])

    snapshot = load_env(runtime, overrides={"WRR_MODE": "override"})

    assert snapshot.values["WRR_MODE"].value == "override"
    assert snapshot.values["WRR_MODE"].source == "overrides"
    assert snapshot.conflicts[0].winner_source == "overrides"


def test_project_env_secret_is_ignored_by_default(tmp_path):
    project_env = tmp_path / ".env"
    user_env = tmp_path.parent / "user.env"
    project_env.write_text("EXA_API_KEY=project-secret\nWRR_MODE=project\n", encoding="utf-8")
    user_env.write_text("EXA_API_KEY=user-secret\n", encoding="utf-8")
    runtime = RuntimeStub(cwd=tmp_path, env_files=[project_env, user_env])

    snapshot = load_env(runtime)

    assert snapshot.values["EXA_API_KEY"].source_path == user_env
    assert snapshot.values["EXA_API_KEY"].value is None
    assert snapshot.values["WRR_MODE"].value == "project"
    assert snapshot.ignored_values[0].key == "EXA_API_KEY"
    assert snapshot.ignored_values[0].ignore_reason == "project_env_ignored_secret"
    assert any("project_env_ignored_secret" in warning for warning in snapshot.warnings)


def test_trust_project_allows_project_secret_to_override(tmp_path):
    project_env = tmp_path / ".env"
    user_env = tmp_path.parent / "user.env"
    project_env.write_text("EXA_API_KEY=project-secret\n", encoding="utf-8")
    user_env.write_text("EXA_API_KEY=user-secret\n", encoding="utf-8")
    runtime = RuntimeStub(cwd=tmp_path, env_files=[project_env, user_env])

    snapshot = load_env(runtime, trust_project=True)

    assert snapshot.values["EXA_API_KEY"].source_path == project_env
    assert snapshot.values["EXA_API_KEY"].secret is True
    assert snapshot.values["EXA_API_KEY"].secret_allowed is True
    assert snapshot.ignored_values == []
    assert snapshot.conflicts[0].winner_path == project_env


def test_snapshot_serialization_redacts_secret_values(tmp_path):
    env_file = tmp_path.parent / "runtime.env"
    env_file.write_text("EXA_API_KEY=super-secret-value\n", encoding="utf-8")
    runtime = RuntimeStub(cwd=tmp_path, env_files=[env_file])

    snapshot = load_env(runtime)
    payload = snapshot.to_dict()
    encoded = json.dumps(payload)

    assert isinstance(snapshot, EnvSnapshot)
    assert isinstance(snapshot.values["EXA_API_KEY"], EnvValue)
    assert snapshot.values["EXA_API_KEY"].redacted == "<secret-present>"
    assert "super-secret-value" not in encoded
    assert payload["values"]["EXA_API_KEY"]["value"] is None


def test_explicit_env_file_candidate_can_load_user_trusted_secret(tmp_path):
    env_file = tmp_path.parent / "runtime.env"
    env_file.write_text("BRAVE_API_KEY=secret\n", encoding="utf-8")
    runtime = RuntimeStub(cwd=tmp_path, env_files=[])

    snapshot = load_env(runtime, env_files=[env_file])

    assert snapshot.values["BRAVE_API_KEY"].source == "explicit_env_path"
    assert snapshot.values["BRAVE_API_KEY"].secret_allowed is True
    assert snapshot.ignored_values == []


def test_snapshot_serialization_uses_home_relative_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    env_file = home / ".hermes" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("EXA_API_KEY=user-secret\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    runtime = RuntimeStub(cwd=tmp_path / "project", env_files=[env_file])

    payload = load_env(runtime).to_dict()

    assert payload["candidates"][0]["path"] == "~/.hermes/.env"
    assert payload["values"]["EXA_API_KEY"]["source_path"] == "~/.hermes/.env"
