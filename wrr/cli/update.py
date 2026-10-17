"""Explicitly authorized Git candidates; never update a live dependency in place."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from wrr.engines.loader import EngineDiscovery, RepoRequirement, discover_engine_plugins


@dataclass(frozen=True)
class ManagedRepoResult:
    engine_id: str
    repo: RepoRequirement
    path: Path
    action: str
    status: str
    dry_run: bool
    message: str
    trust_level: str
    candidate_path: Path | None = None
    current_revision: str | None = None
    candidate_revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        repo = self.repo.to_dict()
        # Rejected credential-bearing URLs must not leak through the report either.
        if repo.get("remote") and _source(repo["remote"]) is None:
            repo["remote"] = "[invalid source omitted]"
        return {
            "engine_id": self.engine_id,
            "repo": repo,
            "path": str(self.path),
            "action": self.action,
            "status": self.status,
            "dry_run": self.dry_run,
            "message": self.message,
            "trust_level": self.trust_level,
            "candidate_path": str(self.candidate_path) if self.candidate_path else None,
            "current_revision": self.current_revision,
            "candidate_revision": self.candidate_revision,
            "revision_verified": self.status == "downloaded",
            "activated": False,
        }


@dataclass(frozen=True)
class UpdateReport:
    dry_run: bool
    trust_project: bool
    repos: tuple[ManagedRepoResult, ...]
    summary: dict[str, Any]
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "trust": {"project": self.trust_project},
            "repos": [item.to_dict() for item in self.repos],
            "summary": dict(self.summary),
            "errors": list(self.errors),
        }


def update(
    *,
    dry_run: bool = True,
    trust_project: bool = False,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    plugin_paths: Iterable[str | Path] | None = None,
    discoveries: Sequence[EngineDiscovery] | None = None,
    owned_paths: Iterable[str | Path] = (),
    candidate_root: str | Path | None = None,
) -> UpdateReport:
    """Plan, or prepare candidates for exact invocation-owned WRR-private targets.

    Nonempty owned_paths also selects targets. Source trust is a separate gate.
    No manifest, cache location or discovery establishes lifecycle ownership.
    Success proves a downloaded commit, not consumer verification or activation.
    """
    resolved_cwd = Path.cwd() if cwd is None else Path(cwd).absolute()
    process_env = os.environ if env is None else env
    paths = tuple(plugin_paths or (resolved_cwd / "plugins" / "engines",))
    discovered = tuple(discoveries) if discoveries is not None else tuple(
        discover_engine_plugins(paths, include_builtin=True, trust_project=trust_project)
    )
    owned = {_absolute(Path(path), resolved_cwd) for path in owned_paths}
    root = _absolute(Path(candidate_root), resolved_cwd) if candidate_root is not None else None
    declarations = [
        (discovery, requirement, _absolute(resolve_repo_path(requirement, env=process_env), resolved_cwd))
        for discovery in discovered if discovery.valid and discovery.manifest is not None
        for requirement in discovery.manifest.repo_requirements
    ]
    unknown = owned - {path for _, _, path in declarations}
    errors = ("owned_target_not_declared",) if unknown else ()
    if root is not None and not dry_run:
        physical_root = root.resolve()
        if any(physical_root.is_relative_to(path.resolve()) or path.resolve().is_relative_to(physical_root)
               for _, _, path in declarations):
            errors += ("candidate_overlaps_declared_target",)
    # A typo in the authorization set must not produce a partial write.
    results = [] if errors else [
        _prepare_candidate(
            requirement, path=path, engine_id=discovery.manifest.id,
            trust_level=discovery.trust_level, trust_project=trust_project,
            dry_run=dry_run, owned=path in owned, candidate_root=root,
        )
        for discovery, requirement, path in declarations if not owned or path in owned
    ]
    return UpdateReport(dry_run, trust_project, tuple(results), _summarize(results, errors), errors)


def _prepare_candidate(
    requirement: RepoRequirement,
    *,
    path: Path,
    engine_id: str,
    trust_level: str,
    trust_project: bool,
    dry_run: bool,
    owned: bool,
    candidate_root: Path | None,
) -> ManagedRepoResult:
    def result(status, message, **details):
        return ManagedRepoResult(engine_id, requirement, path, "prepare_candidate", status,
                                 dry_run, message, trust_level, **details)

    if trust_level not in {"builtin", "user", "entry_point"} and not (
        trust_level == "project" and trust_project
    ):
        return result("refused", "source_trust_required")
    if dry_run:
        return result("planned", "would_prepare_candidate_admission_not_checked")
    if not owned:
        return result("refused", "exact_target_ownership_required")
    if os.name != "posix" or not shutil.rmtree.avoids_symlink_attacks:
        return result("refused", "safe_directory_handles_unavailable")
    if _symlink_component(path):
        return result("refused", "target_symlink_refused")
    if candidate_root is None or not candidate_root.is_dir() or _symlink_component(candidate_root):
        return result("refused", "existing_owned_candidate_root_required")
    if candidate_root.is_relative_to(path) or path.is_relative_to(candidate_root):
        return result("refused", "candidate_target_overlap")
    if not requirement.pin or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", requirement.pin):
        return result("refused", "full_commit_pin_required")
    source = _source(requirement.remote)
    if source is None:
        return result("refused", "source_transport_refused")

    current = None
    try:
        root_identity = candidate_root.stat(follow_symlinks=False)
        if root_identity.st_uid != os.geteuid() or root_identity.st_mode & 0o022:
            return result("refused", "private_candidate_root_required")
        if path.exists():
            reason, current = _target_state(path, source)
            if reason:
                return result("refused", reason, current_revision=current)
        with ExitStack() as handles:
            root_fd = _open_directory(candidate_root)
            handles.callback(os.close, root_fd)
            if not _same_inode(root_identity, os.fstat(root_fd)):
                raise ValueError("candidate_root_changed")
            name = "wrr-candidate-" + uuid.uuid4().hex
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
            try:
                candidate_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                handles.callback(os.close, candidate_fd)
                _git("clone", "--no-checkout", "--no-hardlinks", "--", source, ".", cwd_fd=candidate_fd)
                _git("checkout", "--detach", requirement.pin, "--", cwd_fd=candidate_fd)
                revision = _git("rev-parse", "--verify", "HEAD", cwd_fd=candidate_fd)
                if revision != requirement.pin.lower():
                    raise ValueError("candidate_pin_mismatch")
                if _symlink_component(candidate_root) or not _same_inode(
                    candidate_root.stat(follow_symlinks=False), os.fstat(root_fd)
                ):
                    raise ValueError("candidate_root_changed")
                if path.exists():
                    reason, latest = _target_state(path, source)
                    if reason or latest != current:
                        raise ValueError("target_changed_during_preparation")
                elif current is not None:
                    raise ValueError("target_changed_during_preparation")
                return result("downloaded", "candidate_requires_consumer_verification_and_activation",
                              candidate_path=candidate_root / name, current_revision=current,
                              candidate_revision=revision)
            except (OSError, subprocess.SubprocessError, ValueError):
                try:
                    shutil.rmtree(name, dir_fd=root_fd)
                except OSError:
                    raise ValueError("candidate_cleanup_failed") from None
                raise
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "candidate_git_failed"
        return result("failed", reason, current_revision=current)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_directory(path: Path) -> int:
    """Bind every path component without following a replaceable parent symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise


