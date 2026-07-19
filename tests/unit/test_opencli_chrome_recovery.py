"""P1 OpenCLI Chrome restart control-plane slice — unit tests.

Contract under test: ``wrr.doctor.opencli_recover_once``.

Design gates enforced here:
  * opt-in / default-off: nothing destructive happens unless the caller passes
    ``user_confirmed=True`` AND the bridge is actually disconnected.
  * fully injectable: every side effect (probe / chrome quit+reopen / daemon
    restart / clock / tab count) is a callable the test supplies, so no test
    ever touches a real Chrome, a real OpenCLI daemon, or the wall clock.
  * never wired into the search/route hot path.
  * no browser-automation dependency is introduced.
"""

import subprocess
from pathlib import Path

import pytest

import wrr.doctor as doctor
from wrr.doctor import (
    RecoveryResult,
    _probe_opencli_bridge_status,
    _run_chrome_quit,
    opencli_recover_once,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── injectable fakes ────────────────────────────────────────────────


class FakeProbe:
    """Return a scripted bridge_status per call; record timeouts seen."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def __call__(self, *, timeout_sec):
        idx = len(self.calls)
        self.calls.append(timeout_sec)
        status = self.statuses[min(idx, len(self.statuses) - 1)]
        return {
            "bridge_status": status,
            "source_status_summary": {"probe_index": idx, "opencli": status},
        }


class FakeAction:
    """Scripted step: each call yields the next result dict, or raises if it
    is an Exception instance. Records the timeout it was called with."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, *, timeout_sec):
        idx = len(self.calls)
        self.calls.append(timeout_sec)
        result = self.results[min(idx, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def count(self):
        return len(self.calls)


def _never(*, timeout_sec):  # pragma: no cover - asserts it is never called
    raise AssertionError("destructive step must not run in this scenario")


def _clock():
    """Deterministic monotonic-ish clock; increments 0.1s per read."""
    state = {"t": 0.0}

    def _tick():
        state["t"] += 0.1
        return state["t"]

    return _tick


def _tabs(n):
    def _count(*, timeout_sec):
        return n

    return _count


# ── skipped_because_safe: already connected ─────────────────────────


def test_skipped_when_already_connected():
    probe = FakeProbe(["connected"])
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=probe,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(3),
        now=_clock(),
    )
    assert isinstance(res, RecoveryResult)
    assert res.attempted is False
    assert res.outcome == "skipped_because_safe"
    assert res.actions == []
    assert res.before["bridge_status"] == "connected"
    assert res.after == res.before
    # probe hit exactly once (no re-probe when nothing changed)
    assert len(probe.calls) == 1


def test_skipped_when_not_confirmed_even_if_disconnected():
    probe = FakeProbe(["disconnected"])
    res = opencli_recover_once(
        user_confirmed=False,  # explicit opt-in required
        probe_status=probe,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.attempted is False
    assert res.outcome == "skipped_because_safe"
    assert res.actions == []
    assert res.evidence.get("reason") == "user_not_confirmed"


def test_default_is_off():
    """Called with no user_confirmed kwarg -> defaults to opt-out."""
    probe = FakeProbe(["disconnected"])
    res = opencli_recover_once(
        probe_status=probe,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.attempted is False
    assert res.outcome == "skipped_because_safe"


# ── recovered / still_disconnected ──────────────────────────────────


def test_recovered_after_one_time_restart():
    probe = FakeProbe(["disconnected", "connected"])
    chrome = FakeAction([{"ok": True, "detail": "reopened owned profile"}])
    daemon = FakeAction([{"ok": True, "detail": "daemon up"}])
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=probe,
        run_chrome_quit=chrome,
        run_opencli_daemon_restart=daemon,
        count_open_tabs=_tabs(5),
        now=_clock(),
    )
    assert res.attempted is True
    assert res.outcome == "recovered"
    assert res.before["bridge_status"] == "disconnected"
    assert res.after["bridge_status"] == "connected"
    # exactly one chrome restart, then one daemon restart, in order
    assert res.actions == ["chrome_quit_reopen", "opencli_daemon_restart"]
    assert chrome.count == 1
    assert daemon.count == 1
    # diagnose + re-probe = 2 probes
    assert len(probe.calls) == 2


def test_still_disconnected_after_restart():
    probe = FakeProbe(["disconnected", "disconnected"])
    chrome = FakeAction([{"ok": True}])
    daemon = FakeAction([{"ok": True}])
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=probe,
        run_chrome_quit=chrome,
        run_opencli_daemon_restart=daemon,
        count_open_tabs=_tabs(2),
        now=_clock(),
    )
    assert res.attempted is True
    assert res.outcome == "still_disconnected"
    assert res.actions == ["chrome_quit_reopen", "opencli_daemon_restart"]


# ── fail-closed on non-disconnected (unknown) status ────────────────
#
# Only a *definitely disconnected* bridge may enter the destructive
# Chrome+daemon recovery. An "unknown" status (opencli missing, probe
# timeout, or any non-"disconnected" token) must be treated as safe:
# attempted=False, no actions, skipped_because_safe — even when the
# operator passed user_confirmed=True.


def test_unknown_status_confirmed_is_skipped_not_destructive():
    probe = FakeProbe(["unknown"])
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=probe,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.attempted is False
    assert res.actions == []
    assert res.outcome == "skipped_because_safe"
    assert res.evidence.get("reason") == "status_not_disconnected"
    # never re-probed; touched nothing
    assert len(probe.calls) == 1
    assert res.after == res.before


def test_probe_missing_opencli_yields_unknown_and_skips():
    """Real probe returns bridge_status=unknown when opencli is missing."""

    def _missing_probe(*, timeout_sec):
        return {
            "bridge_status": "unknown",
            "source_status_summary": {},
            "detail": "opencli not found",
        }

    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=_missing_probe,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.attempted is False
    assert res.outcome == "skipped_because_safe"
    assert res.evidence.get("reason") == "status_not_disconnected"


def test_probe_timeout_yields_unknown_and_skips():
    """Real probe returns bridge_status=unknown on a probe timeout."""

    def _timeout_probe(*, timeout_sec):
        return {
            "bridge_status": "unknown",
            "source_status_summary": {},
            "detail": "probe timeout",
        }

    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=_timeout_probe,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.attempted is False
    assert res.outcome == "skipped_because_safe"
    assert res.evidence.get("reason") == "status_not_disconnected"


# ── fail-closed on step failure ─────────────────────────────────────


def test_chrome_step_failure_is_fail_closed_and_skips_daemon():
    probe = FakeProbe(["disconnected"])
    # ok=False twice -> exhausts the single bounded retry -> fail-closed
    chrome = FakeAction([{"ok": False, "detail": "no owned profile"}])
    daemon = FakeAction([{"ok": True}])
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=probe,
        run_chrome_quit=chrome,
        run_opencli_daemon_restart=daemon,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.attempted is True
    assert res.outcome == "error"
    # chrome never succeeded -> not recorded as a completed action
    assert res.actions == []
    # daemon restart must NOT run once chrome failed
    assert daemon.count == 0
    # no re-probe after a failed recovery
    assert len(probe.calls) == 1


def test_daemon_step_failure_is_fail_closed():
    probe = FakeProbe(["disconnected"])
    chrome = FakeAction([{"ok": True}])
    daemon = FakeAction([RuntimeError("boom"), RuntimeError("boom again")])
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=probe,
        run_chrome_quit=chrome,
        run_opencli_daemon_restart=daemon,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.outcome == "error"
    assert res.actions == ["chrome_quit_reopen"]
    assert "recovery_error" in res.evidence


def test_diagnose_error_returns_error_outcome():
    def _boom_probe(*, timeout_sec):
        raise RuntimeError("probe blew up")

    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=_boom_probe,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.attempted is False
    assert res.outcome == "error"
    assert "diagnose_error" in res.evidence


# ── bounded retries (<= 1) ──────────────────────────────────────────


def test_bounded_retry_recovers_on_second_attempt():
    probe = FakeProbe(["disconnected", "connected"])
    # first attempt fails, retry (attempt #2) succeeds
    chrome = FakeAction([{"ok": False}, {"ok": True}])
    daemon = FakeAction([{"ok": True}])
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=probe,
        run_chrome_quit=chrome,
        run_opencli_daemon_restart=daemon,
        count_open_tabs=_tabs(1),
        now=_clock(),
    )
    assert res.outcome == "recovered"
    # exactly two attempts: original + one bounded retry
    assert chrome.count == 2


def test_retry_is_capped_at_one():
    probe = FakeProbe(["disconnected"])
    # fails 3 times; with cap=1 only 2 attempts happen, then fail-closed
    chrome = FakeAction([{"ok": False}, {"ok": False}, {"ok": False}])
    daemon = FakeAction([{"ok": True}])
    res = opencli_recover_once(
        user_confirmed=True,
        max_retries=99,  # caller over-asks; implementation must clamp to <=1
        probe_status=probe,
        run_chrome_quit=chrome,
        run_opencli_daemon_restart=daemon,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.outcome == "error"
    assert chrome.count == 2  # 1 original + 1 retry, never more


# ── tab count is best-effort and never affects the main flow ─────────


def test_tab_count_recorded():
    probe = FakeProbe(["disconnected", "connected"])
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=probe,
        run_chrome_quit=FakeAction([{"ok": True}]),
        run_opencli_daemon_restart=FakeAction([{"ok": True}]),
        count_open_tabs=_tabs(7),
        now=_clock(),
    )
    assert res.evidence.get("open_tabs") == 7


def test_tab_count_failure_does_not_break_recovery():
    def _boom_tabs(*, timeout_sec):
        raise RuntimeError("tab count unavailable")

    probe = FakeProbe(["disconnected", "connected"])
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=probe,
        run_chrome_quit=FakeAction([{"ok": True}]),
        run_opencli_daemon_restart=FakeAction([{"ok": True}]),
        count_open_tabs=_boom_tabs,
        now=_clock(),
    )
    # recovery still succeeds despite the tab-count probe failing
    assert res.outcome == "recovered"
    assert res.evidence.get("open_tabs") is None
    assert "open_tabs_error" in res.evidence


