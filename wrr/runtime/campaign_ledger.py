"""EV-D6 Slice 1 — a SQLite-backed campaign admission ledger.

A campaign ledger records the admission lifecycle of one campaign, bound to a
single owning process epoch, in a single SQLite database. It exists so that a
campaign's routing admissions can be reconstructed and audited after the fact,
and so that an interrupted campaign is never silently accepted as complete.

Contract (WRR EV-D6 S1):

* stdlib ``sqlite3`` only; no third-party dependencies and no routing here.
* One campaign and one process epoch per database file. Opening a database that
  already carries a campaign is a *reopen*: inspect/export only. A reopened
  OPEN/dirty campaign can never be turned clean, and admission is refused.
* ``begin`` commits a monotonic ``seq`` and a unique ``token`` in its own short
  transaction. ``finish`` atomically persists the exact ``DecisionEvidenceV2``
  whitelist projection. ``drop`` abandons a pending admission.
* The database is WAL with ``synchronous=FULL``; every mutation is a short,
  explicit transaction.
* ``close`` reports ``clean`` only when nothing is left pending, dropped, or
  faulted; otherwise ``dirty``.
* Duplicate ``request_key``, duplicate/unknown ``token``, finish evidence
  whose ``request_key`` disagrees with the admitted one, and capacity
  exhaustion all fail closed and poison the campaign so it can no longer close
  clean. A refused admission (capacity) means the ledger is no longer a
  complete record of what the campaign was asked to route, so it too poisons.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Mapping, Optional

from ..schemas import DecisionEvidenceV2

SCHEMA_VERSION = 1

_STATUS_OPEN = "open"
_STATUS_CLEAN = "clean"
_STATUS_DIRTY = "dirty"

_STATE_PENDING = "pending"
_STATE_FINISHED = "finished"
_STATE_DROPPED = "dropped"

# Fixed, reconcilable fault causes recorded in ``fault_events``.
_FAULT_CAPACITY_EXHAUSTED = "capacity_exhausted"
_FAULT_DUPLICATE_REQUEST_KEY = "duplicate_request_key"
_FAULT_UNKNOWN_TOKEN = "unknown_token"
_FAULT_REQUEST_KEY_MISMATCH = "request_key_mismatch"
_EXTERNAL_FAULT_REASONS = frozenset({
    "identity_mint_failed",
    "admission_failed",
    "evidence_finish_failed",
    "admission_drop_failed",
    "downstream_evidence_failed",
    "context_changed",
    "owner_unloaded",
    "sampler_start_failed",
    "sampler_search_failed",
    "sampler_sleep_failed",
})


def _synchronized(method):
    """Serialize one shared ledger connection across request threads."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


class CampaignLedgerError(RuntimeError):
    """Base class for every ledger fail-closed condition."""


class CampaignClosed(CampaignLedgerError):
    """Raised when operating on a ledger whose ``close`` already ran."""


class CampaignReopened(CampaignLedgerError):
    """Raised when a write is attempted on an inspect/export-only reopen."""


