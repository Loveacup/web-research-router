"""Opt-in production activation for one fixed WRR evidence campaign."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .campaign_ledger import Admission, CampaignLedger


_CAMPAIGN_KEYS = frozenset({
    "enabled", "id", "mode", "capacity", "policy_version",
    "build_manifest_id", "ledger_path", "context_ttl_sec",
})
_CAMPAIGN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_BUILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MIN_OBSERVATION_SECONDS = 86400.0


@dataclass(frozen=True)
class CampaignActivationConfig:
    campaign_id: str
    mode: str
    capacity: int
    policy_version: str
    build_manifest_id: str
    ledger_path: Path
    context_ttl_sec: float


def parse_campaign_config(raw: Any):
    """Return a validated activation config, or ``None`` when not explicitly enabled."""
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    unknown = set(raw) - _CAMPAIGN_KEYS
    if unknown:
        raise ValueError("campaign config contains unknown fields")
    campaign_id = raw.get("id")
    mode = raw.get("mode")
    capacity = raw.get("capacity")
    policy_version = raw.get("policy_version")
    build_manifest_id = raw.get("build_manifest_id")
    ledger_raw = raw.get("ledger_path")
    ttl = raw.get("context_ttl_sec")
    if type(campaign_id) is not str or not _CAMPAIGN_ID_RE.fullmatch(campaign_id):
        raise ValueError("campaign id must be a bounded lowercase token")
    if mode != "grounding":
        raise ValueError("D7-S4 supports exactly the grounding mode")
    if type(capacity) is not int or capacity < 50:
        raise ValueError("campaign capacity must be an int >= 50")
    if policy_version != "EV-D5-v1":
        raise ValueError("unsupported campaign policy_version")
    if type(build_manifest_id) is not str or not _BUILD_ID_RE.fullmatch(build_manifest_id):
        raise ValueError("build_manifest_id must be a bounded build token")
    if type(ledger_raw) is not str:
        raise ValueError("ledger_path must be an absolute path string")
    ledger_path = Path(ledger_raw).expanduser()
    if not ledger_path.is_absolute():
        raise ValueError("ledger_path must be absolute")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 86400:
        raise ValueError("context_ttl_sec must exceed the 24h observation window")
    return CampaignActivationConfig(
        campaign_id=campaign_id,
        mode=mode,
        capacity=capacity,
        policy_version=policy_version,
        build_manifest_id=build_manifest_id,
        ledger_path=ledger_path,
        context_ttl_sec=float(ttl),
    )


_STATE_DISABLED = "disabled"
_STATE_ACTIVE = "active"
_STATE_DRAINING = "draining"
_STATE_CLOSED = "closed"


class CampaignController:
    """Own the exact one-campaign admission lifecycle for one Gateway process."""

    def __init__(
        self,
        config: CampaignActivationConfig,
        ledger: CampaignLedger,
        cohort_id: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        minimum_observation_seconds: float = _MIN_OBSERVATION_SECONDS,
    ) -> None:
        self._config = config
        self._ledger = ledger
        self._cohort_id = cohort_id
        self._clock = clock
        self._minimum_observation_seconds = minimum_observation_seconds
        self._lock = threading.RLock()
        # A reopened ledger is inspect/export-only. A restart therefore disables
        # this campaign rather than silently creating a second process epoch.
        self._state = _STATE_DISABLED if ledger.reopened else _STATE_ACTIVE
        self._in_flight = 0
        self._started_at: float | None = None
        self._final_status: str | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def terminal_guard(self):
        """Serialize terminal persistence against a concurrent clean close."""
        return self._lock

    def finish(self, token: str, evidence) -> None:
        self._ledger.finish(token, evidence)

    def drop(self, token: str) -> None:
        self._ledger.drop(token)

    def record_fault(self, reason: str) -> None:
        with self._lock:
            try:
                self._ledger.record_fault(reason)
            finally:
                self._state = _STATE_DISABLED

    def facts(self):
        return self._ledger.facts()

    def admit(
        self,
        request_key: str,
        *,
        campaign_id: str | None,
        mode: str | None,
        provider: str | None,
        context_status: str | None = None,
        context_cohort_id: str | None = None,
        context_expires_at: float | None = None,
        context_now: float | None = None,
    ) -> Admission | None:
        """Mint a receipt only for a matching request under the frozen context."""
        with self._lock:
            if self._state != _STATE_ACTIVE:
                return None
            if campaign_id != self._config.campaign_id:
                return None
            if mode != self._config.mode or provider is not None:
                return None
            if not self._context_matches(context_status, context_cohort_id):
                self._disable_for_context_change()
                return None
            if self._started_at is None and context_expires_at is not None:
                if context_now is None or (
                    context_expires_at - context_now
                    < self._minimum_observation_seconds
                ):
                    self._disable_for_context_change()
                    return None
            try:
                if self._ledger.inspect().total >= self._config.capacity:
                    return None
            except Exception:
                self._disable_after_storage_fault()
                return None
            try:
                admission = self._ledger.begin_or_fault(request_key)
            except Exception:
                # begin_or_fault already records the canonical durable fault.
                self._state = _STATE_DISABLED
                return None
            try:
                total = self._ledger.inspect().total
            except Exception:
                self._disable_after_storage_fault()
                return None
            if self._started_at is None:
                self._started_at = self._clock()
            self._in_flight += 1
            if total >= self._config.capacity:
                self._state = _STATE_DRAINING
            return admission

    def tick(
        self,
        *,
        context_status: str | None,
        context_cohort_id: str | None,
    ) -> None:
        """Revalidate the fixed cohort and close once both D7-S4 bounds hold."""
        with self._lock:
            if self._state in {_STATE_DISABLED, _STATE_CLOSED}:
                return
            if not self._context_matches(context_status, context_cohort_id):
                self._disable_for_context_change()
                return
            try:
                self._maybe_close()
            except Exception:
                self._state = _STATE_DISABLED

    def on_terminal(self) -> None:
        """Complete one in-flight request without leaking campaign faults to search."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            try:
                self._maybe_close()
            except Exception:
                self._state = _STATE_DISABLED

    def _context_matches(
        self,
        context_status: str | None,
        context_cohort_id: str | None,
    ) -> bool:
        if context_status is None and context_cohort_id is None:
            return True
        return context_status == "available" and context_cohort_id == self._cohort_id

    def _disable_for_context_change(self) -> None:
        try:
            self._ledger.record_fault("context_changed")
        except Exception:
            pass
        self._state = _STATE_DISABLED

    def _disable_after_storage_fault(self) -> None:
        try:
            self._ledger.record_fault("admission_failed")
        except Exception:
            pass
        self._state = _STATE_DISABLED

    def _observation_complete(self) -> bool:
        return (
            self._started_at is not None
            and self._clock() - self._started_at >= self._minimum_observation_seconds
        )

    def _maybe_close(self) -> None:
        if self._state != _STATE_DRAINING or self._in_flight != 0:
            return
        if not self._observation_complete():
            return
        if self._ledger.inspect().pending == 0:
            self.close()

    def inspect(self):
        return self._ledger.inspect()

    def shutdown(self) -> str:
        """Dirty-close an incomplete owner session and release its SQLite handle."""
        with self._lock:
            if self._state == _STATE_CLOSED:
                return self._final_status or "dirty"
            if self._state in {_STATE_ACTIVE, _STATE_DRAINING}:
                try:
                    self._ledger.record_fault("owner_unloaded")
                finally:
                    self._state = _STATE_DISABLED
            try:
                status = self._ledger.close()
            except Exception:
                self._state = _STATE_DISABLED
                raise
            self._final_status = status
            self._state = _STATE_CLOSED
            return status

    def close(self) -> str:
        with self._lock:
            if self._state == _STATE_CLOSED:
                return self._final_status or "dirty"
            if self._state != _STATE_DRAINING or self._in_flight != 0:
                return "open"
            if not self._observation_complete():
                return "open"
            try:
                snapshot = self._ledger.inspect()
                if (
                    snapshot.total != self._config.capacity
                    or snapshot.pending != 0
                ):
                    return "open"
                status = self._ledger.close()
            except Exception:
                self._state = _STATE_DISABLED
                return "dirty"
            self._final_status = status
            self._state = _STATE_CLOSED
            return status