# ── result shape ────────────────────────────────────────────────────


def test_result_shape_and_to_dict():
    probe = FakeProbe(["connected"])
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=probe,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    for field in ("attempted", "before", "actions", "after", "outcome", "evidence"):
        assert hasattr(res, field)
    d = res.to_dict()
    assert set(d) == {"attempted", "before", "actions", "after", "outcome", "evidence"}
    assert set(d["before"]) == {"bridge_status", "source_status_summary"}
    assert set(d["after"]) == {"bridge_status", "source_status_summary"}


# ── production helper _run_chrome_quit: honour subprocess return codes ──
#
# These exercise the real production default, but with doctor.subprocess.run
# monkeypatched to a fake — no real pkill/open/Chrome ever runs.


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRun:
    """Fake subprocess.run: returns a scripted returncode per command name,
    or raises if the scripted value is an Exception. Records the argv seen."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        val = self.script.get(cmd[0], 0)
        if isinstance(val, Exception):
            raise val
        return _FakeCompleted(returncode=val)

    @property
    def names(self):
        return [c[0] for c in self.calls]


def _owned_profile(monkeypatch):
    monkeypatch.setenv("WRR_OPENCLI_CHROME_PROFILE", "/tmp/opencli-owned-profile")
    return "/tmp/opencli-owned-profile"


def test_chrome_quit_pkill_1_open_0_is_ok(monkeypatch):
    """pkill rc=1 (no matching process) is fine; reopen still runs and succeeds."""
    profile = _owned_profile(monkeypatch)
    fake = _FakeRun({"pkill": 1, "open": 0})
    monkeypatch.setattr(doctor.subprocess, "run", fake)
    res = _run_chrome_quit(timeout_sec=1.0)
    assert res["ok"] is True
    assert fake.names == ["pkill", "open"]
    # profile text is allowed in evidence; nothing else sensitive is
    assert profile in res["detail"]


def test_chrome_quit_pkill_0_open_0_is_ok(monkeypatch):
    _owned_profile(monkeypatch)
    fake = _FakeRun({"pkill": 0, "open": 0})
    monkeypatch.setattr(doctor.subprocess, "run", fake)
    res = _run_chrome_quit(timeout_sec=1.0)
    assert res["ok"] is True
    assert fake.names == ["pkill", "open"]


def test_chrome_quit_pkill_abnormal_fail_closed_and_skips_open(monkeypatch):
    """pkill rc>=2 means we cannot trust process state -> fail-closed, no reopen."""
    _owned_profile(monkeypatch)
    fake = _FakeRun({"pkill": 2, "open": 0})
    monkeypatch.setattr(doctor.subprocess, "run", fake)
    res = _run_chrome_quit(timeout_sec=1.0)
    assert res["ok"] is False
    # open (reopen) must NOT run after an abnormal pkill
    assert fake.names == ["pkill"]


def test_chrome_quit_open_abnormal_fail_closed(monkeypatch):
    _owned_profile(monkeypatch)
    fake = _FakeRun({"pkill": 0, "open": 3})
    monkeypatch.setattr(doctor.subprocess, "run", fake)
    res = _run_chrome_quit(timeout_sec=1.0)
    assert res["ok"] is False
    assert fake.names == ["pkill", "open"]


def test_chrome_quit_timeout_is_fail_closed(monkeypatch):
    _owned_profile(monkeypatch)

    def _raise(cmd, *a, **k):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(doctor.subprocess, "run", _raise)
    res = _run_chrome_quit(timeout_sec=1.0)
    assert res["ok"] is False


def test_chrome_quit_no_profile_refuses(monkeypatch):
    monkeypatch.delenv("WRR_OPENCLI_CHROME_PROFILE", raising=False)
    # subprocess.run must never even be reached when no owned profile is set
    fake = _FakeRun({})
    monkeypatch.setattr(doctor.subprocess, "run", fake)
    res = _run_chrome_quit(timeout_sec=1.0)
    assert res["ok"] is False
    assert fake.calls == []


def test_chrome_quit_failure_detail_leaks_no_process_output(monkeypatch):
    """On failure, detail may include the profile + rc but not raw stdout/stderr."""
    _owned_profile(monkeypatch)

    class _NoisyRun(_FakeRun):
        def __call__(self, cmd, *args, **kwargs):
            self.calls.append(list(cmd))
            return _FakeCompleted(returncode=7, stdout="SECRET_STDOUT", stderr="SECRET_STDERR")

    monkeypatch.setattr(doctor.subprocess, "run", _NoisyRun({}))
    res = _run_chrome_quit(timeout_sec=1.0)
    assert res["ok"] is False
    assert "SECRET_STDOUT" not in res["detail"]
    assert "SECRET_STDERR" not in res["detail"]


# ── design gates: opt-in only, never in hot path, no browser automation ─


def test_recovery_not_wired_into_search_or_route_hot_path():
    for rel in ("wrr/engines/community.py", "wrr/router.py"):
        content = (REPO_ROOT / rel).read_text()
        assert "opencli_recover_once" not in content, (
            f"{rel} must never auto-invoke opencli_recover_once (control-plane only)"
        )


def test_doctor_has_no_browser_automation_dependency():
    content = (REPO_ROOT / "wrr" / "doctor.py").read_text().lower()
    for banned in ("playwright", "puppeteer", "selenium"):
        assert banned not in content, f"doctor.py must not import {banned}"


# ── production probe _probe_opencli_bridge_status: honour the exit code ──
#
# The read-only probe shells out to ``opencli daemon status``. When that
# process exits nonzero the probe is *broken*, and its stdout/stderr can no
# longer be trusted to describe the bridge — a nonzero exit may still emit
# "disconnected" (or even "extension: connected") noise. Parsing that text and
# returning bridge_status="disconnected" would license a destructive Chrome
# restart under user_confirmed=True. So a nonzero exit MUST fail-closed to
# bridge_status="unknown" without parsing the output at all.
#
# These exercise the real production default with doctor.subprocess.run
# monkeypatched — no real opencli / Chrome / subprocess ever runs.


def _probe_run(returncode=0, stdout="", stderr=""):
    """Fake subprocess.run for the probe: scripted returncode + stdout/stderr."""

    def _run(cmd, *args, **kwargs):
        return _FakeCompleted(returncode=returncode, stdout=stdout, stderr=stderr)

    return _run


def test_probe_nonzero_returncode_with_disconnected_text_is_unknown(monkeypatch):
    """Nonzero exit + misleading 'disconnected' text -> unknown, never disconnected."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=1, stdout="daemon error: extension: disconnected"),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "unknown"
    assert res["bridge_status"] != "disconnected"
    assert res["source_status_summary"].get("exit_code") == 1