class CampaignMismatch(CampaignLedgerError):
    """Raised when a database already belongs to a different fixed campaign."""

    def __init__(
        self,
        reason: str,
        message: str,
        diagnostic: Mapping[str, object],
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self._diagnostic = dict(diagnostic)

    @property
    def diagnostic(self) -> dict[str, object]:
        """A defensive copy suitable for structured operator diagnostics."""
        return dict(self._diagnostic)




class DuplicateRequestKey(CampaignLedgerError):
    """Raised when a request_key has already been admitted."""


class UnknownToken(CampaignLedgerError):
    """Raised when a token is unknown or no longer pending."""


class RequestKeyMismatch(CampaignLedgerError):
    """Raised when finish evidence names a different request_key than admitted."""


class CapacityExhausted(CampaignLedgerError):
    """Raised when the campaign's admission capacity is spent."""


class InvalidEvidence(CampaignLedgerError):
    """Raised when finish receives anything but exact DecisionEvidenceV2."""


@dataclass(frozen=True)
class CampaignDeclaration:
    """A campaign's canonical, pre-declared fixed policy.

    A declaration binds a campaign, at open time, to the policy it will run
    under: the ``policy_version`` it was admitted against, the exact set of
    routing ``requested_modes`` it is allowed to ask for, and the
    ``accepted_context_cohort_id`` whose decision context it consented to. It is
    frozen and canonical so that a reopen can reconstruct the identical object
    and reject any caller that tries to open the same database under a different
    policy.

    Only the *shape* is enforced here (non-empty, de-duplicated, string modes);
    whether a given mode names a real routing mode is left to the D5 evaluator,
    so this module never re-enumerates the mode set nor takes a new dependency.
    """

    policy_version: str
    requested_modes: tuple[str, ...]
    accepted_context_cohort_id: str
    build_manifest_id: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.policy_version) is not str or not self.policy_version:
            raise ValueError("policy_version must be a non-empty string")
        if (
            type(self.accepted_context_cohort_id) is not str
            or not self.accepted_context_cohort_id
        ):
            raise ValueError("accepted_context_cohort_id must be a non-empty string")
        if type(self.requested_modes) is not tuple or not self.requested_modes:
            raise ValueError("requested_modes must be a non-empty tuple")
        for mode in self.requested_modes:
            if type(mode) is not str or not mode:
                raise ValueError("requested_modes entries must be non-empty strings")
        if len(set(self.requested_modes)) != len(self.requested_modes):
            raise ValueError("requested_modes must not contain duplicates")
        if self.build_manifest_id is not None and (
            type(self.build_manifest_id) is not str
            or not self.build_manifest_id
            or len(self.build_manifest_id) > 128
        ):
            raise ValueError("build_manifest_id must be a bounded non-empty string")


def _declaration_to_json(declaration: CampaignDeclaration) -> str:
    obj = {
        "policy_version": declaration.policy_version,
        "requested_modes": list(declaration.requested_modes),
        "accepted_context_cohort_id": declaration.accepted_context_cohort_id,
    }
    if declaration.build_manifest_id is not None:
        obj["build_manifest_id"] = declaration.build_manifest_id
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
    )


def _declaration_from_json(raw: Optional[str]) -> Optional[CampaignDeclaration]:
    if raw is None:
        return None
    obj = json.loads(raw)
    return CampaignDeclaration(
        policy_version=obj["policy_version"],
        requested_modes=tuple(obj["requested_modes"]),
        accepted_context_cohort_id=obj["accepted_context_cohort_id"],
        build_manifest_id=obj.get("build_manifest_id"),
    )


@dataclass(frozen=True)
class Admission:
    """The receipt returned by :meth:`CampaignLedger.begin`."""

    seq: int
    token: str
    request_key: str


@dataclass(frozen=True)
class CampaignSnapshot:
    """A read-only projection of the ledger's current counters."""

    campaign_id: str
    epoch: str
    status: str
    capacity: Optional[int]
    pending: int
    finished: int
    dropped: int
    faults: int

    @property
    def total(self) -> int:
        return self.pending + self.finished + self.dropped


