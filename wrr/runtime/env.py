"""Environment snapshot loading for the WRR v6 control plane.

This module parses .env files into an immutable report object without mutating
``os.environ``. Secret values are never emitted raw by ``to_dict()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence


TrustLevel = Literal["builtin", "user", "env_path", "project", "entry_point"]

_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET")


@dataclass(frozen=True)
class EnvFileCandidate:
    path: Path
    source: str
    trust_level: TrustLevel
    priority: int
    exists: bool
    loaded: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": _display_path(self.path),
            "source": self.source,
            "trust_level": self.trust_level,
            "priority": self.priority,
            "exists": self.exists,
            "loaded": self.loaded,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EnvValue:
    key: str
    value: str | None
    source: str
    source_path: Path | None
    priority: int
    secret: bool
    secret_allowed: bool
    redacted: str
    ignored: bool = False
    ignore_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "value": None if self.secret else self.value,
            "source": self.source,
            "source_path": _display_path(self.source_path),
            "priority": self.priority,
            "secret": self.secret,
            "secret_allowed": self.secret_allowed,
            "redacted": self.redacted,
            "ignored": self.ignored,
            "ignore_reason": self.ignore_reason,
        }


@dataclass(frozen=True)
class EnvConflict:
    key: str
    winner_source: str
    loser_source: str
    winner_path: Path | None
    loser_path: Path | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "winner_source": self.winner_source,
            "loser_source": self.loser_source,
            "winner_path": _display_path(self.winner_path),
            "loser_path": _display_path(self.loser_path),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EnvSnapshot:
    values: dict[str, EnvValue]
    candidates: list[EnvFileCandidate]
    conflicts: list[EnvConflict]
    ignored_values: list[EnvValue]
    warnings: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "values": {key: value.to_dict() for key, value in sorted(self.values.items())},
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "ignored_values": [value.to_dict() for value in self.ignored_values],
            "warnings": list(self.warnings),
        }


def env_file_candidates(runtime: object, cwd: Path | str) -> list[EnvFileCandidate]:
    """Return runtime env file candidates with provenance and trust metadata."""

    resolved_cwd = Path(cwd).resolve()
    paths = [Path(path).expanduser() for path in getattr(runtime, "env_files", [])]
    total = len(paths)
    candidates: list[EnvFileCandidate] = []
    seen: set[Path] = set()

    for index, path in enumerate(paths):
        resolved = path.resolve() if path.exists() else path.absolute()
        if resolved in seen:
            continue
        seen.add(resolved)
        trust_level, source = _classify_path(resolved, resolved_cwd)
        candidates.append(
            EnvFileCandidate(
                path=resolved,
                source=source,
                trust_level=trust_level,
                priority=total - index,
                exists=resolved.is_file(),
                reason=None if resolved.exists() else "missing",
            )
        )

    return candidates


def load_env(
    runtime: object,
    overrides: Mapping[str, str] | None = None,
    env_files: Sequence[Path | str | EnvFileCandidate] | None = None,
    trust_project: bool = False,
) -> EnvSnapshot:
    """Load an environment snapshot without changing process environment."""

    cwd = Path(getattr(runtime, "cwd", Path.cwd()))
    candidates = (
        _normalize_env_files(env_files, cwd)
        if env_files is not None
        else env_file_candidates(runtime, cwd)
    )

    values: dict[str, EnvValue] = {}
    conflicts: list[EnvConflict] = []
    ignored: list[EnvValue] = []
    warnings: list[str] = []
    reported_candidates: list[EnvFileCandidate] = []

    for candidate in sorted(candidates, key=lambda item: item.priority):
        if not candidate.exists:
            reported_candidates.append(candidate)
            continue

        try:
            parsed = _parse_env_file(candidate.path)
        except OSError as exc:
            reported_candidates.append(
                EnvFileCandidate(
                    path=candidate.path,
                    source=candidate.source,
                    trust_level=candidate.trust_level,
                    priority=candidate.priority,
                    exists=candidate.exists,
                    loaded=False,
                    reason=f"read_error: {exc}",
                )
            )
            warnings.append(f"env_file_read_error: {candidate.path}: {exc}")
            continue

        reported_candidates.append(
            EnvFileCandidate(
                path=candidate.path,
                source=candidate.source,
                trust_level=candidate.trust_level,
                priority=candidate.priority,
                exists=True,
                loaded=True,
            )
        )
        for key, raw_value in parsed.items():
            env_value = _make_env_value(
                key,
                raw_value,
                source=candidate.source,
                source_path=candidate.path,
                priority=candidate.priority,
                trust_level=candidate.trust_level,
                trust_project=trust_project,
            )
            if env_value.ignored:
                ignored.append(env_value)
                warnings.append(f"{env_value.ignore_reason}: {key} from {candidate.path}")
                continue
            _record_value(values, conflicts, env_value)

    if overrides:
        for key, raw_value in overrides.items():
            env_value = _make_env_value(
                key,
                str(raw_value),
                source="overrides",
                source_path=None,
                priority=10_000,
                trust_level="user",
                trust_project=True,
            )
            _record_value(values, conflicts, env_value)

    return EnvSnapshot(
        values=values,
        candidates=reported_candidates,
        conflicts=conflicts,
        ignored_values=ignored,
        warnings=warnings,
    )


def _display_path(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return f"~/{path.expanduser().resolve().relative_to(Path.home().resolve())}"
    except ValueError:
        return str(path)


def _normalize_env_files(
    env_files: Sequence[Path | str | EnvFileCandidate],
    cwd: Path,
) -> list[EnvFileCandidate]:
    total = len(env_files)
    candidates: list[EnvFileCandidate] = []
    for index, item in enumerate(env_files):
        if isinstance(item, EnvFileCandidate):
            candidates.append(item)
            continue
        path = Path(item).expanduser()
        resolved = path.resolve() if path.exists() else path.absolute()
        trust_level, source = _classify_explicit_env_path(resolved, cwd.resolve())
        candidates.append(
            EnvFileCandidate(
                path=resolved,
                source=source,
                trust_level=trust_level,
                priority=total - index,
                exists=resolved.is_file(),
                reason=None if resolved.exists() else "missing",
            )
        )
    return candidates


def _classify_path(path: Path, cwd: Path) -> tuple[TrustLevel, str]:
    try:
        path.relative_to(cwd)
    except ValueError:
        pass
    else:
        return "project", "project_env"
    return "user", "runtime_env"


def _classify_explicit_env_path(path: Path, cwd: Path) -> tuple[TrustLevel, str]:
    try:
        path.relative_to(cwd)
    except ValueError:
        # A file explicitly passed by the CLI/user is user-trusted even when it
        # lives outside the project. This is distinct from auto-discovered
        # project env files, whose secrets remain blocked by default.
        return "user", "explicit_env_path"
    return "project", "project_env"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            key, _sep, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            values[key] = value.strip().strip('"').strip("'")
    return values


def _record_value(
    values: dict[str, EnvValue],
    conflicts: list[EnvConflict],
    value: EnvValue,
) -> None:
    previous = values.get(value.key)
    if previous is not None:
        conflicts.append(
            EnvConflict(
                key=value.key,
                winner_source=value.source,
                loser_source=previous.source,
                winner_path=value.source_path,
                loser_path=previous.source_path,
                reason="higher_priority_override",
            )
        )
    values[value.key] = value


def _make_env_value(
    key: str,
    raw_value: str,
    *,
    source: str,
    source_path: Path | None,
    priority: int,
    trust_level: TrustLevel,
    trust_project: bool,
) -> EnvValue:
    secret = _is_secret_key(key)
    secret_allowed = _secret_allowed(trust_level, trust_project)
    ignored = secret and not secret_allowed
    value = None if secret or ignored else raw_value
    return EnvValue(
        key=key,
        value=value,
        source=source,
        source_path=source_path,
        priority=priority,
        secret=secret,
        secret_allowed=secret_allowed,
        redacted=_redact(raw_value, secret=secret),
        ignored=ignored,
        ignore_reason="project_env_ignored_secret" if ignored and trust_level == "project" else "untrusted_env_ignored_secret"
        if ignored
        else None,
    )


def _is_secret_key(key: str) -> bool:
    normalized = key.upper()
    return normalized.endswith(_SECRET_SUFFIXES) or normalized in {"API_KEY", "TOKEN", "SECRET"}


def _secret_allowed(trust_level: TrustLevel, trust_project: bool) -> bool:
    if trust_level == "project":
        return trust_project
    return trust_level in {"builtin", "user", "entry_point"}


def _redact(value: str, *, secret: bool) -> str:
    if not secret:
        return value
    return "<secret-present>"