def test_probe_nonzero_returncode_with_connected_text_is_unknown(monkeypatch):
    """Nonzero exit is untrusted even if stdout says 'extension: connected'."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=2, stdout="extension: connected"),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "unknown"


def test_probe_nonzero_detail_has_exit_code_and_no_process_output(monkeypatch):
    """Failure detail is a stable token with the exit code, never raw output."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=5, stdout="SECRET_STDOUT connected", stderr="SECRET_STDERR"),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "unknown"
    assert "exit_code=5" in res["detail"]
    assert "SECRET_STDOUT" not in res["detail"]
    assert "SECRET_STDERR" not in res["detail"]
    assert res["source_status_summary"].get("exit_code") == 5


def test_probe_zero_returncode_connected_unchanged(monkeypatch):
    """returncode==0 keeps the existing connected text parsing."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=0, stdout="extension: connected"),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "connected"


def test_probe_zero_returncode_disconnected_unchanged(monkeypatch):
    """returncode==0 keeps the existing disconnected text parsing."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=0, stdout="extension: disconnected"),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "disconnected"


def test_recovery_with_real_probe_nonzero_returncode_has_no_side_effect(monkeypatch):
    """Integration: a nonzero-exit probe under user_confirmed=True must skip and
    touch nothing — no Chrome quit, no daemon restart, no re-probe."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=1, stdout="extension: disconnected (stale)"),
    )
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=_probe_opencli_bridge_status,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.attempted is False
    assert res.actions == []
    assert res.outcome == "skipped_because_safe"
    assert res.evidence.get("reason") == "status_not_disconnected"
    assert res.after == res.before


# ── production probe zero-exit: only *explicit* tokens are trusted ──────
#
# A clean exit (returncode==0) is NECESSARY but NOT SUFFICIENT to trust the
# text as "disconnected". Only an explicit "extension: disconnected" marker
# means disconnected; only an explicit "extension: connected" (with no
# disconnected marker) means connected. Empty output, unrecognized text, or a
# contradictory mix of both markers is NOT a mandate to restart Chrome — it
# must fail-closed to bridge_status="unknown" so the recovery orchestrator
# treats it as safe under user_confirmed=True. The unknown detail is a stable
# token; raw stdout/stderr is never surfaced.


def test_probe_zero_returncode_empty_output_is_unknown(monkeypatch):
    """Clean exit but no output at all -> unknown, never disconnected."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=0, stdout="", stderr=""),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "unknown"
    assert res["bridge_status"] != "disconnected"
    assert res["source_status_summary"].get("exit_code") == 0


