"""v6 engine manifest discovery and schema validation.

P0 intentionally stops at parsing and validation. Adapter modules are only
checked as manifest strings; importing them belongs to registry/doctor phases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
MANIFEST_FILENAME = "engine.yaml"
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
TRUST_BUILTIN = "builtin"
TRUST_PROJECT = "project"
TRUST_USER = "user"


@dataclass(frozen=True)
class EnginePluginManifest:
    schema_version: int
    id: str
    name: str
    kind: str
    capabilities: dict[str, Any]
    routing: dict[str, Any]
    requirements: dict[str, Any]
    health: dict[str, Any]
    requires_capabilities: dict[str, bool] = field(default_factory=dict)
    adapter: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "capabilities": self.capabilities,
            "routing": self.routing,
            "requirements": self.requirements,
            "health": self.health,
            "requires_capabilities": self.requires_capabilities,
            "adapter": self.adapter,
        }


@dataclass(frozen=True)
class EngineDiscovery:
    path: Path
    source: str
    trust_level: str
    valid: bool
    manifest: EnginePluginManifest | None = None
    errors: tuple[str, ...] = ()
    duplicate_of: Path | None = None
    overridden_by: Path | None = None

    @property
    def engine_id(self) -> str | None:
        if self.manifest is None:
            return None
        return self.manifest.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "source": self.source,
            "trust_level": self.trust_level,
            "valid": self.valid,
            "engine_id": self.engine_id,
            "errors": list(self.errors),
            "duplicate_of": str(self.duplicate_of) if self.duplicate_of else None,
            "overridden_by": str(self.overridden_by) if self.overridden_by else None,
            "manifest": self.manifest.to_dict() if self.manifest else None,
        }


def builtin_manifest_paths() -> list[Path]:
    """Return builtin manifest paths in deterministic order."""
    if not BUILTIN_DIR.exists():
        return []
    return sorted(BUILTIN_DIR.glob(f"*/{MANIFEST_FILENAME}"))


def discover_engine_plugins(
    paths: Sequence[str | Path] | None = None,
    *,
    include_builtin: bool = True,
) -> list[EngineDiscovery]:
    """Discover engine manifests from builtin and extra plugin roots.

    Extra entries may point either to an ``engine.yaml`` file or to a directory.
    Directories are scanned for immediate ``*/engine.yaml`` plugin manifests and
    for a direct ``engine.yaml`` when the directory itself is a plugin.
    """
    candidates: list[tuple[Path, str, str]] = []
    if include_builtin:
        candidates.extend((p, "builtin", TRUST_BUILTIN) for p in builtin_manifest_paths())

    for raw_path in paths or ():
        root = Path(raw_path).expanduser()
        for manifest_path in _manifest_paths_from(root):
            candidates.append((manifest_path, _source_for_path(root), _trust_for_path(root)))

    discoveries: list[EngineDiscovery] = []
    first_by_id: dict[str, int] = {}
    last_by_id: dict[str, int] = {}

    for manifest_path, source, trust_level in candidates:
        discovery = load_engine_manifest(manifest_path, source=source, trust_level=trust_level)
        discoveries.append(discovery)
        if not discovery.valid or discovery.engine_id is None:
            continue

        previous_index = last_by_id.get(discovery.engine_id)
        if previous_index is None:
            first_by_id[discovery.engine_id] = len(discoveries) - 1
        else:
            previous = discoveries[previous_index]
            discoveries[previous_index] = EngineDiscovery(
                path=previous.path,
                source=previous.source,
                trust_level=previous.trust_level,
                valid=previous.valid,
                manifest=previous.manifest,
                errors=previous.errors,
                duplicate_of=previous.duplicate_of,
                overridden_by=discovery.path,
            )
            discovery = EngineDiscovery(
                path=discovery.path,
                source=discovery.source,
                trust_level=discovery.trust_level,
                valid=discovery.valid,
                manifest=discovery.manifest,
                errors=discovery.errors,
                duplicate_of=discoveries[first_by_id[discovery.engine_id]].path,
                overridden_by=discovery.overridden_by,
            )
            discoveries[-1] = discovery
        last_by_id[discovery.engine_id] = len(discoveries) - 1

    return discoveries


def load_engine_manifest(
    path: str | Path,
    *,
    source: str = "path",
    trust_level: str = TRUST_PROJECT,
) -> EngineDiscovery:
    manifest_path = Path(path)
    try:
        payload = _parse_manifest_text(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return EngineDiscovery(
            path=manifest_path,
            source=source,
            trust_level=trust_level,
            valid=False,
            errors=(f"read_error:{exc}",),
        )
    except ValueError as exc:
        return EngineDiscovery(
            path=manifest_path,
            source=source,
            trust_level=trust_level,
            valid=False,
            errors=(f"parse_error:{exc}",),
        )

    manifest, errors = parse_engine_manifest(payload)
    return EngineDiscovery(
        path=manifest_path,
        source=source,
        trust_level=trust_level,
        valid=not errors,
        manifest=manifest,
        errors=tuple(errors),
    )


def parse_engine_manifest(payload: Mapping[str, Any]) -> tuple[EnginePluginManifest | None, list[str]]:
    errors: list[str] = []
    data = dict(payload)
    required = (
        "schema_version",
        "id",
        "name",
        "kind",
        "capabilities",
        "routing",
        "requirements",
        "health",
    )

    for field_name in required:
        if field_name not in data:
            errors.append(f"missing:{field_name}")

    schema_version = _coerce_int(data.get("schema_version"))
    if schema_version != SCHEMA_VERSION:
        errors.append(f"unsupported_schema_version:{data.get('schema_version')!r}")

    for field_name in ("id", "name", "kind"):
        if field_name in data and not _non_empty_string(data[field_name]):
            errors.append(f"invalid:{field_name}")

    capabilities = _mapping_or_error(data.get("capabilities"), "capabilities", errors)
    routing = _mapping_or_error(data.get("routing"), "routing", errors)
    requirements = _mapping_or_error(data.get("requirements"), "requirements", errors)
    health = _mapping_or_error(data.get("health"), "health", errors)
    requires_capabilities = _bool_mapping_or_error(
        data.get("requires_capabilities", {}),
        "requires_capabilities",
        errors,
    )

    if capabilities is not None:
        actions = capabilities.get("actions")
        if not isinstance(actions, list) or not all(_non_empty_string(v) for v in actions):
            errors.append("invalid:capabilities.actions")

    if routing is not None:
        modes = routing.get("modes")
        if not isinstance(modes, list) or not all(_non_empty_string(v) for v in modes):
            errors.append("invalid:routing.modes")

    checks = health.get("checks") if health is not None else None
    if checks is not None and not isinstance(checks, list):
        errors.append("invalid:health.checks")

    adapter = data.get("adapter")
    if adapter is not None and not _valid_adapter_reference(adapter):
        errors.append("invalid:adapter")

    if errors:
        return None, errors

    return (
        EnginePluginManifest(
            schema_version=schema_version,
            id=str(data["id"]),
            name=str(data["name"]),
            kind=str(data["kind"]),
            capabilities=dict(capabilities or {}),
            routing=dict(routing or {}),
            requirements=dict(requirements or {}),
            health=dict(health or {}),
            requires_capabilities=dict(requires_capabilities or {}),
            adapter=str(adapter) if adapter is not None else None,
            raw=data,
        ),
        [],
    )


def _manifest_paths_from(root: Path) -> list[Path]:
    if root.name == MANIFEST_FILENAME:
        return [root]

    direct = root / MANIFEST_FILENAME
    paths: list[Path] = []
    if direct.exists():
        paths.append(direct)
    if root.exists():
        paths.extend(sorted(root.glob(f"*/{MANIFEST_FILENAME}")))
    return paths


def _source_for_path(root: Path) -> str:
    parts = set(root.parts)
    if "plugins" in parts:
        return "project"
    return "path"


def _trust_for_path(root: Path) -> str:
    return TRUST_PROJECT if _source_for_path(root) == "project" else TRUST_USER


def _parse_manifest_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty manifest")

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = _parse_yaml_subset(stripped)

    if not isinstance(parsed, dict):
        raise ValueError("manifest root must be an object")
    return parsed


def _parse_yaml_subset(text: str) -> dict[str, Any]:
    """Parse a small YAML subset for tests and human-authored manifests.

    This supports indentation-based mappings, scalar lists, inline JSON values,
    booleans, nulls, numbers, and strings. It deliberately avoids full YAML
    semantics so P0 does not add a parser dependency.
    """
    cleaned_lines = _clean_yaml_lines(text)
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    for index, (lineno, line) in enumerate(cleaned_lines):
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"bad indentation at line {lineno}")

        parent = stack[-1][1]
        if stripped.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"unexpected list item at line {lineno}")
            parent.append(_parse_scalar(stripped[2:].strip()))
            continue

        if ":" not in stripped:
            raise ValueError(f"expected key/value at line {lineno}")
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            raise ValueError(f"empty key at line {lineno}")
        if not isinstance(parent, dict):
            raise ValueError(f"mapping item under list is unsupported at line {lineno}")

        if raw_value:
            parent[key] = _parse_scalar(raw_value)
            continue

        child: dict[str, Any] | list[Any]
        child = [] if _next_content_is_list(cleaned_lines, index, indent) else {}
        parent[key] = child
        stack.append((indent, child))
    return root


def _clean_yaml_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].rstrip()
        if line.strip():
            lines.append((lineno, line))
    return lines


def _next_content_is_list(lines: Sequence[tuple[int, str]], index: int, indent: int) -> bool:
    if index + 1 >= len(lines):
        return False
    _, next_line = lines[index + 1]
    next_indent = len(next_line) - len(next_line.lstrip(" "))
    return next_indent > indent and next_line.strip().startswith("- ")


def _parse_scalar(raw_value: str) -> Any:
    if raw_value in {"true", "True"}:
        return True
    if raw_value in {"false", "False"}:
        return False
    if raw_value in {"null", "None", "~"}:
        return None
    if raw_value.startswith(("[", "{", '"')) or raw_value.isdigit():
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            pass
    if (raw_value.startswith("'") and raw_value.endswith("'")) or (
        raw_value.startswith('"') and raw_value.endswith('"')
    ):
        return raw_value[1:-1]
    return raw_value


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _mapping_or_error(value: Any, field_name: str, errors: list[str]) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if value is not None or field_name not in errors:
        errors.append(f"invalid:{field_name}")
    return None


def _bool_mapping_or_error(value: Any, field_name: str, errors: list[str]) -> dict[str, bool] | None:
    if not isinstance(value, dict):
        errors.append(f"invalid:{field_name}")
        return None
    out: dict[str, bool] = {}
    for key, item in value.items():
        if not _non_empty_string(key) or not isinstance(item, bool):
            errors.append(f"invalid:{field_name}")
            return None
        out[str(key)] = item
    return out


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_adapter_reference(value: Any) -> bool:
    if not _non_empty_string(value):
        return False
    adapter = str(value)
    return "\x00" not in adapter and not adapter.startswith(("http://", "https://"))
