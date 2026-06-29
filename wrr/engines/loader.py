"""v6 engine manifest discovery and schema validation.

P0 intentionally stops at parsing and validation. Adapter modules are only
checked as manifest strings; importing them belongs to registry/doctor phases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
MANIFEST_FILENAME = "engine.yaml"
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
TRUST_BUILTIN = "builtin"
TRUST_PROJECT = "project"
TRUST_USER = "user"


@dataclass(frozen=True)
class RepoRequirement:
    name: str
    env: str | None = None
    path: str | None = None
    default_path: str | None = None
    remote: str | None = None
    pin: str | None = None
    required: bool = True
    trust_required: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "env": self.env,
            "path": self.path,
            "default_path": self.default_path,
            "remote": self.remote,
            "pin": self.pin,
            "required": self.required,
            "trust_required": self.trust_required,
        }


@dataclass(frozen=True)
class RepoPinStatus:
    path: str
    expected_pin: str | None
    current_revision: str | None
    status: str
    drift: bool
    message: str

    @property
    def matched(self) -> bool:
        return self.status in {"match", "unpinned"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "expected_pin": self.expected_pin,
            "current_revision": self.current_revision,
            "status": self.status,
            "drift": self.drift,
            "matched": self.matched,
            "message": self.message,
        }


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
    fusion: dict[str, Any] = field(default_factory=dict)
    repo_requirements: tuple[RepoRequirement, ...] = ()
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
            "fusion": self.fusion,
            "repo_requirements": [item.to_dict() for item in self.repo_requirements],
        }


@dataclass(frozen=True)
class EngineDiscovery:
    path: Path
    source: str
    trust_level: str
    valid: bool
    manifest: EnginePluginManifest | None = None
    errors: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
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
            "blocked_reasons": list(self.blocked_reasons),
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
    trust_project: bool = False,
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
        discovery = _with_blocked_reasons(discovery, trust_project=trust_project)
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
                blocked_reasons=previous.blocked_reasons,
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
                blocked_reasons=discovery.blocked_reasons,
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
            blocked_reasons=(),
        )
    except ValueError as exc:
        return EngineDiscovery(
            path=manifest_path,
            source=source,
            trust_level=trust_level,
            valid=False,
            errors=(f"parse_error:{exc}",),
            blocked_reasons=(),
        )

    manifest, errors = parse_engine_manifest(payload)
    return EngineDiscovery(
        path=manifest_path,
        source=source,
        trust_level=trust_level,
        valid=not errors,
        manifest=manifest,
        errors=tuple(errors),
        blocked_reasons=(),
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
    fusion = _fusion_or_default(data.get("fusion"), errors)

    if capabilities is not None:
        actions = capabilities.get("actions")
        if not isinstance(actions, list) or not all(_non_empty_string(v) for v in actions):
            errors.append("invalid:capabilities.actions")
        domains = capabilities.get("domains")
        if domains is None:
            capabilities["domains"] = ["web"]
        elif not isinstance(domains, list) or not all(_non_empty_string(v) for v in domains):
            errors.append("invalid:capabilities.domains")

    if routing is not None:
        modes = routing.get("modes")
        if not isinstance(modes, list) or not all(_non_empty_string(v) for v in modes):
            errors.append("invalid:routing.modes")
        _validate_triggers(routing.get("triggers", []), errors)

    checks = health.get("checks") if health is not None else None
    if checks is not None and not isinstance(checks, list):
        errors.append("invalid:health.checks")

    repo_requirements = _repo_requirements_or_error(requirements or {}, errors)

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
            fusion=dict(fusion),
            repo_requirements=tuple(repo_requirements),
            raw=data,
        ),
        [],
    )


def default_fusion_config() -> dict[str, Any]:
    """Return v6 manifest fusion defaults aligned with v5 config constants."""

    from wrr import config

    defaults = getattr(config, "FUSION_DEFAULTS", None)
    if isinstance(defaults, Mapping):
        return dict(defaults)
    return {
        "rrf_k": config.RRF_K,
        "dedup_key": "canonical_url",
        "weights_merge_policy": "multiply",
    }


def trigger_matches(manifest: EnginePluginManifest | Mapping[str, Any], query: str) -> bool:
    """Evaluate manifest ``routing.triggers[].match`` rules against a query.

    ``classifier_ref`` is schema metadata here; resolve/routing schema tests must
    not import adapters or execute arbitrary plugin code.
    """

    routing = manifest.routing if isinstance(manifest, EnginePluginManifest) else manifest.get("routing", {})
    triggers = routing.get("triggers", []) if isinstance(routing, Mapping) else []
    if not isinstance(triggers, list):
        return False
    return any(
        _trigger_match(item.get("match"), query)
        for item in triggers
        if isinstance(item, Mapping)
    )


def merge_routing_config(
    manifest: EnginePluginManifest,
    user_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge manifest routing/fusion with user overrides.

    Scalars are replaced by user values. Lists are ordered unions. Routing
    weights multiply by default; ``replace=true`` switches weight replacement.
    """

    user_config = user_config or {}
    merged = {
        "routing": dict(manifest.routing),
        "fusion": dict(manifest.fusion),
    }
    routing_override = user_config.get("routing")
    if isinstance(routing_override, Mapping):
        merged["routing"] = _merge_mapping(
            merged["routing"],
            routing_override,
            multiply_weight=not bool(routing_override.get("replace")),
        )
    fusion_override = user_config.get("fusion")
    if isinstance(fusion_override, Mapping):
        merged["fusion"] = _merge_mapping(
            merged["fusion"],
            fusion_override,
            multiply_weight=False,
        )
    return merged


