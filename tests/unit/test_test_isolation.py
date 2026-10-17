"""Regression checks for unit-test environment boundaries."""
import os
from pathlib import Path


def test_home_is_per_test_and_empty(tmp_path, _isolate_home):
    assert Path.home() == _isolate_home
    assert os.environ["HOME"] == str(_isolate_home)
    assert _isolate_home.parent == tmp_path.parent
    assert list(tmp_path.iterdir()) == []
    assert list(Path.home().iterdir()) == []


def test_legacy_doctor_does_not_launch_installed_cli(tmp_path, monkeypatch, capsys, mock_doctor_checks):
    import json
    import sys
    from wrr._cli import main

    marker = tmp_path / "external-cli-ran"
    binary = tmp_path / "opencli"
    binary.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('unexpected execution')\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)

    assert main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["engines"][0]["engine"] == "community"
    assert not marker.exists()
    checks, deps = mock_doctor_checks
    checks.assert_awaited_once()
    deps.assert_awaited_once_with(deep=False)