def test_probe_zero_returncode_unrecognized_text_is_unknown(monkeypatch):
    """Clean exit with text that names neither explicit marker -> unknown."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=0, stdout="daemon: ok\nsome unrelated banner\n"),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "unknown"
    assert res["bridge_status"] != "disconnected"


def test_probe_zero_returncode_ambiguous_both_markers_is_unknown(monkeypatch):
    """Clean exit naming BOTH markers is contradictory -> unknown, not disconnected."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(
            returncode=0,
            stdout="extension: connected",
            stderr="extension: disconnected",
        ),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "unknown"
    assert res["bridge_status"] not in ("disconnected", "connected")


def test_probe_zero_returncode_unknown_detail_has_no_raw_output(monkeypatch):
    """Zero-exit unknown detail is a stable token, never raw stdout/stderr."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=0, stdout="SECRET_STDOUT banner", stderr="SECRET_STDERR"),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "unknown"
    assert "SECRET_STDOUT" not in res["detail"]
    assert "SECRET_STDERR" not in res["detail"]
    assert res["source_status_summary"].get("exit_code") == 0


def test_recovery_with_real_probe_zero_exit_empty_has_no_side_effect(monkeypatch):
    """Integration: a clean-exit-but-empty probe under user_confirmed=True must
    skip and touch nothing — no Chrome quit, no daemon restart, no re-probe."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=0, stdout="", stderr=""),
    )
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=_probe_opencli_bridge_status,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.attempted is False
    assert res.actions == []
    assert res.outcome == "skipped_because_safe"
    assert res.evidence.get("reason") == "status_not_disconnected"
    assert res.after == res.before


