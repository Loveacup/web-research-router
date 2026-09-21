"""Cached DecisionContext provider for the WRR v6 control plane.

This is a lazy, refresh-driven cache in front of a ``DecisionContext`` builder.
It exists so descriptor-selection consumers can read an immutable snapshot without
each read paying the cost of rebuilding it, and without the provider itself doing
any discovery, probing, bridging, routing, or I/O.

Contract:

* Construction never invokes the builder.
* ``get()`` never invokes the builder; it returns the last published snapshot and
  never filters by TTL. Before the first successful refresh it returns ``None``.
* ``observe()`` returns one immutable atomic ``(context, status, cohort_id)``
  view. Cohort IDs are internally derived canonical UUIDv4 values from routing
  semantics and cannot be supplied by callers.
* ``refresh()`` invokes the builder, publishes the result atomically on success, and
  returns it. Refreshes are serialized (no single-flight merge). A failed build or
  cohort derivation propagates, records ``refresh_failed``, and retains the last-good
  snapshot/cohort pair. Equivalent rebuilt contexts retain their cohort; a later
  semantic change publishes a new pair and clears failure.
* A read is never blocked by an in-flight refresh: the builder runs while holding
  only the refresh lock; snapshot reads and the publish write are guarded by a
  separate, always-brief state lock. Correctness does not rely on the GIL.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Callable, NamedTuple
import uuid

from wrr.schemas import DecisionContext


class DecisionContextObservation(NamedTuple):
    """One immutable, bounded read of the provider's published state."""

    context: DecisionContext | None
    status: str
    cohort_id: str | None


def _semantic_cohort_id(context: DecisionContext) -> str:
    """Return a stable UUIDv4 identity for the routing semantics in ``context``."""
    payload = {
        "runtime": context.runtime,
        "profile": context.profile,
        "registry_source": context.registry_source,
        "routable_descriptor_ids": context.routable_descriptor_ids,
        "bridged_provider_ids": context.bridged_provider_ids,
        "missing_provider_ids": context.missing_provider_ids,
        "adapter_errors": context.adapter_errors,
        "descriptor_reasons": context.descriptor_reasons,
        "descriptor_provider_aliases": context.descriptor_provider_aliases,
        "config_fingerprint": context.config_fingerprint,
    }
    digest = bytearray(
        hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


class CachedDecisionContextProvider:
    """Serialize refreshes of a DecisionContext builder; serve the last good snapshot."""

    def __init__(self, builder: Callable[[], DecisionContext]) -> None:
        if not callable(builder):
            raise TypeError("builder must be callable")
        self._builder = builder
        # Serializes refresh() calls so the builder never runs concurrently. Held
        # for the entire (potentially slow) build; get() never takes this lock.
        self._refresh_lock = threading.Lock()
        # Guards the snapshot reference only. Every acquisition is a single
        # reference read or assignment, so it is held for a bounded, tiny window
        # and a slow builder — which runs outside this lock — cannot block readers.
        self._state_lock = threading.Lock()
        # One state-lock-owned tuple. None snapshot/cohort + cold means no
        # successful refresh yet; failure updates status without tearing apart a
        # retained last-good snapshot/cohort pair.
        self._snapshot: DecisionContext | None = None
        self._status = "cold"
        self._cohort_id: str | None = None

    def get(self) -> DecisionContext | None:
        """Return the last published snapshot, or None before the first refresh.

        Never rebuilds and never filters by TTL. Acquires only the brief state
        lock, so an in-flight refresh cannot block this read.
        """
        with self._state_lock:
            return self._snapshot

    def observe(self) -> DecisionContextObservation:
        """Return one atomic immutable view without invoking the builder."""
        with self._state_lock:
            return DecisionContextObservation(
                self._snapshot,
                self._status,
                self._cohort_id,
            )

    def refresh(self) -> DecisionContext:
        """Rebuild via the builder and publish atomically; retain last-good on failure."""
        with self._refresh_lock:
            # The builder runs while holding only the refresh lock; get() takes the
            # separate state lock, so reads stay non-blocking during a slow rebuild.
            try:
                context = self._builder()
                if not isinstance(context, DecisionContext):
                    raise TypeError(
                        "builder must return a DecisionContext, got "
                        f"{type(context).__name__}"
                    )
                cohort_id = _semantic_cohort_id(context)
            except Exception:
                with self._state_lock:
                    self._status = "refresh_failed"
                raise
            # Publish under the brief state lock: a failed build above never reaches
            # here, so the previously published snapshot is retained.
            with self._state_lock:
                self._snapshot = context
                self._status = "available"
                self._cohort_id = cohort_id
            return context
