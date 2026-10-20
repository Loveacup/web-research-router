"""P0-3 CLI quality/provenance contract tests（不启动真实引擎）。"""
import json

from wrr import _cli
from wrr.errors import AllEnginesFailedError, TerminalReason
from wrr.formatters import format_extract
from wrr.schemas import ExtractOptions, FallbackStep, RouteTrace



def test_cli_fetch_failure_keeps_frozen_result_shape(monkeypatch, capsys):
    async def fake_run(*_args):
        raise AllEnginesFailedError(
            "down", terminal_reason=TerminalReason.FETCH_POLICY_DENIED,
        )

    monkeypatch.setattr(_cli, "_run", fake_run)
    rc = _cli._dispatch(
        "extract", ExtractOptions("https://fixture.invalid"), None,
        "https://fixture.invalid", True, False, format_extract,
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert set(payload) == {"operation", "ok", "error", "detail", "quality"}


def test_cli_fetch_opt_in_exposes_terminal_policy_veto(monkeypatch, capsys):
    async def fake_run(*_args):
        raise AllEnginesFailedError(
            "down", terminal_reason=TerminalReason.FETCH_POLICY_DENIED,
        )

    monkeypatch.setattr(_cli, "_run", fake_run)
    rc = _cli._dispatch(
        "extract", ExtractOptions("https://fixture.invalid"), None,
        "https://fixture.invalid", True, False, format_extract,
        host_fallback_requested=True,
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["host_fallback"] == {
        "status": "denied",
        "reason": "fetch_policy_denied",
    }


def test_cli_fetch_flag_requests_terminal_policy_metadata(monkeypatch, capsys):
    async def fake_run(*_args):
        raise AllEnginesFailedError(
            "down", terminal_reason=TerminalReason.FETCH_POLICY_DENIED,
        )

    monkeypatch.setattr(_cli, "_run", fake_run)
    rc = _cli.main([
        "fetch", "--host-fallback", "request", "--json", "-q", "--",
        "https://fixture.invalid",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["host_fallback"] == {
        "status": "denied",
        "reason": "fetch_policy_denied",
    }