def verify_repo_pin(
    path: str | Path,
    pin: str | None,
    *,
    revision_resolver: Any | None = None,
) -> RepoPinStatus:
    """Check a local git worktree revision against an expected commit/tag pin.

    The optional resolver keeps tests free of git subprocesses. It receives
    ``(path, ref)`` where ``ref`` is ``"HEAD"`` or the requested pin.
    """

    repo_path = Path(path).expanduser()
    if not repo_path.exists():
        return RepoPinStatus(
            path=str(repo_path),
            expected_pin=pin,
            current_revision=None,
            status="missing",
            drift=bool(pin),
            message="repo_path_missing",
        )
    if revision_resolver is None and not (repo_path / ".git").exists():
        return RepoPinStatus(
            path=str(repo_path),
            expected_pin=pin,
            current_revision=None,
            status="unavailable",
            drift=bool(pin),
            message="repo_revision_unavailable",
        )

    current = _resolve_git_revision(repo_path, "HEAD", revision_resolver)
    if not current:
        return RepoPinStatus(
            path=str(repo_path),
            expected_pin=pin,
            current_revision=None,
            status="unavailable",
            drift=bool(pin),
            message="repo_revision_unavailable",
        )

    if not pin:
        return RepoPinStatus(
            path=str(repo_path),
            expected_pin=None,
            current_revision=current,
            status="unpinned",
            drift=False,
            message="repo_unpinned",
        )

    expected = _resolve_git_revision(repo_path, pin, revision_resolver) or pin
    matched = current == expected or current.startswith(pin) or pin.startswith(current)
    return RepoPinStatus(
        path=str(repo_path),
        expected_pin=pin,
        current_revision=current,
        status="match" if matched else "mismatch",
        drift=not matched,
        message="repo_pin_match" if matched else "repo_pin_mismatch",
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


def _with_blocked_reasons(discovery: EngineDiscovery, *, trust_project: bool) -> EngineDiscovery:
    manifest = discovery.manifest
    reasons: list[str] = []
    if (
        discovery.valid
        and manifest is not None
        and manifest.adapter
        and discovery.trust_level == TRUST_PROJECT
        and not trust_project
    ):
        reasons.append("untrusted_project_plugin")
        if manifest.adapter.startswith(".") or manifest.adapter.endswith(".py"):
            reasons.append("untrusted_project_adapter_path")
    if not reasons:
        return discovery
    return EngineDiscovery(
        path=discovery.path,
        source=discovery.source,
        trust_level=discovery.trust_level,
        valid=discovery.valid,
        manifest=discovery.manifest,
        errors=discovery.errors,
        blocked_reasons=tuple(reasons),
        duplicate_of=discovery.duplicate_of,
        overridden_by=discovery.overridden_by,
    )


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


def _repo_requirements_or_error(
    requirements: Mapping[str, Any],
    errors: list[str],
) -> list[RepoRequirement]:
    raw_repos = requirements.get("repos", [])
    if raw_repos is None:
        return []
    if not isinstance(raw_repos, list):
        errors.append("invalid:requirements.repos")
        return []

    repos: list[RepoRequirement] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_repos):
        if not isinstance(item, Mapping):
            errors.append(f"invalid:requirements.repos[{index}]")
            continue
        name = item.get("name")
        if not _non_empty_string(name):
            errors.append(f"invalid:requirements.repos[{index}].name")
            continue
        repo_name = str(name)
        if repo_name in seen:
            errors.append(f"duplicate:requirements.repos[{repo_name}]")
            continue
        seen.add(repo_name)
        for field_name in ("env", "path", "default_path", "remote", "pin", "trust_required"):
            value = item.get(field_name)
            if value is not None and not _non_empty_string(value):
                errors.append(f"invalid:requirements.repos[{repo_name}].{field_name}")
        required = item.get("required", True)
        if not isinstance(required, bool):
            errors.append(f"invalid:requirements.repos[{repo_name}].required")
            continue
        repos.append(
            RepoRequirement(
                name=repo_name,
                env=str(item["env"]) if item.get("env") is not None else None,
                path=str(item["path"]) if item.get("path") is not None else None,
                default_path=str(item["default_path"]) if item.get("default_path") is not None else None,
                remote=str(item["remote"]) if item.get("remote") is not None else None,
                pin=str(item["pin"]) if item.get("pin") is not None else None,
                required=required,
                trust_required=str(item["trust_required"])
                if item.get("trust_required") is not None
                else None,
            )
        )
    return repos