def _absolute(path: Path, cwd: Path) -> Path:
    path = path.expanduser()
    return Path(os.path.abspath(path if path.is_absolute() else cwd / path))


def _symlink_component(path: Path) -> bool:
    return any(part.is_symlink() for part in (path, *path.parents))


def _source(remote: str | None) -> str | None:
    if not remote or any(character in remote for character in "\x00\r\n"):
        return None
    if Path(remote).is_absolute():
        return str(Path(remote).resolve())
    try:
        parsed = urlsplit(remote)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        if parsed.scheme == "https" and parsed.hostname and parsed.port != 0:
            return remote
        if parsed.scheme == "file" and not parsed.netloc and Path(unquote(parsed.path)).is_absolute():
            return str(Path(unquote(parsed.path)).resolve())
    except ValueError:
        pass
    return None


def _target_state(path: Path, source: str) -> tuple[str | None, str | None]:
    try:
        if not path.is_dir() or not (path / ".git").is_dir() or (path / ".git").is_symlink():
            return "standalone_git_target_required", None
        top = _git("-C", str(path), "rev-parse", "--show-toplevel")
        if Path(top) != path:
            return "git_target_identity_mismatch", None
        origins = _git("-C", str(path), "config", "--get-all", "remote.origin.url").splitlines()
        if len(origins) != 1 or _source(origins[0]) != source:
            return "target_origin_mismatch", None
        revision = _git("-C", str(path), "rev-parse", "--verify", "HEAD")
        # Git status deliberately hides these entries, so it cannot certify a clean target.
        entries = _git("-C", str(path), "ls-files", "-v").splitlines()
        if any(entry and (entry[0].islower() or entry[0] == "S") for entry in entries):
            return "unverifiable_index_flags", revision
        filters = _git("-C", str(path), "config", "--includes", "--name-only",
                       "--get-regexp", r"^filter\..*\.(clean|process)$", ok_codes=(0, 1))
        if filters:
            return "repository_filters_require_owner_review", revision
        staged = _git("-C", str(path), "ls-files", "--stage").splitlines()
        if any(entry.startswith("160000 ") for entry in staged):
            return "submodules_require_owner_review", revision
        dirty = _git("-C", str(path), "status", "--porcelain=v1", "--untracked-files=all",
                     "--ignored=matching", "--ignore-submodules=none")
        return ("dirty_target_refused" if dirty else None), revision
    except (OSError, subprocess.SubprocessError):
        return "target_git_inspection_failed", None


