"""Runtime identity and capability detection for WRR v6.

The detector is intentionally dependency-free and fully injectable so unit
tests do not need live Hermes, Codex, Claude Code, or OMP processes.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Mapping


RuntimeName = Literal["hermes", "claude_code", "codex", "omp", "standalone", "unknown"]
RuntimeSource = Literal["explicit", "env", "config", "process", "fallback"]
ExecutionModel = Literal["daemon", "one_shot", "unknown"]

_RUNTIME_NAMES = {"hermes", "claude_code", "codex", "omp", "standalone", "unknown"}


@dataclass(frozen=True)
class RuntimeCapabilities:
    conversation_history: bool
    agent_memory: bool
    local_kb: bool
    ai_cli_search: bool
    can_spawn_cli: bool
    can_read_user_env: bool
    preferred_timeout_ms: int
    execution_model: ExecutionModel = "unknown"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int | None = None
    executable: str | None = None
    argv: list[str] | None = None
    parent_pid: int | None = None
    parent_executable: str | None = None
    parent_argv: list[str] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeInfo:
    name: RuntimeName
    confidence: float
    source: RuntimeSource
    cwd: Path
    config_roots: list[Path]
    env_files: list[Path]
    data_roots: list[Path]
    capabilities: RuntimeCapabilities
    warnings: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "confidence": self.confidence,
            "source": self.source,
            "cwd": str(self.cwd),
            "config_roots": [str(path) for path in self.config_roots],
            "env_files": [str(path) for path in self.env_files],
            "data_roots": [str(path) for path in self.data_roots],
            "capabilities": self.capabilities.to_dict(),
            "warnings": list(self.warnings),
        }


def detect_runtime(
    *,
    explicit: str | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    process: ProcessSnapshot | None = None,
) -> RuntimeInfo:
    """Detect the current WRR runtime.

    Precedence is:
    explicit arg > WRR_RUNTIME > env signals > cwd/config markers > process > fallback.
    """

    runtime_env = os.environ if env is None else env
    resolved_cwd = Path.cwd() if cwd is None else Path(cwd)

    candidates: list[tuple[RuntimeName, RuntimeSource, float, str]] = []

    explicit_name = _normalize_runtime(explicit)
    if explicit_name is not None:
        candidates.append((explicit_name, "explicit", 1.0, "explicit"))

    wrr_runtime_name = _normalize_runtime(runtime_env.get("WRR_RUNTIME"))
    if wrr_runtime_name is not None:
        candidates.append((wrr_runtime_name, "env", 0.98, "WRR_RUNTIME"))

    env_name = _detect_from_env(runtime_env)
    if env_name is not None:
        candidates.append((env_name, "env", 0.92, "environment"))

    config_name = _detect_from_config(resolved_cwd)
    if config_name is not None:
        candidates.append((config_name, "config", 0.8, "cwd_config"))

    process_name = _detect_from_process(process)
    if process_name is not None:
        candidates.append((process_name, "process", 0.65, "process"))

    if candidates:
        name, source, confidence, label = candidates[0]
    else:
        name, source, confidence, label = "standalone", "fallback", 0.2, "fallback"

    warnings = _conflict_warnings(name, label, candidates)
    return RuntimeInfo(
        name=name,
        confidence=confidence,
        source=source,
        cwd=resolved_cwd,
        config_roots=_config_roots(name, resolved_cwd),
        env_files=_env_files(name, resolved_cwd),
        data_roots=_data_roots(name),
        capabilities=_capabilities(name),
        warnings=warnings,
    )


def _normalize_runtime(value: str | None) -> RuntimeName | None:
    if value is None:
        return None

    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "claude": "claude_code",
        "claudecode": "claude_code",
        "claude_cli": "claude_code",
        "default": "standalone",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in _RUNTIME_NAMES:
        return normalized  # type: ignore[return-value]
    return None


def _detect_from_env(env: Mapping[str, str]) -> RuntimeName | None:
    key_prefixes: list[tuple[str, RuntimeName]] = [
        ("HERMES_", "hermes"),
        ("CLAUDECODE_", "claude_code"),
        ("CLAUDE_CODE_", "claude_code"),
        ("CODEX_", "codex"),
        ("OMP_", "omp"),
    ]
    for prefix, runtime in key_prefixes:
        if any(key.startswith(prefix) for key in env):
            return runtime
    return None


def _detect_from_config(cwd: Path) -> RuntimeName | None:
    markers: list[tuple[str, RuntimeName]] = [
        (".hermes", "hermes"),
        (".claude", "claude_code"),
        (".codex", "codex"),
        (".omp", "omp"),
    ]
    for current in [cwd, *cwd.parents]:
        for marker, runtime in markers:
            if (current / marker).exists():
                return runtime
    return None


def _detect_from_process(process: ProcessSnapshot | None) -> RuntimeName | None:
    if process is None:
        return None

    parts: list[str] = []
    for value in (process.executable, process.parent_executable):
        if value:
            parts.append(Path(value).name.lower())
    for argv in (process.argv, process.parent_argv):
        if argv:
            parts.extend(str(part).lower() for part in argv)

    joined = " ".join(parts)
    if "hermes" in joined:
        return "hermes"
    if "claudecode" in joined or "claude-code" in joined or "claude_code" in joined:
        return "claude_code"
    if "codex" in joined:
        return "codex"
    if "omp" in joined:
        return "omp"
    return None


def _conflict_warnings(
    selected: RuntimeName,
    selected_label: str,
    candidates: list[tuple[RuntimeName, RuntimeSource, float, str]],
) -> list[str]:
    warnings: list[str] = []
    seen: set[tuple[str, RuntimeName]] = set()
    for name, _source, _confidence, label in candidates[1:]:
        if name == selected or (label, name) in seen:
            continue
        warnings.append(
            f"runtime_signal_conflict: selected {selected} from {selected_label}; "
            f"ignored {name} from {label}"
        )
        seen.add((label, name))
    return warnings


def _config_roots(name: RuntimeName, cwd: Path) -> list[Path]:
    home = Path.home()
    roots = {
        "hermes": [home / ".hermes", cwd],
        "claude_code": [home / ".claude", home / ".config" / "claude", cwd],
        "codex": [home / ".codex", home / ".config" / "codex", cwd],
        "omp": [home / ".omp", home / ".omp" / "agent", cwd],
        "standalone": [cwd],
        "unknown": [cwd],
    }
    return roots[name]


def _env_files(name: RuntimeName, cwd: Path) -> list[Path]:
    home = Path.home()
    by_runtime = {
        "hermes": [cwd / ".env", home / ".config" / "wrr" / "env.d" / "hermes.env", home / ".hermes" / ".env"],
        "claude_code": [cwd / ".env", home / ".config" / "wrr" / "env.d" / "claude_code.env", home / ".claude" / ".env"],
        "codex": [cwd / ".env", home / ".config" / "wrr" / "env.d" / "codex.env", home / ".codex" / ".env"],
        "omp": [
            cwd / ".env",
            home / ".config" / "wrr" / "env.d" / "omp.env",
            home / ".omp" / ".env",
            home / ".omp" / "agent" / ".env",
        ],
        "standalone": [cwd / ".env", home / ".config" / "wrr" / "env.d" / "default.env"],
        "unknown": [cwd / ".env", home / ".config" / "wrr" / "env.d" / "default.env"],
    }
    return by_runtime[name]


def _data_roots(name: RuntimeName) -> list[Path]:
    home = Path.home()
    cache = home / ".cache" / "wrr"
    roots = {
        "hermes": [home / ".hermes", cache],
        "claude_code": [home / ".claude", cache],
        "codex": [home / ".codex", cache],
        "omp": [home / ".omp" / "agent", cache],
        "standalone": [cache],
        "unknown": [cache],
    }
    return roots[name]


def _capabilities(name: RuntimeName) -> RuntimeCapabilities:
    defaults: dict[RuntimeName, RuntimeCapabilities] = {
        "hermes": RuntimeCapabilities(True, True, True, True, True, True, 30000, "daemon"),
        "claude_code": RuntimeCapabilities(True, True, True, True, True, True, 30000, "one_shot"),
        "codex": RuntimeCapabilities(True, True, True, True, True, True, 30000, "one_shot"),
        "omp": RuntimeCapabilities(True, False, True, True, True, True, 30000, "daemon"),
        "standalone": RuntimeCapabilities(False, False, True, False, True, True, 10000, "one_shot"),
        "unknown": RuntimeCapabilities(False, False, False, False, False, False, 10000, "unknown"),
    }
    return defaults[name]