def test_recovery_with_real_probe_zero_exit_ambiguous_has_no_side_effect(monkeypatch):
    """Integration: a clean-exit-but-contradictory probe must also skip safely."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(
            returncode=0,
            stdout="extension: connected",
            stderr="extension: disconnected",
        ),
    )
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=_probe_opencli_bridge_status,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.attempted is False
    assert res.actions == []
    assert res.outcome == "skipped_because_safe"
    assert res.evidence.get("reason") == "status_not_disconnected"
    assert res.after == res.before


# ── production probe zero-exit: markers parsed per-stream, per-line, EXACTLY ─
#
# A clean exit still only trusts a line whose whitespace-stripped, case-folded
# content is EXACTLY "extension: connected" or "extension: disconnected". The
# two streams are parsed INDEPENDENTLY (never concatenated) and matched by
# whole line (never substring). This closes two spoofing gaps that the old
# ``(stdout + stderr).lower()`` substring parse left open:
#   * cross-stream stitching — stdout="extension:" + stderr=" disconnected"
#     must NOT become a disconnected marker;
#   * substring bleed — "not extension: disconnected" or
#     "extension: disconnected-ish" must NOT count as a marker.
# Duplicate markers or both kinds present remain contradictory -> unknown.


def test_probe_zero_exit_cross_stream_stitch_is_unknown(monkeypatch):
    """A marker split across stdout+stderr must never be stitched together."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=0, stdout="extension:", stderr=" disconnected"),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "unknown"
    assert res["bridge_status"] != "disconnected"