def _git(*args: str, cwd_fd: int | None = None, ok_codes: tuple[int, ...] = (0,)) -> str:
    # Maintenance must not inherit host credentials, URL rewrites, hooks or GIT_* overrides.
    env = {
        "PATH": os.defpath,
        "HOME": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    command = [
        "git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false", "-c", "credential.helper=", "-c", "core.askPass=",
        "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
        "-c", "protocol.https.allow=always", *args,
    ]
    if cwd_fd is not None:
        # macOS cannot chdir through /dev/fd. A fresh isolated child pins cwd before
        # exec, avoiding Python's thread-unsafe preexec_fn and all path re-resolution.
        command = [
            sys.executable, "-I", "-S", "-c",
            "import os,sys; fd=int(sys.argv[1]); os.fchdir(fd); "
            "os.set_inheritable(fd,False); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)",
            str(cwd_fd), *command,
        ]
    completed = subprocess.run(
        command, pass_fds=() if cwd_fd is None else (cwd_fd,),
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=30, check=False,
    )
    if completed.returncode not in ok_codes:
        raise subprocess.CalledProcessError(completed.returncode, ["git"])
    return completed.stdout.strip()


def resolve_repo_path(
    requirement: RepoRequirement,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    process_env = os.environ if env is None else env
    if requirement.env and process_env.get(requirement.env):
        return Path(str(process_env[requirement.env])).expanduser()
    if requirement.path:
        return Path(requirement.path).expanduser()
    if requirement.default_path:
        return _cache_overridden_path(requirement.default_path, env=process_env)
    cache_root = Path(process_env.get("WRR_CACHE_DIR") or "~/.cache/wrr").expanduser()
    return cache_root / "deps" / requirement.name


def _cache_overridden_path(raw_path: str, *, env: Mapping[str, str]) -> Path:
    expanded = Path(raw_path).expanduser()
    override = env.get("WRR_CACHE_DIR")
    if not override:
        return expanded
    raw = str(raw_path)
    if raw.startswith("~/.cache/wrr/"):
        suffix = raw.removeprefix("~/.cache/wrr/")
        return Path(override).expanduser() / suffix
    if raw == "~/.cache/wrr":
        return Path(override).expanduser()
    return expanded


def _summarize(results: Sequence[ManagedRepoResult], errors: Sequence[str]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return {
        "status": "fail" if errors or counts.get("failed") or counts.get("refused") else "ok",
        "repos": len(results),
        **{status: counts.get(status, 0) for status in ("planned", "downloaded", "refused", "failed")},
        "activated": 0,
    }