@dataclass(frozen=True)
class CampaignFacts:
    """A durable, deterministic projection of the whole campaign record.

    Every field is reconstructed purely from the committed SQLite tables — no
    process clock, environment, or in-memory counter is consulted — so two
    processes reading the same database observe identical facts. This is the
    audit-grade summary an owning session, or any later reopen, uses to decide
    whether the campaign is a complete and trustworthy record.
    """

    campaign_id: str
    epoch: str
    status: str
    capacity: Optional[int]
    declaration: Optional[CampaignDeclaration]
    opened_at: str
    closed_at: Optional[str]
    start_sequence: Optional[int]
    end_sequence: Optional[int]
    attempts_started: int
    terminal_count: int
    persisted_evidence_count: int
    dropped_count: int
    unresolved_count: int
    duplicate_count: int
    sequence_gaps: int
    fault_count: int
    session_closed_cleanly: bool
    request_keys: tuple[str, ...]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class CampaignLedger:
    """A SQLite admission ledger for exactly one campaign."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        campaign_id: str,
        epoch: str,
        capacity: Optional[int],
        declaration: Optional[CampaignDeclaration],
        reopened: bool,
    ) -> None:
        self._lock = threading.RLock()
        self._conn = conn
        self._campaign_id = campaign_id
        self._epoch = epoch
        self._capacity = capacity
        self._declaration = declaration
        self._reopened = reopened
        self._closed = False
        self._runtime_faulted = False
        self._final_status: Optional[str] = None

    # ── construction ────────────────────────────────────────────────────

    @classmethod
    def open(
        cls,
        path: os.PathLike[str] | str,
        *,
        campaign_id: str,
        capacity: Optional[int] = None,
        declaration: Optional[CampaignDeclaration] = None,
    ) -> "CampaignLedger":
        """Open (or create) the ledger at ``path`` for ``campaign_id``.

        A fresh database is stamped with this process's epoch and, when a
        ``declaration`` is supplied, its canonical fixed policy; it returns a
        writable ledger. A declared campaign is a *fixed* campaign and must
        bound its ``capacity`` to a positive int. A database that already
        carries a campaign returns a reopened, inspect/export-only ledger; a
        campaign_id mismatch, or an explicit ``declaration``/``capacity`` that
        disagrees with the persisted one, fails closed.
        """
        if type(campaign_id) is not str or not campaign_id:
            raise ValueError("campaign_id must be a non-empty string")
        if capacity is not None and (type(capacity) is not int or capacity <= 0):
            raise ValueError("capacity must be a positive int or None")
        if declaration is not None:
            if type(declaration) is not CampaignDeclaration:
                raise ValueError("declaration must be a CampaignDeclaration or None")
            if capacity is None:
                raise ValueError("a declared campaign must bound a positive capacity")

        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(target), isolation_level=None, check_same_thread=False,
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            cls._ensure_schema(conn)
            row = conn.execute(
                "SELECT campaign_id, epoch, capacity, declaration FROM meta "
                "WHERE id = 1"
            ).fetchone()
            if row is None:
                epoch = f"{os.getpid()}-{uuid.uuid4().hex}"
                declaration_json = (
                    _declaration_to_json(declaration)
                    if declaration is not None
                    else None
                )
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "INSERT INTO meta (id, campaign_id, epoch, schema_version, "
                        "capacity, declaration, status, fault_count, created_at) "
                        "VALUES (1, ?, ?, ?, ?, ?, ?, 0, ?)",
                        (
                            campaign_id,
                            epoch,
                            SCHEMA_VERSION,
                            capacity,
                            declaration_json,
                            _STATUS_OPEN,
                            _now(),
                        ),
                    )
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                return cls(
                    conn,
                    campaign_id=campaign_id,
                    epoch=epoch,
                    capacity=capacity,
                    declaration=declaration,
                    reopened=False,
                )
            stored_campaign, stored_epoch, stored_capacity, stored_declaration_json = row
            if stored_campaign != campaign_id:
                raise CampaignMismatch(
                    "campaign_id_mismatch",
                    "database already belongs to a different campaign",
                    {
                        "ledger_path": str(target),
                        "stored_campaign_id": stored_campaign,
                        "requested_campaign_id": campaign_id,
                        "remediation": (
                            "Preserve the existing ledger and select a different "
                            "campaign id with an unused ledger path."
                        ),
                    },
                )
            stored_declaration = _declaration_from_json(stored_declaration_json)
            # A reopen reconstructs the canonical declaration/capacity. If the
            # caller *explicitly* re-declares a different policy or capacity, the
            # database no longer belongs to the campaign they think they own, so
            # fail closed rather than silently ignore the disagreement.
            if declaration is not None and declaration != stored_declaration:
                raise CampaignMismatch(
                    "declaration_mismatch",
                    "database already belongs to a different declaration",
                    {
                        "ledger_path": str(target),
                        "stored_campaign_id": stored_campaign,
                        "requested_campaign_id": campaign_id,
                        "stored_declaration": json.loads(stored_declaration_json)
                        if stored_declaration_json is not None
                        else None,
                        "requested_declaration": json.loads(_declaration_to_json(declaration)),
                        "remediation": (
                            "Preserve the existing ledger. An equivalent restart must "
                            "reconstruct its declaration; a changed semantic context or "
                            "policy cannot resume this fixed campaign."
                        ),
                    },
                )
            if capacity is not None and capacity != stored_capacity:
                raise CampaignMismatch(
                    "capacity_mismatch",
                    "database already belongs to a different capacity",
                    {
                        "ledger_path": str(target),
                        "stored_campaign_id": stored_campaign,
                        "requested_campaign_id": campaign_id,
                        "stored_capacity": stored_capacity,
                        "requested_capacity": capacity,
                        "remediation": (
                            "Preserve the existing ledger and select a matching capacity "
                            "or an unused campaign id and ledger path."
                        ),
                    },
                )
            return cls(
                conn,
                campaign_id=stored_campaign,
                epoch=stored_epoch,
                capacity=stored_capacity,
                declaration=stored_declaration,
                reopened=True,
            )
        except BaseException:
            conn.close()
            raise

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                "  id INTEGER PRIMARY KEY CHECK (id = 1),"
                "  campaign_id TEXT NOT NULL,"
                "  epoch TEXT NOT NULL,"
                "  schema_version INTEGER NOT NULL,"
                "  capacity INTEGER,"
                "  declaration TEXT,"
                "  status TEXT NOT NULL,"
                "  fault_count INTEGER NOT NULL DEFAULT 0,"
                "  created_at TEXT NOT NULL,"
                "  closed_at TEXT"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS admissions ("
                "  seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  token TEXT NOT NULL UNIQUE,"
                "  request_key TEXT NOT NULL UNIQUE,"
                "  state TEXT NOT NULL,"
                "  evidence TEXT"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fault_events ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  reason TEXT NOT NULL"
                ")"
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    # ── properties ──────────────────────────────────────────────────────

    @property
    def campaign_id(self) -> str:
        return self._campaign_id

    @property
    def epoch(self) -> str:
        return self._epoch

    @property
    def capacity(self) -> Optional[int]:
        return self._capacity

    @property
    def declaration(self) -> Optional[CampaignDeclaration]:
        return self._declaration

    @property
    def reopened(self) -> bool:
        return self._reopened

    @property
    def closed(self) -> bool:
        return self._closed

    def terminal_guard(self):
        """Serialize finish plus downstream persistence against close()."""
        return self._lock

    @_synchronized
    def sqlite_pragmas(self) -> dict[str, Any]:
        """Return the live connection's durability-relevant PRAGMA state."""
        self._require_live()
        journal = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = self._conn.execute("PRAGMA synchronous").fetchone()[0]
        return {"journal_mode": journal, "synchronous": synchronous}

    # ── admission mutations ─────────────────────────────────────────────

    @_synchronized
    def record_fault(self, reason: str) -> None:
        """Durably poison a live campaign for a composition-layer fault."""
        self._require_writable()
        if reason not in _EXTERNAL_FAULT_REASONS:
            raise ValueError("unsupported external campaign fault reason")
        self._runtime_faulted = True
        self._record_fault(reason)

    @_synchronized
    def begin(self, request_key: str) -> Admission:
        """Admit ``request_key``, committing a monotonic seq and unique token."""
        self._require_writable()
        if type(request_key) is not str or not request_key:
            raise ValueError("request_key must be a non-empty string")

        if self._capacity is not None:
            count = self._conn.execute("SELECT COUNT(*) FROM admissions").fetchone()[0]
            if count >= self._capacity:
                # A refused admission means this campaign can no longer be a
                # complete record of what it was asked to route, so capacity
                # exhaustion poisons it and it can never close clean.
                self._record_fault(_FAULT_CAPACITY_EXHAUSTED)
                raise CapacityExhausted("campaign admission capacity is exhausted")

        token = uuid.uuid4().hex
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                "INSERT INTO admissions (token, request_key, state, evidence) "
                "VALUES (?, ?, ?, NULL)",
                (token, request_key, _STATE_PENDING),
            )
            seq = int(cur.lastrowid)
            self._conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            self._conn.execute("ROLLBACK")
            self._record_fault(_FAULT_DUPLICATE_REQUEST_KEY)
            raise DuplicateRequestKey(
                "request_key has already been admitted"
            ) from None
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return Admission(seq=seq, token=token, request_key=request_key)

    @_synchronized
    def begin_or_fault(self, request_key: str) -> Admission:
        """Admit, atomically poisoning the campaign before any failure escapes."""
        try:
            return self.begin(request_key)
        except (
            CampaignClosed,
            CampaignReopened,
            CapacityExhausted,
            DuplicateRequestKey,
        ):
            raise
        except BaseException:
            self._runtime_faulted = True
            try:
                self._record_fault("admission_failed")
            except BaseException:
                pass
            raise

    @_synchronized
    def finish(self, token: str, evidence: DecisionEvidenceV2) -> None:
        """Atomically persist the exact v2 whitelist for a pending ``token``."""
        self._require_writable()
        # Exact-type boundary: reject v1, duck types, and subclasses that could
        # override to_dict to smuggle unapproved fields into the record.
        if type(evidence) is not DecisionEvidenceV2:
            raise InvalidEvidence("finish requires an exact DecisionEvidenceV2")
        try:
            payload = json.dumps(
                DecisionEvidenceV2.to_dict(evidence),
                ensure_ascii=False,
                sort_keys=True,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as fail-closed
            raise InvalidEvidence("evidence failed whitelist serialization") from exc

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT state, request_key FROM admissions WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None or row[0] != _STATE_PENDING:
                self._conn.execute("ROLLBACK")
                self._record_fault(_FAULT_UNKNOWN_TOKEN)
                raise UnknownToken("token is unknown or no longer pending")
            # The evidence must describe the request this token actually
            # admitted; a mismatch would mislabel the persisted record, so it is
            # an integrity fault that poisons the campaign.
            if row[1] != evidence.request_key:
                self._conn.execute("ROLLBACK")
                self._record_fault(_FAULT_REQUEST_KEY_MISMATCH)
                raise RequestKeyMismatch(
                    "finish evidence request_key does not match the admitted "
                    "request_key"
                )
            self._conn.execute(
                "UPDATE admissions SET state = ?, evidence = ? WHERE token = ?",
                (_STATE_FINISHED, payload, token),
            )
            self._conn.execute("COMMIT")
        except (UnknownToken, RequestKeyMismatch):
            raise
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    @_synchronized
    def drop(self, token: str) -> None:
        """Abandon a pending admission, leaving the campaign non-clean."""
        self._require_writable()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT state FROM admissions WHERE token = ?", (token,)
            ).fetchone()
            if row is None or row[0] != _STATE_PENDING:
                self._conn.execute("ROLLBACK")
                self._record_fault(_FAULT_UNKNOWN_TOKEN)
                raise UnknownToken("token is unknown or no longer pending")
            self._conn.execute(
                "UPDATE admissions SET state = ? WHERE token = ?",
                (_STATE_DROPPED, token),
            )
            self._conn.execute("COMMIT")
        except UnknownToken:
            raise
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    # ── read paths ──────────────────────────────────────────────────────

    @_synchronized
    def inspect(self) -> CampaignSnapshot:
        """Return the current campaign counters."""
        self._require_live()
        campaign_id, epoch, status, capacity, faults = self._conn.execute(
            "SELECT campaign_id, epoch, status, capacity, fault_count "
            "FROM meta WHERE id = 1"
        ).fetchone()
        counts = {_STATE_PENDING: 0, _STATE_FINISHED: 0, _STATE_DROPPED: 0}
        for state, n in self._conn.execute(
            "SELECT state, COUNT(*) FROM admissions GROUP BY state"
        ):
            counts[state] = n
        return CampaignSnapshot(
            campaign_id=campaign_id,
            epoch=epoch,
            status=status,
            capacity=capacity,
            pending=counts[_STATE_PENDING],
            finished=counts[_STATE_FINISHED],
            dropped=counts[_STATE_DROPPED],
            faults=faults,
        )

    @_synchronized
    def export(self) -> list[dict[str, Any]]:
        """Return the finished admissions with their persisted v2 whitelist."""
        self._require_live()
        rows = self._conn.execute(
            "SELECT seq, request_key, evidence FROM admissions "
            "WHERE state = ? ORDER BY seq",
            (_STATE_FINISHED,),
        ).fetchall()
        return [
            {
                "seq": seq,
                "request_key": request_key,
                "evidence": json.loads(evidence),
            }
            for seq, request_key, evidence in rows
        ]

    @_synchronized
    def facts(self) -> CampaignFacts:
        """Reconstruct the durable :class:`CampaignFacts` from committed rows.

        Reads only SQLite (no clock, env, or in-memory counter), so the result
        is deterministic and identical across processes and repeated calls.
        """
        self._require_live()
        (
            campaign_id,
            epoch,
            status,
            capacity,
            fault_count,
            declaration_json,
            opened_at,
            closed_at,
        ) = self._conn.execute(
            "SELECT campaign_id, epoch, status, capacity, fault_count, "
            "declaration, created_at, closed_at FROM meta WHERE id = 1"
        ).fetchone()
        declaration = _declaration_from_json(declaration_json)
        rows = self._conn.execute(
            "SELECT seq, request_key, state FROM admissions ORDER BY seq"
        ).fetchall()

        attempts_started = len(rows)
        request_keys = tuple(request_key for _seq, request_key, _state in rows)
        persisted_evidence_count = sum(
            1 for _seq, _rk, state in rows if state == _STATE_FINISHED
        )
        dropped_count = sum(
            1 for _seq, _rk, state in rows if state == _STATE_DROPPED
        )
        unresolved_count = sum(
            1 for _seq, _rk, state in rows if state == _STATE_PENDING
        )

        if rows:
            start_sequence: Optional[int] = rows[0][0]
            end_sequence: Optional[int] = rows[-1][0]
            span = end_sequence - start_sequence + 1
            # A missing seq between the first and last surviving admission means a
            # committed record vanished — corruption the campaign cannot ignore.
            sequence_gaps = span - attempts_started
        else:
            start_sequence = None
            end_sequence = None
            sequence_gaps = 0

        duplicate_count = self._conn.execute(
            "SELECT COUNT(*) FROM fault_events WHERE reason = ?",
            (_FAULT_DUPLICATE_REQUEST_KEY,),
        ).fetchone()[0]

        return CampaignFacts(
            campaign_id=campaign_id,
            epoch=epoch,
            status=status,
            capacity=capacity,
            declaration=declaration,
            opened_at=opened_at,
            closed_at=closed_at,
            start_sequence=start_sequence,
            end_sequence=end_sequence,
            attempts_started=attempts_started,
            terminal_count=persisted_evidence_count + dropped_count,
            persisted_evidence_count=persisted_evidence_count,
            dropped_count=dropped_count,
            unresolved_count=unresolved_count,
            duplicate_count=duplicate_count,
            sequence_gaps=sequence_gaps,
            fault_count=fault_count,
            session_closed_cleanly=(status == _STATUS_CLEAN),
            request_keys=request_keys,
        )

    # ── teardown ────────────────────────────────────────────────────────

    @_synchronized
    def close(self) -> str:
        """Finalize the campaign and return its terminal ``clean``/``dirty``.

        A reopened ledger never mutates the database and never reports clean.
        """
        if self._closed:
            return self._final_status or _STATUS_DIRTY

        if self._reopened:
            stored = self._conn.execute(
                "SELECT status FROM meta WHERE id = 1"
            ).fetchone()[0]
            status = _STATUS_CLEAN if stored == _STATUS_CLEAN else _STATUS_DIRTY
        else:
            facts = self.facts()
            # A clean close still demands nothing pending, dropped, or faulted;
            # additionally, a sequence gap means a committed admission is missing,
            # so the record is incomplete and cannot be certified clean either.
            clean = (
                facts.unresolved_count == 0
                and facts.dropped_count == 0
                and facts.fault_count == 0
                and facts.sequence_gaps == 0
                and not self._runtime_faulted
            )
            status = _STATUS_CLEAN if clean else _STATUS_DIRTY
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # The owner's terminal verdict and the moment it was sealed are
                # written in one transaction, so a durable close can never carry
                # a status without its closed_at (or vice versa).
                self._conn.execute(
                    "UPDATE meta SET status = ?, closed_at = ? WHERE id = 1",
                    (status, _now()),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

        self._final_status = status
        self._closed = True
        self._conn.close()
        self._conn = None  # type: ignore[assignment]
        return status

    def __enter__(self) -> "CampaignLedger":
        return self

    def __exit__(self, *_exc: object) -> None:
        if not self._closed:
            self.close()

    # ── internals ───────────────────────────────────────────────────────

    def _record_fault(self, reason: str) -> None:
        """Persistently poison the campaign after an integrity anomaly.

        The ``reason`` is appended to ``fault_events`` so the durable fault tally
        can be reconciled by cause (e.g. ``duplicate_count`` is the exact number
        of ``duplicate_request_key`` rows), and ``meta.fault_count`` is bumped in
        the same transaction so the two can never drift.
        """
        self._runtime_faulted = True
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "UPDATE meta SET fault_count = fault_count + 1, "
                "status = CASE WHEN status = ? THEN ? ELSE status END "
                "WHERE id = 1",
                (_STATUS_OPEN, _STATUS_DIRTY),
            )
            self._conn.execute(
                "INSERT INTO fault_events (reason) VALUES (?)", (reason,)
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _require_live(self) -> None:
        if self._closed:
            raise CampaignClosed("campaign ledger is closed")

    def _require_writable(self) -> None:
        self._require_live()
        if self._reopened:
            raise CampaignReopened(
                "reopened campaign is inspect/export only"
            )