def test_probe_zero_exit_negated_marker_substring_is_unknown(monkeypatch):
    """'not extension: disconnected' contains the marker as a substring but is
    not an exact line -> unknown, never disconnected."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=0, stdout="not extension: disconnected"),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "unknown"
    assert res["bridge_status"] != "disconnected"


def test_probe_zero_exit_suffixed_marker_substring_is_unknown(monkeypatch):
    """'extension: disconnected-ish' has the marker as a prefix substring but is
    not an exact line -> unknown, never disconnected."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=0, stdout="extension: disconnected-ish"),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "unknown"
    assert res["bridge_status"] != "disconnected"


def test_probe_zero_exit_valid_marker_on_stderr_alone_is_disconnected(monkeypatch):
    """An exact disconnected marker on stderr (stdout empty) is a valid marker."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=0, stdout="", stderr="extension: disconnected"),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "disconnected"


def test_probe_zero_exit_valid_marker_among_banner_lines_is_disconnected(monkeypatch):
    """A valid marker on its own line among unrelated banner lines still parses."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(
            returncode=0,
            stdout="daemon: ok\n  Extension: Disconnected  \nbye\n",
        ),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "disconnected"


def test_probe_zero_exit_duplicate_disconnected_markers_is_unknown(monkeypatch):
    """Two disconnected markers is not a clean single signal -> unknown."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(
            returncode=0,
            stdout="extension: disconnected\nextension: disconnected",
        ),
    )
    res = _probe_opencli_bridge_status(timeout_sec=1.0)
    assert res["bridge_status"] == "unknown"
    assert res["bridge_status"] != "disconnected"


# Integration: every zero-exit "unknown" shape below must skip safely under
# user_confirmed=True — no Chrome quit, no daemon restart, no re-probe.
@pytest.mark.parametrize(
    "stdout,stderr",
    [
        ("extension:", " disconnected"),          # cross-stream stitch
        ("not extension: disconnected", ""),      # negated substring
        ("extension: disconnected-ish", ""),      # suffixed substring
        ("extension: disconnected\nextension: disconnected", ""),  # duplicate
    ],
)
def test_recovery_with_real_probe_zero_exit_ambiguous_marker_no_side_effect(
    monkeypatch, stdout, stderr
):
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _probe_run(returncode=0, stdout=stdout, stderr=stderr),
    )
    res = opencli_recover_once(
        user_confirmed=True,
        probe_status=_probe_opencli_bridge_status,
        run_chrome_quit=_never,
        run_opencli_daemon_restart=_never,
        count_open_tabs=_tabs(0),
        now=_clock(),
    )
    assert res.attempted is False
    assert res.actions == []
    assert res.outcome == "skipped_because_safe"
    assert res.evidence.get("reason") == "status_not_disconnected"
    assert res.after == res.before
