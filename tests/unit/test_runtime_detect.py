"""P0-T1 runtime detection tests."""

import json

from wrr.runtime import ProcessSnapshot, detect_runtime


def test_explicit_codex_wins_with_full_confidence(tmp_path):
    info = detect_runtime(explicit="codex", cwd=tmp_path, env={"WRR_RUNTIME": "hermes"})

    assert info.name == "codex"
    assert info.source == "explicit"
    assert info.confidence == 1.0
    assert info.capabilities.agent_memory is True
    assert info.capabilities.ai_cli_search is True


def test_wrr_runtime_env_overrides_cwd_marker(tmp_path):
    (tmp_path / ".codex").mkdir()

    info = detect_runtime(cwd=tmp_path, env={"WRR_RUNTIME": "hermes"})

    assert info.name == "hermes"
    assert info.source == "env"
    assert any("ignored codex from cwd_config" in warning for warning in info.warnings)


def test_env_signal_detects_claude_code(tmp_path):
    info = detect_runtime(cwd=tmp_path, env={"CLAUDECODE_SESSION_ID": "x"})

    assert info.name == "claude_code"
    assert info.source == "env"
    assert info.capabilities.conversation_history is True


def test_cwd_marker_beats_process_signal(tmp_path):
    (tmp_path / ".omp").mkdir()
    process = ProcessSnapshot(parent_executable="/usr/local/bin/codex")

    info = detect_runtime(cwd=tmp_path, env={}, process=process)

    assert info.name == "omp"
    assert info.source == "config"
    assert any("ignored codex from process" in warning for warning in info.warnings)


def test_process_signal_detects_codex(tmp_path):
    process = ProcessSnapshot(executable="/opt/homebrew/bin/codex", argv=["codex", "exec"])

    info = detect_runtime(cwd=tmp_path, env={}, process=process)

    assert info.name == "codex"
    assert info.source == "process"


def test_standalone_fallback_capabilities(tmp_path):
    info = detect_runtime(cwd=tmp_path, env={})

    assert info.name == "standalone"
    assert info.source == "fallback"
    assert info.capabilities.agent_memory is False
    assert info.capabilities.ai_cli_search is False
    assert info.capabilities.local_kb is True


def test_runtime_info_to_dict_is_json_serializable_and_redacted(tmp_path):
    info = detect_runtime(cwd=tmp_path, env={"CODEX_SECRET_TOKEN": "do-not-leak"})

    payload = info.to_dict()
    encoded = json.dumps(payload)

    assert payload["name"] == "codex"
    assert str(tmp_path) in encoded
    assert "do-not-leak" not in encoded
    assert "CODEX_SECRET_TOKEN" not in encoded
