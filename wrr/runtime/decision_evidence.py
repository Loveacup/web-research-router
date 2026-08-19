"""Stage-S decision-evidence sinks (WRR P1 3b.1).

Best-effort, privacy-bounded JSONL persistence of a routing decision's
whitelisted projection. This module performs no routing, no search, and no
network I/O. Records are append-only and every write is fault-swallowing: a
sink failure must never disturb the routing path.

Only the explicit whitelist below is ever serialized. Nothing carries the
user's request text, digests of it, result excerpts, response payloads,
exception text, headers, tokens, or credentials.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Mapping, Optional, Protocol, runtime_checkable

from ..schemas import DecisionEvidence, DecisionEvidenceV2
from .detect import RuntimeInfo, detect_runtime


_ENV_OVERRIDE = "WRR_DECISION_EVIDENCE_PATH"
_FILE_NAME = "decision-evidence.jsonl"
_FILE_MODE = 0o600

# One in-process lock per canonical path serializes threads; fcntl.flock adds
# cross-process advisory coordination on top of it.
_PATH_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()


@runtime_checkable
class DecisionEvidenceSink(Protocol):
    """Accepts one immutable decision evidence record; must never raise."""

    def record(self, evidence: DecisionEvidence | DecisionEvidenceV2) -> None: ...


class NoopDecisionEvidenceSink:
    """Discards every record. The safe default when persistence is disabled."""

    def record(self, evidence: DecisionEvidence | DecisionEvidenceV2) -> None:  # noqa: D401 - trivial
        return None


def decision_evidence_path(
    runtime: Optional[RuntimeInfo] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Resolve the JSONL path.

    Precedence: WRR_DECISION_EVIDENCE_PATH override > the last of the runtime's
    data_roots. When no runtime is supplied, the current runtime is detected.
    """
    environ = os.environ if env is None else env
    override = environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()
    resolved = runtime if runtime is not None else detect_runtime()
    return resolved.data_roots[-1] / _FILE_NAME


def _lock_for(canonical: str) -> threading.Lock:
    with _REGISTRY_LOCK:
        lock = _PATH_LOCKS.get(canonical)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[canonical] = lock
        return lock


def _whitelist_record(evidence: DecisionEvidence | DecisionEvidenceV2) -> dict:
    """Build the explicit whitelist projection to serialize."""
    # Exact-type boundary: reject duck-typed objects and subclasses that could
    # override ``to_dict`` to smuggle unapproved fields into the JSONL record.
    if type(evidence) is DecisionEvidence:
        return DecisionEvidence.to_dict(evidence)
    if type(evidence) is DecisionEvidenceV2:
        # Re-run the exact class serializer: it revalidates every field and emits
        # a fixed whitelist without invoking nested polymorphic methods.
        return DecisionEvidenceV2.to_dict(evidence)
    raise TypeError("decision evidence sink accepts exact v1/v2 evidence only")


class JsonlDecisionEvidenceSink:
    """Append one UTF-8 JSON line per record. Best-effort and never raises."""

    def __init__(
        self,
        path: Optional[os.PathLike | str] = None,
        *,
        runtime: Optional[RuntimeInfo] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        if path is not None:
            self._path = Path(path).expanduser()
        else:
            self._path = decision_evidence_path(runtime=runtime, env=env)

    @property
    def path(self) -> Path:
        return self._path

    def record(self, evidence: DecisionEvidence | DecisionEvidenceV2) -> None:
        try:
            line = json.dumps(
                _whitelist_record(evidence), ensure_ascii=False, sort_keys=True
            )
            payload = (line + "\n").encode("utf-8")
        except Exception:
            return
        try:
            self._append(payload)
        except Exception:
            return

    def _append(self, payload: bytes) -> None:
        canonical = os.path.abspath(str(self._path))
        with _lock_for(canonical):
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                canonical, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _FILE_MODE
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    view = memoryview(payload)
                    while view:
                        written = os.write(fd, view)
                        # A zero-length write signals no forward progress; loop
                        # instead of hanging forever. record() swallows this.
                        if written <= 0:
                            raise OSError("decision-evidence append made no progress")
                        view = view[written:]
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