def _fusion_or_default(value: Any, errors: list[str]) -> dict[str, Any]:
    if value is None:
        return default_fusion_config()
    if not isinstance(value, dict):
        errors.append("invalid:fusion")
        return default_fusion_config()

    fusion = default_fusion_config()
    fusion.update(value)
    if not isinstance(fusion.get("rrf_k"), int) or isinstance(fusion.get("rrf_k"), bool):
        errors.append("invalid:fusion.rrf_k")
    if not _non_empty_string(fusion.get("dedup_key")):
        errors.append("invalid:fusion.dedup_key")
    if fusion.get("weights_merge_policy") not in {"multiply", "replace"}:
        errors.append("invalid:fusion.weights_merge_policy")
    return fusion


def _validate_triggers(value: Any, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        errors.append("invalid:routing.triggers")
        return
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(f"invalid:routing.triggers[{index}]")
            continue
        match = item.get("match")
        if not isinstance(match, Mapping):
            errors.append(f"invalid:routing.triggers[{index}].match")
            continue
        if not any(key in match for key in ("keywords", "regex", "classifier_ref")):
            errors.append(f"invalid:routing.triggers[{index}].match")
        keywords = match.get("keywords")
        if keywords is not None and (
            not isinstance(keywords, list) or not all(_non_empty_string(v) for v in keywords)
        ):
            errors.append(f"invalid:routing.triggers[{index}].match.keywords")
        regex = match.get("regex")
        if regex is not None:
            patterns = regex if isinstance(regex, list) else [regex]
            if not all(_non_empty_string(pattern) for pattern in patterns):
                errors.append(f"invalid:routing.triggers[{index}].match.regex")
            else:
                for pattern in patterns:
                    try:
                        re.compile(str(pattern))
                    except re.error:
                        errors.append(f"invalid:routing.triggers[{index}].match.regex")
                        break
        classifier_ref = match.get("classifier_ref")
        if classifier_ref is not None and not _non_empty_string(classifier_ref):
            errors.append(f"invalid:routing.triggers[{index}].match.classifier_ref")


def _trigger_match(match: Any, query: str) -> bool:
    if not isinstance(match, Mapping):
        return False
    q = (query or "").lower()
    keywords = match.get("keywords") or []
    if isinstance(keywords, list) and any(str(keyword).lower() in q for keyword in keywords):
        return True
    regex = match.get("regex")
    patterns = regex if isinstance(regex, list) else ([regex] if regex else [])
    for pattern in patterns:
        if _non_empty_string(pattern) and re.search(str(pattern), query or "", flags=re.IGNORECASE):
            return True
    return False


def _merge_mapping(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
    *,
    multiply_weight: bool,
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key == "replace":
            continue
        if key == "weight" and multiply_weight and isinstance(value, (int, float)):
            current = merged.get(key, 1.0)
            merged[key] = current * value if isinstance(current, (int, float)) else value
            continue
        current = merged.get(key)
        if isinstance(current, list) and isinstance(value, list):
            merged[key] = _ordered_union(current, value)
        else:
            merged[key] = value
    return merged


def _ordered_union(left: Sequence[Any], right: Sequence[Any]) -> list[Any]:
    out: list[Any] = []
    for item in [*left, *right]:
        if item not in out:
            out.append(item)
    return out


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


def _resolve_git_revision(
    path: Path,
    ref: str,
    revision_resolver: Any | None,
) -> str | None:
    if revision_resolver is not None:
        try:
            revision = revision_resolver(path, ref)
        except TypeError:
            revision = revision_resolver(path)
        return str(revision).strip() if revision else None

    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", ref],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    revision = completed.stdout.strip()
    if completed.returncode != 0 or not revision:
        return None
    return revision
