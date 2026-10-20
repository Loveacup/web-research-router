"""Core data contracts (spec §4.1, §4.3, §4.8).

Plain dataclasses with explicit `to_dict` so every host sees the same JSON.
Invariants are checked by `validate_evidence`, not assumed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Transport(str, Enum):
    API = "api"
    CLI = "cli"
    MCP = "mcp"
    SKILL = "skill"
    HOST = "host"
    REMOTE = "remote"


class Kind(str, Enum):
    WEB = "web"
    ACADEMIC = "academic"
    CODE = "code"
    COMMUNITY = "community"
    LOCAL = "local"
    HOST = "host"


class PrivacyClass(str, Enum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    LOCAL = "local"


class Provision(str, Enum):
    INSTALLED = "installed"
    CONFIGURED = "configured"
    AUTHORIZED = "authorized"
    DISABLED = "disabled"


class Health(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    COOLDOWN = "cooldown"


class RunStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    AUTH_ERROR = "auth_error"
    RATE_LIMIT = "rate_limit"
    SCHEMA_ERROR = "schema_error"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class SourceProfile:
    """What Jev reads to decide `fit`. JSON, not prose (spec §4.1)."""
    covers: tuple[str, ...] = ()
    best_for: tuple[str, ...] = ()
    weak_for: tuple[str, ...] = ()
    freshness: str = "unknown"      # realtime | daily | indexed | archive | unknown
    languages: tuple[str, ...] = ()
    auth: str = "none"              # none | api_key | login | local

    def to_dict(self) -> dict[str, Any]:
        return {
            "covers": list(self.covers), "best_for": list(self.best_for),
            "weak_for": list(self.weak_for), "freshness": self.freshness,
            "languages": list(self.languages), "auth": self.auth,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceProfile":
        return cls(
            covers=tuple(data.get("covers", ())), best_for=tuple(data.get("best_for", ())),
            weak_for=tuple(data.get("weak_for", ())), freshness=str(data.get("freshness", "unknown")),
            languages=tuple(data.get("languages", ())), auth=str(data.get("auth", "none")),
        )


@dataclass(frozen=True)
class Capabilities:
    search: bool = True
    fetch: bool = False
    similar: bool = False
    returns_answer: bool = False
    returns_citations: bool = False
    fresh: bool = False             # can serve recent material (used by need_fresher)

    def to_dict(self) -> dict[str, Any]:
        return {"search": self.search, "fetch": self.fetch, "similar": self.similar,
                "returns_answer": self.returns_answer, "returns_citations": self.returns_citations,
                "fresh": self.fresh}


@dataclass(frozen=True)
class SourceDescriptor:
    id: str
    kind: Kind
    transport: Transport
    upstream_family: str
    privacy_class: PrivacyClass
    profile: SourceProfile = field(default_factory=SourceProfile)
    capabilities: Capabilities = field(default_factory=Capabilities)
    provision: Provision = Provision.INSTALLED
    health: Health = Health.UNKNOWN
    primary: bool = False           # forced into every plan when routable (Exa, DeepSeek)
    device: Optional[str] = None    # remote peer id
    remote_transport: Optional[Transport] = None
    failure_category: Optional[str] = None
    light_probe_ok: Optional[bool] = None
    source_block: Optional[str] = None  # present | missing (legacy manifest)
    query_style: str = "natural"
    # Trusted local execution references; never exposed to hosts/peers or judges.
    adapter: Optional[str] = field(default=None, repr=False, compare=False)
    declarative: Any = field(default=None, repr=False, compare=False)
    configured_source: Optional[dict[str, Any]] = field(default=None, repr=False, compare=False)

    @property
    def executable(self) -> bool:
        """Whether trusted local execution references are available."""
        return bool(self.adapter) or self.declarative is not None or self.configured_source is not None

    @property
    def routable(self) -> bool:
        if self.provision != Provision.AUTHORIZED:
            return False
        if self.health in (Health.HEALTHY, Health.DEGRADED):
            return True
        return self.health == Health.UNKNOWN and self.light_probe_ok is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind.value, "transport": self.transport.value,
            "upstream_family": self.upstream_family, "privacy_class": self.privacy_class.value,
            "profile": self.profile.to_dict(), "capabilities": self.capabilities.to_dict(),
            "provision": self.provision.value, "health": self.health.value,
            "primary": self.primary, "device": self.device,
            "remote_transport": self.remote_transport.value if self.remote_transport else None,
            "failure_category": self.failure_category, "light_probe_ok": self.light_probe_ok,
            "source_block": self.source_block,
            "query_style": self.query_style,
            "routable": self.routable,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class Contribution:
    source_id: str
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "rank": self.rank}


@dataclass
class Candidate:
    """A fused result. `cid` is the positional id used inside judge state."""
    cid: str
    title: str
    url: str
    snippet: str
    contributions: list[Contribution]
    privacy_class: PrivacyClass
    source_kind: Kind
    source_ts: float = 0.0
    clipped: bool = False
    rrf_score: float = 0.0
    judgment: dict[str, float] = field(default_factory=dict)   # rel / inj
    flags: list[str] = field(default_factory=list)             # unjudged:private, inj:suspect, ...

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "url": self.url, "snippet": self.snippet,
            "contributions": [c.to_dict() for c in self.contributions],
            "judgment": dict(self.judgment), "flags": list(self.flags),
            "source_ts": self.source_ts or None, "rrf_score": round(self.rrf_score, 6),
        }


@dataclass
class SourceRun:
    source_id: str
    status: RunStatus
    elapsed_ms: int
    results: list[Any] = field(default_factory=list)    # SearchResult-like objects
    device: Optional[str] = None
    error: Optional[str] = None                          # whitelisted category text only
    query_sent: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out = {"source_id": self.source_id, "status": self.status.value,
               "elapsed_ms": self.elapsed_ms, "count": len(self.results)}
        if self.device:
            out["device"] = self.device
        if self.error:
            out["error"] = self.error
        if self.query_sent is not None:
            out["query_sent"] = self.query_sent
        return out


@dataclass
class ChoiceAnswerSummary:
    choice: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {"choice": self.choice, "confidence": round(self.confidence, 4)}


@dataclass
class Plan:
    sources: list[str]
    reasons: dict[str, str]                     # source_id -> "forced:primary" | "fit:0.72" | "uncertain:0.5" | "default"
    judge: str                                  # host | typesafe | none | degraded | skipped:<reason>
    evidence_shape: Optional[ChoiceAnswerSummary] = None
    freshness_required: Optional[float] = None
    gate: Optional[str] = None                  # explicit_sources | bibliographic | local | sensitive_query | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": list(self.sources), "reasons": dict(self.reasons), "judge": self.judge,
            "evidence_shape": self.evidence_shape.to_dict() if self.evidence_shape else None,
            "freshness_required": self.freshness_required, "gate": self.gate,
        }


@dataclass
class Sufficiency:
    enough: Optional[float]
    next_action: Optional[str]
    confidence: Optional[float]
    scope: str                                  # delivered | public_only | after_recovery:raw_scope | none

    def to_dict(self) -> dict[str, Any]:
        return {"enough": self.enough, "next_action": self.next_action,
                "confidence": self.confidence, "scope": self.scope}


@dataclass
class Quality:
    executed: str                               # ok | partial | failed | cancelled
    relevance: str                              # judged | degraded | unjudged | skipped
    sufficiency: Sufficiency
    independent_families: int
    recovered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"executed": self.executed, "relevance": self.relevance,
                "sufficiency": self.sufficiency.to_dict(),
                "independent_families": self.independent_families, "recovered": self.recovered}


@dataclass
class JudgeDiagnostics:
    calls: int = 0          # logical judgments
    posts: int = 0          # physical requests
    model: Optional[str] = None
    backend: Optional[str] = None
    usage: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"calls": self.calls, "posts": self.posts, "model": self.model,
                "backend": self.backend, "usage": dict(self.usage), "failures": list(self.failures)}


@dataclass
class Evidence:
    query: str
    plan: Plan
    runs: list[SourceRun]
    results: list[Candidate]
    quality: Quality
    deadline_ms: int
    elapsed_ms: int
    judge: JudgeDiagnostics = field(default_factory=JudgeDiagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "plan": self.plan.to_dict(),
            "runs": [r.to_dict() for r in self.runs],
            "results": [c.to_dict() for c in self.results],
            "quality": self.quality.to_dict(),
            "diagnostics": {"deadline_ms": self.deadline_ms, "elapsed_ms": self.elapsed_ms,
                            "judge": self.judge.to_dict()},
        }


class ContractViolation(ValueError):
    pass


def validate_evidence(evidence: Evidence) -> None:
    """Spec §4.8 invariants. Raises ContractViolation."""
    run_ids = {run.source_id for run in evidence.runs}
    run_urls = {getattr(r, "url", None) for run in evidence.runs for r in run.results}
    for candidate in evidence.results:
        if candidate.url not in run_urls:
            raise ContractViolation(f"result url not produced by any run: {candidate.url}")
        for contribution in candidate.contributions:
            if contribution.source_id not in run_ids:
                raise ContractViolation(f"contribution from unknown run: {contribution.source_id}")
        if candidate.privacy_class != PrivacyClass.PUBLIC and candidate.judgment:
            raise ContractViolation(f"private candidate carries cloud judgment: {candidate.url}")
    if evidence.quality.executed not in {"ok", "partial", "failed", "cancelled"}:
        raise ContractViolation("bad executed state")
    if evidence.quality.relevance not in {"judged", "degraded", "unjudged", "skipped"}:
        raise ContractViolation("bad relevance state")
    if evidence.judge.posts > evidence.judge.calls:
        raise ContractViolation("posts cannot exceed logical judge calls")
    if evidence.quality.executed == "cancelled" and evidence.quality.relevance == "judged":
        raise ContractViolation("cancelled run cannot claim judged relevance")
