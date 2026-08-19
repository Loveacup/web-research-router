"""CLI adapter tests for the offline Stage-S evidence gate."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import wrr._cli as cli
import wrr.evidence_gate as evidence_gate


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "wrr-cli.py"


def test_evidence_gate_dispatches_before_legacy_env_loading(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_bytes(b"")
    captured = {}

    def fake_command(ns):
        captured["path"] = ns.path
        captured["modes"] = ns.mode
        captured["json"] = ns.json
        return 1

    monkeypatch.setattr(cli, "cmd_evidence_gate", fake_command, raising=False)
    monkeypatch.setattr(
        cli,
        "load_env",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not load env")),
    )

    rc = cli.main([
        "evidence-gate",
        "--path", str(evidence),
        "--mode", "research",
        "--mode", "grounding",
        "--json",
    ])

    assert rc == 1
    assert captured == {
        "path": str(evidence),
        "modes": ["research", "grounding"],
        "json": True,
    }


def test_json_output_is_deterministic_read_only_and_not_ready(tmp_path, capsys):
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_bytes(b"")
    before_stat = evidence.stat()
    before_hash = hashlib.sha256(evidence.read_bytes()).hexdigest()

    args = ["evidence-gate", "--path", str(evidence), "--mode", "grounding", "--json"]
    first_rc = cli.main(args)
    first = capsys.readouterr()
    second_rc = cli.main(args)
    second = capsys.readouterr()

    assert first_rc == second_rc == 1
    assert first.out == second.out
    assert first.err == second.err == ""
    report = json.loads(first.out)
    assert report["status"] == "NOT_READY"
    assert report["requested_modes"] == ["grounding"]
    assert evidence.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert evidence.stat().st_size == before_stat.st_size
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() == before_hash


def test_missing_or_nonregular_path_returns_usage_failure_without_echoing_path(tmp_path, capsys):
    missing = tmp_path / "private-name.jsonl"
    assert cli.main(["evidence-gate", "--path", str(missing), "--json"]) == 2
    missing_output = capsys.readouterr()
    assert str(missing) not in missing_output.err

    assert cli.main(["evidence-gate", "--path", str(tmp_path), "--json"]) == 2
    directory_output = capsys.readouterr()
    assert str(tmp_path) not in directory_output.err


def test_evidence_gate_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["evidence-gate", "--help"])

    assert exc.value.code == 0
    assert "--path" in capsys.readouterr().out


def test_broken_stdout_maps_to_exit_two(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_bytes(b"")
    ns = cli.build_parser().parse_args([
        "evidence-gate", "--path", str(evidence), "--json",
    ])
    monkeypatch.setattr(
        "builtins.print",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BrokenPipeError()),
    )

    assert cli.cmd_evidence_gate(ns) == 2


def test_fifo_is_rejected_without_blocking(tmp_path):
    fifo = tmp_path / "evidence.fifo"
    os.mkfifo(fifo)

    completed = subprocess.run(
        [sys.executable, str(CLI), "evidence-gate", "--path", str(fifo), "--json"],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=2,
    )

    assert completed.returncode == 2
    assert str(fifo) not in completed.stderr


def test_oversized_regular_file_returns_exit_two(tmp_path, capsys):
    evidence = tmp_path / "oversized.jsonl"
    with evidence.open("wb") as stream:
        stream.truncate(evidence_gate.MAX_FILE_BYTES + 1)

    assert cli.main(["evidence-gate", "--path", str(evidence), "--json"]) == 2
    assert str(evidence) not in capsys.readouterr().err


def test_unexpected_evaluator_error_is_sanitized_to_exit_two(monkeypatch, tmp_path, capsys):
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_bytes(b"")
    monkeypatch.setattr(
        evidence_gate,
        "evaluate_jsonl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private defect")),
    )

    assert cli.main(["evidence-gate", "--path", str(evidence), "--json"]) == 2
    output = capsys.readouterr()
    assert "private defect" not in output.err
    assert str(evidence) not in output.err
