"""Profile matrix diagnostic tests."""
import os
from pathlib import Path
import pytest
from wrr.doctor import doctor_profile_matrix, default_doctor_profiles, DoctorProfile


def test_profile_matrix_default_shape_without_real_env(monkeypatch, tmp_path):
    """Matrix returns 7 profiles with expected structure."""
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("OPENCLI_TOKEN", raising=False)

    report = doctor_profile_matrix(
        json=True,
        deep=False,
        trust_project=False,
        runtime_hint=None,
        cwd=tmp_path,
        env=dict(os.environ),
        env_files=None,
        plugin_paths=None
    )

    assert report.kind == "profile_matrix"
    assert len(report.profiles) == 7

    profile_ids = [p["id"] for p in report.profiles]
    assert "default" in profile_ids
    assert "editable" in profile_ids
    assert "standalone" in profile_ids
    assert "S2" in profile_ids
    assert "S3" in profile_ids
    assert "cron-worker" in profile_ids
    assert "hermes" in profile_ids

    for p in report.profiles:
        assert "id" in p
        assert "label" in p
        assert "runtime" in p
        assert "router_mode" in p
        assert "routable_engine_ids" in p

    assert "status" in report.summary


def test_profile_matrix_maps_profiles_to_existing_runtime_names(monkeypatch, tmp_path):
    """Each profile's runtime.name matches known runtime names."""
    monkeypatch.setenv("WRR_V6_ROUTER", "0")

    report = doctor_profile_matrix(
        json=True,
        deep=False,
        trust_project=False,
        runtime_hint=None,
        cwd=tmp_path,
        env=dict(os.environ),
        env_files=None,
        plugin_paths=None
    )

    known_runtimes = {"editable", "standalone", "codex", "hermes"}
    for p in report.profiles:
        runtime_name = p["runtime"]["name"]
        assert runtime_name in known_runtimes


def test_profile_matrix_does_not_leak_unrelated_or_secret_env(monkeypatch, tmp_path):
    """Matrix does not expose secret env values."""
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret123456")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_secret_xyz")
    monkeypatch.setenv("UNRELATED_VAR", "unrelated_value")

    report = doctor_profile_matrix(
        json=True,
        deep=False,
        trust_project=False,
        runtime_hint=None,
        cwd=tmp_path,
        env=dict(os.environ),
        env_files=None,
        plugin_paths=None
    )

    report_str = str(report.to_dict())
    assert "ghp_secret123456" not in report_str
    assert "aws_secret_xyz" not in report_str
    assert "unrelated_value" not in report_str


def test_profile_matrix_env_file_applies_to_all_profiles(monkeypatch, tmp_path):
    """Env file is loaded for all profiles."""
    monkeypatch.setenv("WRR_V6_ROUTER", "0")

    env_file = tmp_path / ".env.test"
    env_file.write_text("GITHUB_TOKEN=ghp_from_file\nEXA_API_KEY=exa_from_file\n")

    report = doctor_profile_matrix(
        json=True,
        deep=False,
        trust_project=False,
        runtime_hint=None,
        cwd=tmp_path,
        env=dict(os.environ),
        env_files=[env_file],
        plugin_paths=None
    )

    for p in report.profiles:
        env_candidates_count = p.get("env", {}).get("candidates", 0)
        assert env_candidates_count > 0


def test_profile_matrix_rejects_deep(monkeypatch, tmp_path):
    """Matrix raises ValueError when deep=True."""
    monkeypatch.setenv("WRR_V6_ROUTER", "0")

    with pytest.raises(ValueError, match="does not support --deep"):
        doctor_profile_matrix(
            json=True,
            deep=True,
            trust_project=False,
            runtime_hint=None,
            cwd=tmp_path,
            env=dict(os.environ),
            env_files=None,
            plugin_paths=None
        )


def test_default_doctor_profiles():
    """default_doctor_profiles returns 7 profiles with correct structure."""
    profiles = default_doctor_profiles(runtime_hint=None, trust_project=False)

    assert len(profiles) == 7
    assert all(isinstance(p, DoctorProfile) for p in profiles)

    profile_dict = {p.id: p for p in profiles}

    assert profile_dict["default"].runtime_hint is None
    assert profile_dict["default"].trust_project is False

    assert profile_dict["editable"].runtime_hint == "editable"
    assert profile_dict["editable"].trust_project is True

    assert profile_dict["standalone"].runtime_hint == "standalone"
    assert profile_dict["standalone"].trust_project is False

    assert profile_dict["S2"].runtime_hint == "codex"
    assert profile_dict["S2"].trust_project is False

    assert profile_dict["S3"].runtime_hint == "hermes"
    assert profile_dict["S3"].trust_project is False

    assert profile_dict["cron-worker"].runtime_hint == "standalone"
    assert profile_dict["cron-worker"].trust_project is False

    assert profile_dict["hermes"].runtime_hint == "hermes"
    assert profile_dict["hermes"].trust_project is False
