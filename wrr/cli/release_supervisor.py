"""Finite, externally authorized release selection for an isolated WRR deployment.

Candidate preparation is owned by :mod:`wrr.cli.update`.  This module only
selects already-verified standalone releases.  It deliberately has no daemon,
discovery, package-management, migration, or consumer-command execution path.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from wrr.cli.update import _git, _source, _target_state


PRE_ACTIVATION_CHECKS = frozenset({
    "candidate_git_safety",
    "targeted_tests",
    "full_unit_suite",
    "finite_local_git_smoke",
    "independent_review",
    "controlled_omp_consumer",
    "controlled_hermes_consumer",
})
REAL_HOST_PRE_ACTIVATION_CHECKS = frozenset({"actual_omp_candidate", "actual_hermes_candidate"})
POST_ACTIVATION_CHECKS = frozenset({"post_switch_omp", "post_switch_hermes"})
OPERATIONS = frozenset({"activate", "finalize", "rollback", "recover"})


class SupervisorRefusal(RuntimeError):
    """Stable fail-closed refusal from the release supervisor."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _refuse(code: str) -> None:
    raise SupervisorRefusal(code)


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _regular_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError:
        _refuse("input_file_unavailable")
    if not stat.S_ISREG(info.st_mode):
        _refuse("regular_file_required")
    return info


def _single_link_regular(path: Path, code: str) -> os.stat_result:
    info = _regular_file(path)
    if info.st_nlink != 1:
        _refuse(code)
    return info


def _read_bytes(path: Path) -> bytes:
    before = _regular_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _refuse("input_file_unavailable")
    try:
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            _refuse("input_identity_changed")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(_read_bytes(path))


def _protected_file_sha256(
    path: Path,
    *,
    hard_link_code: str = "hard_link_input_refused",
    identity_code: str = "input_identity_changed",
) -> tuple[str, os.stat_result]:
    before = _single_link_regular(path, hard_link_code)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _refuse(identity_code)
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if opened.st_nlink > 1:
            _refuse(hard_link_code)
        if opened.st_nlink != 1:
            _refuse(identity_code)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            _refuse(identity_code)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        finished = os.fstat(descriptor)
        if finished.st_nlink > 1:
            _refuse(hard_link_code)
        if finished.st_nlink != 1:
            _refuse(identity_code)
        if (
            not stat.S_ISREG(finished.st_mode)
            or (finished.st_dev, finished.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            _refuse(identity_code)
    finally:
        os.close(descriptor)
    try:
        current = path.lstat()
    except OSError:
        _refuse(identity_code)
    if current.st_nlink > 1:
        _refuse(hard_link_code)
    if current.st_nlink != 1:
        _refuse(identity_code)
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino)
        != (opened.st_dev, opened.st_ino)
    ):
        _refuse(identity_code)
    return digest.hexdigest(), current


def _capture_authorization_identity(
    path: Path, expected_sha256: str,
) -> dict[str, Any]:
    actual_sha256, info = _protected_file_sha256(
        path,
        hard_link_code="authorization_hard_link_refused",
        identity_code="authorization_identity_changed",
    )
    try:
        canonical = path.resolve(strict=True)
    except OSError:
        _refuse("authorization_identity_changed")
    if actual_sha256 != expected_sha256:
        _refuse("authorization_digest_mismatch")
    return {
        "path": str(path),
        "canonical_path": str(canonical),
        "st_dev": info.st_dev,
        "st_ino": info.st_ino,
        "sha256": expected_sha256,
    }


def _assert_authorization_unchanged(identity: Mapping[str, Any]) -> None:
    path = Path(identity["path"])
    actual_sha256, info = _protected_file_sha256(
        path,
        hard_link_code="authorization_hard_link_refused",
        identity_code="authorization_identity_changed",
    )
    try:
        canonical = path.resolve(strict=True)
    except OSError:
        _refuse("authorization_identity_changed")
    if (
        (info.st_dev, info.st_ino)
        != (identity.get("st_dev"), identity.get("st_ino"))
        or str(canonical) != identity.get("canonical_path")
    ):
        _refuse("authorization_identity_changed")
    if actual_sha256 != identity.get("sha256"):
        _refuse("authorization_digest_mismatch")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_bytes(path))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _refuse("invalid_json_input")
    if not isinstance(value, dict):
        _refuse("json_object_required")
    return value


def _absolute(value: object, code: str = "absolute_path_required") -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        _refuse(code)
    path = Path(value)
    if not path.is_absolute():
        _refuse(code)
    return path


def _same_mapping(left: object, right: object) -> bool:
    return isinstance(left, Mapping) and isinstance(right, Mapping) and dict(left) == dict(right)




def _validate_private_root(root_identity: object) -> Path:
    if not isinstance(root_identity, Mapping):
        _refuse("deployment_root_invalid")
    path = _absolute(root_identity.get("path"), "deployment_root_invalid")
    try:
        canonical = path.resolve(strict=True)
        info = path.lstat()
    except OSError:
        _refuse("deployment_root_unavailable")
    if canonical != path:
        _refuse("deployment_root_noncanonical")
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _refuse("deployment_root_not_real_directory")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        _refuse("deployment_root_not_private")
    if root_identity.get("st_dev") != info.st_dev or root_identity.get("st_ino") != info.st_ino:
        _refuse("deployment_root_identity_mismatch")
    return path


def _validate_child_directory(path: Path, parent: Path, code: str) -> None:
    try:
        info = path.lstat()
    except OSError:
        _refuse(code)
    if path.parent != parent or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _refuse(code)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        _refuse(code)

def _open_root(root: Path, root_identity: Mapping[str, Any]) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError:
        _refuse("deployment_root_unavailable")
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_dev != root_identity.get("st_dev")
        or info.st_ino != root_identity.get("st_ino")
    ):
        os.close(descriptor)
        _refuse("deployment_root_identity_mismatch")
    return descriptor


def _root_name(root: Path, path: str | Path) -> str:
    child = Path(path)
    if child.parent != root or child.name in {"", ".", ".."}:
        _refuse("write_outside_deployment_root")
    return child.name


def _write_exclusive_json_at(root_fd: int, root: Path, path: str | Path, value: object) -> None:
    payload = _canonical_json(value)
    name = _root_name(root, path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=root_fd)
    except FileExistsError:
        _refuse("output_already_exists")
    except OSError:
        _refuse("output_create_failed")
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(root_fd)


def _write_atomic_json_at(
    root_fd: int,
    root: Path,
    path: str | Path,
    temporary: str | Path,
    value: object,
) -> None:
    destination_name = _root_name(root, path)
    temporary_name = _root_name(root, temporary)
    _write_exclusive_json_at(root_fd, root, temporary, value)
    try:
        os.replace(
            temporary_name,
            destination_name,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        os.fsync(root_fd)
    except OSError:
        _refuse("atomic_state_write_failed")


def _assert_root_file_unchanged(
    root_fd: int,
    name: str,
    descriptor: int,
    opened: os.stat_result,
) -> None:
    finished = os.fstat(descriptor)
    if finished.st_nlink > 1:
        _refuse("hard_link_input_refused")
    if finished.st_nlink != 1:
        _refuse("input_identity_changed")
    if (
        not stat.S_ISREG(finished.st_mode)
        or (finished.st_dev, finished.st_ino)
        != (opened.st_dev, opened.st_ino)
    ):
        _refuse("input_identity_changed")
    try:
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        _refuse("input_identity_changed")
    if current.st_nlink > 1:
        _refuse("hard_link_input_refused")
    if current.st_nlink != 1:
        _refuse("input_identity_changed")
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino)
        != (opened.st_dev, opened.st_ino)
    ):
        _refuse("input_identity_changed")


def _load_json_at(root_fd: int, root: Path, path: str | Path) -> dict[str, Any]:
    name = _root_name(root, path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=root_fd)
    except OSError:
        _refuse("input_file_unavailable")
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            _refuse("regular_file_required")
        if info.st_nlink > 1:
            _refuse("hard_link_input_refused")
        if info.st_nlink != 1:
            _refuse("input_identity_changed")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        _assert_root_file_unchanged(root_fd, name, descriptor, info)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _refuse("invalid_json_input")
    if not isinstance(value, dict):
        _refuse("json_object_required")
    return value


def _sha256_at(root_fd: int, root: Path, path: str | Path) -> str:
    name = _root_name(root, path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=root_fd)
    except OSError:
        _refuse("input_file_unavailable")
    digest = hashlib.sha256()
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            _refuse("regular_file_required")
        if info.st_nlink > 1:
            _refuse("hard_link_input_refused")
        if info.st_nlink != 1:
            _refuse("input_identity_changed")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        _assert_root_file_unchanged(root_fd, name, descriptor, info)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _active_target_at(root_fd: int) -> str:
    try:
        target = os.readlink("active", dir_fd=root_fd)
    except OSError:
        _refuse("active_link_unavailable")
    if not os.path.isabs(target):
        _refuse("active_target_must_be_absolute")
    return str(Path(target))


def _exists_at(root_fd: int, root: Path, path: str | Path) -> bool:
    name = _root_name(root, path)
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        _refuse("deployment_entry_inspection_failed")


def _manifest_entry(path: Path, relative: str) -> dict[str, Any]:
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode):
        return {"path": relative, "type": "directory", "mode": mode}
    if stat.S_ISREG(info.st_mode):
        return {
            "path": relative,
            "type": "file",
            "mode": mode,
            "sha256": _file_sha256(path),
        }
    if stat.S_ISLNK(info.st_mode):
        return {
            "path": relative,
            "type": "symlink",
            "mode": mode,
            "target": os.readlink(path),
        }
    _refuse("release_special_file_refused")


def tree_digest(root: str | Path) -> str:
    """Digest release content without following links or including top-level .git."""
    root_path = Path(root)
    entries: list[dict[str, Any]] = []
    for current, directories, files in os.walk(root_path, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_parent = current_path.relative_to(root_path)
        kept_directories = []
        for name in sorted(directories, key=os.fsencode):
            relative = (relative_parent / name).as_posix()
            child = current_path / name
            if relative == ".git":
                continue
            if name == ".git":
                _refuse("nested_git_entry_refused")
            entries.append(_manifest_entry(child, relative))
            if not child.is_symlink():
                kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(files, key=os.fsencode):
            relative = (relative_parent / name).as_posix()
            if name == ".git":
                _refuse("nested_git_entry_refused")
            entries.append(_manifest_entry(current_path / name, relative))
    entries.sort(key=lambda item: os.fsencode(item["path"]))
    return _sha256_bytes(json.dumps(
        entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode())


def _package_version(path: Path) -> str:
    try:
        project = tomllib.loads(_read_bytes(path / "pyproject.toml").decode())["project"]
        version = project["version"]
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError):
        _refuse("release_version_unavailable")
    if not isinstance(version, str) or not version:
        _refuse("release_version_unavailable")
    return version


def release_identity(path: str | Path, *, expected_origin: str) -> dict[str, Any]:
    """Read the exact identity of one safe standalone release repository."""
    try:
        release = Path(path).resolve(strict=True)
    except OSError:
        _refuse("release_directory_invalid")
    normalized_origin = _source(expected_origin)
    if normalized_origin is None:
        _refuse("release_origin_invalid")
    reason, revision = _target_state(release, normalized_origin)
    if reason or revision is None:
        _refuse(f"release_git_admission_failed:{reason or 'revision_unavailable'}")
    origins = _git("-C", str(release), "config", "--get-all", "remote.origin.url").splitlines()
    if len(origins) != 1:
        _refuse("release_git_admission_failed:origin_count")
    actual_origin = _source(origins[0])
    if actual_origin != normalized_origin:
        _refuse("release_git_admission_failed:origin_mismatch")
    return {
        "path": str(release.resolve()),
        "origin": actual_origin,
        "revision": revision,
        "version": _package_version(release),
        "tree_sha256": tree_digest(release),
    }


def _verify_digest_reference(reference: object, authorization_inputs: Mapping[str, str]) -> Path:
    if not isinstance(reference, Mapping):
        _refuse("digest_reference_required")
    path = _absolute(reference.get("path"), "digest_reference_invalid")
    digest = reference.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        _refuse("digest_reference_invalid")
    if authorization_inputs.get(str(path)) != digest:
        _refuse("unauthorized_input")
    if _protected_file_sha256(path)[0] != digest:
        _refuse("authorized_input_digest_mismatch")
    return path


def _authorization_inputs(
    authorization: Mapping[str, Any],
    *,
    authorization_path: Path,
    operation_path: Path,
) -> dict[str, str]:
    entries = authorization.get("inputs")
    if not isinstance(entries, list) or not entries:
        _refuse("authorization_inputs_required")
    result: dict[str, str] = {}
    identities = {(authorization_path.lstat().st_dev, authorization_path.lstat().st_ino)}
    for entry in entries:
        if not isinstance(entry, Mapping):
            _refuse("authorization_input_invalid")
        path = _absolute(entry.get("path"), "authorization_input_invalid")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or str(path) in result:
            _refuse("authorization_input_invalid")
        actual_digest, info = _protected_file_sha256(path)
        identity = (info.st_dev, info.st_ino)
        if identity in identities:
            _refuse("input_path_alias_refused")
        identities.add(identity)
        if actual_digest != digest:
            _refuse("authorized_input_digest_mismatch")
        result[str(path)] = digest
    if result.get(str(operation_path)) != _protected_file_sha256(operation_path)[0]:
        _refuse("operation_input_not_authorized")
    return result


def _parse_expiry(value: object) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        _refuse("authorization_expiry_invalid")
    try:
        expiry = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _refuse("authorization_expiry_invalid")
    if expiry <= datetime.now(timezone.utc):
        _refuse("authorization_expired")


def _expected_paths(root: Path, transaction_id: str, output: Path) -> dict[str, str]:
    return {
        "active_link": str(root / "active"),
        "releases_root": str(root / "releases"),
        "lock": str(root / ".wrr-release.lock"),
        "generation": str(root / ".wrr-release-generation.json"),
        "generation_tmp": str(root / f".wrr-release-generation.{transaction_id}.tmp"),
        "journal": str(root / ".wrr-release-journal.json"),
        "journal_tmp": str(root / f".wrr-release-journal.{transaction_id}.tmp"),
        "journal_guard": str(root / ".wrr-release-journal.guard.json"),
        "active_tmp": str(root / f".active.{transaction_id}.tmp"),
        "output_receipt": str(output),
    }


def _expected_writes(
    paths: Mapping[str, str], operation: str, cleanup_paths: Sequence[str] = (),
) -> list[str]:
    names = ["output_receipt"]
    if operation in {"activate", "rollback", "recover"}:
        names += [
            "active_link", "active_tmp", "journal", "journal_guard",
            "journal_tmp", "generation", "generation_tmp",
        ]
    return sorted({*(paths[name] for name in names), *cleanup_paths})


def _validate_paths(
    plan: Mapping[str, Any], authorization: Mapping[str, Any], root: Path, operation: str,
) -> dict[str, str]:
    transaction_id = plan.get("transaction_id")
    if not isinstance(transaction_id, str) or not transaction_id or "/" in transaction_id or "\x00" in transaction_id:
        _refuse("transaction_id_invalid")
    output = _absolute(plan.get("output_receipt"), "output_path_invalid")
    expected = _expected_paths(root, transaction_id, output)
    paths = plan.get("paths")
    if not isinstance(paths, Mapping) or dict(paths) != expected:
        _refuse("operation_paths_mismatch")
    if len(set(expected.values())) != len(expected):
        _refuse("path_role_alias")
    auth_paths = authorization.get("paths")
    stable_paths = {
        name: expected[name]
        for name in (
            "active_link", "releases_root", "lock", "generation", "journal",
            "journal_guard",
        )
    }
    if not isinstance(auth_paths, Mapping) or dict(auth_paths) != stable_paths:
        _refuse("authorization_paths_mismatch")
    cleanup = plan.get("cleanup_paths", ())
    if operation == "recover":
        if (
            not isinstance(cleanup, list)
            or not all(isinstance(value, str) for value in cleanup)
            or len(cleanup) != len(set(cleanup))
        ):
            _refuse("recovery_cleanup_paths_invalid")
        for value in cleanup:
            cleanup_path = _absolute(value, "recovery_cleanup_paths_invalid")
            if cleanup_path.parent != root or value in expected.values():
                _refuse("recovery_cleanup_paths_invalid")
    elif cleanup:
        _refuse("recovery_cleanup_paths_invalid")
    writes = authorization.get("allowed_writes")
    expected_writes = _expected_writes(expected, operation, cleanup)
    if (
        not isinstance(writes, list)
        or not all(isinstance(value, str) for value in writes)
        or len(writes) != len(set(writes))
        or sorted(writes) != expected_writes
    ):
        _refuse("allowed_writes_mismatch")
    if any(Path(value).parent != root for name, value in expected.items() if name != "releases_root"):
        _refuse("write_outside_deployment_root")
    if Path(expected["releases_root"]) != root / "releases":
        _refuse("releases_root_mismatch")
    for name in ("output_receipt", "active_tmp", "journal_tmp", "generation_tmp"):
        if os.path.lexists(expected[name]):
            _refuse("output_already_exists")
    return expected

def _validate_input_write_separation(
    *,
    inputs: Mapping[str, str],
    authorization_path: Path,
    writes: Sequence[str],
    operation: str,
    journal_path: str,
) -> None:
    protected_inputs = [authorization_path, *(Path(path) for path in inputs)]
    write_paths = [Path(path) for path in writes]
    for input_path in protected_inputs:
        for write_path in write_paths:
            record_overlap = (
                operation == "recover"
                and str(input_path) in {journal_path, str(write_path)}
                and str(input_path) == str(write_path)
                and str(input_path).endswith(
                    (".wrr-release-journal.json", ".wrr-release-journal.guard.json")
                )
            )
            if input_path == write_path and not record_overlap:
                _refuse("input_write_role_alias")
            if record_overlap or not os.path.lexists(write_path):
                continue
            try:
                input_info = input_path.lstat()
                write_info = write_path.lstat()
            except OSError:
                _refuse("path_identity_inspection_failed")
            if (
                stat.S_ISREG(input_info.st_mode)
                and stat.S_ISREG(write_info.st_mode)
                and (input_info.st_dev, input_info.st_ino)
                == (write_info.st_dev, write_info.st_ino)
            ):
                _refuse("input_write_role_alias")


def _validate_input_release_separation(
    inputs: Sequence[str | Path], releases_root: str,
) -> None:
    try:
        canonical_releases = Path(releases_root).resolve(strict=True)
    except OSError:
        _refuse("releases_root_invalid")
    for value in inputs:
        try:
            canonical = Path(value).resolve(strict=True)
        except OSError:
            _refuse("input_file_unavailable")
        if canonical.is_relative_to(canonical_releases):
            _refuse("input_inside_release")




def _validate_state(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version",
        "generation",
        "last_transaction_id",
        "active_target",
        "committed_operation",
        "committed_receipt_path",
        "committed_receipt_sha256",
    }
    if set(value) != expected or value.get("schema_version") != 1:
        _refuse("generation_state_invalid")
    generation = value.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or not isinstance(value.get("active_target"), str)
    ):
        _refuse("generation_state_invalid")
    last = value.get("last_transaction_id")
    operation = value.get("committed_operation")
    receipt_path = value.get("committed_receipt_path")
    receipt_sha256 = value.get("committed_receipt_sha256")
    if generation == 0:
        if any(item is not None for item in (last, operation, receipt_path, receipt_sha256)):
            _refuse("generation_state_invalid")
    elif (
        not isinstance(last, str)
        or not isinstance(operation, str)
        or operation not in {"activate", "rollback", "recover"}
        or not isinstance(receipt_path, str)
        or not Path(receipt_path).is_absolute()
        or not isinstance(receipt_sha256, str)
        or len(receipt_sha256) != 64
    ):
        _refuse("generation_state_invalid")
    return dict(value)


def _state_at(root_fd: int, root: Path, path: str | Path) -> dict[str, Any]:
    value = _validate_state(_load_json_at(root_fd, root, path))
    receipt_path = value["committed_receipt_path"]
    if receipt_path is not None and Path(receipt_path).parent != root:
        _refuse("generation_state_invalid")
    return value


def _validate_configs(
    plan: Mapping[str, Any],
    inputs: Mapping[str, str],
    releases_root: Path,
) -> dict[str, str]:
    configs = plan.get("configs")
    if not isinstance(configs, list):
        _refuse("config_list_invalid")
    try:
        canonical_releases = releases_root.resolve(strict=True)
    except OSError:
        _refuse("releases_root_invalid")
    result: dict[str, str] = {}
    identities: set[tuple[int, int]] = set()
    for reference in configs:
        path = _verify_digest_reference(reference, inputs)
        try:
            canonical = path.resolve(strict=True)
            info = path.stat()
        except OSError:
            _refuse("config_path_invalid")
        if canonical.is_relative_to(canonical_releases):
            _refuse("config_inside_release")
        identity = (info.st_dev, info.st_ino)
        if str(path) in result or identity in identities:
            _refuse("config_list_invalid")
        identities.add(identity)
        result[str(path)] = reference["sha256"]
    return result


def _validate_release(expected: object, releases_root: Path) -> dict[str, Any]:
    if not isinstance(expected, Mapping):
        _refuse("release_identity_invalid")
    path = _absolute(expected.get("path"), "release_identity_invalid")
    if path.parent != releases_root:
        _refuse("release_outside_releases_root")
    _validate_child_directory(path, releases_root, "release_directory_invalid")
    origin = expected.get("origin")
    if not isinstance(origin, str):
        _refuse("release_identity_invalid")
    actual = release_identity(path, expected_origin=origin)
    if actual != dict(expected):
        _refuse("release_identity_mismatch")
    return actual


def _validate_check_receipt(
    path: Path,
    *,
    inputs: Mapping[str, str],
    evidence_kind: str,
    candidate: Mapping[str, Any],
    post: bool,
    activation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    receipt = _load_json(path)
    if receipt.get("schema_version") != 1:
        _refuse("verification_schema_invalid")
    if receipt.get("evidence_kind") != evidence_kind:
        _refuse("evidence_kind_mismatch")
    producer = "fixture_harness" if evidence_kind == "isolated_fixture" else "real_host_matrix"
    if receipt.get("producer") != producer:
        _refuse("verification_producer_mismatch")
    if not _same_mapping(receipt.get("candidate"), candidate):
        _refuse("verification_candidate_mismatch")
    expected_ids = set(POST_ACTIVATION_CHECKS if post else PRE_ACTIVATION_CHECKS)
    if not post and evidence_kind == "real_host":
        expected_ids.update(REAL_HOST_PRE_ACTIVATION_CHECKS)
    checks = receipt.get("checks")
    if not isinstance(checks, list) or not checks:
        _refuse("verification_checks_mismatch")
    identifiers = [check.get("id") for check in checks if isinstance(check, Mapping)]
    if (
        len(identifiers) != len(checks)
        or not all(isinstance(identifier, str) for identifier in identifiers)
        or len(set(identifiers)) != len(identifiers)
        or set(identifiers) != expected_ids
    ):
        _refuse("verification_checks_mismatch")
    for check in checks:
        if check.get("verdict") != "PASS":
            _refuse("verification_check_not_pass")
        evidence = {
            "path": check.get("evidence_path"),
            "sha256": check.get("evidence_sha256"),
        }
        _verify_digest_reference(evidence, inputs)
    consumers = receipt.get("consumer_verdicts")
    if consumers != {"omp": "PASS", "hermes": "PASS"}:
        _refuse("verification_consumer_not_pass")
    if post:
        if activation is None:
            _refuse("activation_receipt_required")
        if receipt.get("activation_transaction_id") != activation.get("transaction_id"):
            _refuse("post_verification_transaction_mismatch")
        if receipt.get("activation_generation") != activation.get("generation_after"):
            _refuse("post_verification_generation_mismatch")
    return receipt


def _validate_impact(path: Path, *, inputs: Mapping[str, str], evidence_kind: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
    receipt = _load_json(path)
    expected_fields = {
        "schema_version", "producer", "evidence_kind", "candidate_revision",
        "candidate_tree_sha256", "config_schema_change", "state_schema_change",
        "shared_resource_change", "requires_migration", "rollback_compatible",
        "evidence",
    }
    if set(receipt) != expected_fields or receipt.get("schema_version") != 1 or receipt.get("producer") != "release_impact_review":
        _refuse("impact_receipt_invalid")
    if receipt.get("evidence_kind") != evidence_kind:
        _refuse("impact_evidence_kind_mismatch")
    if receipt.get("candidate_revision") != candidate.get("revision") or receipt.get("candidate_tree_sha256") != candidate.get("tree_sha256"):
        _refuse("impact_candidate_mismatch")
    required_false = ("config_schema_change", "state_schema_change", "shared_resource_change")
    for field in required_false:
        if field not in receipt or receipt[field] is not False:
            _refuse("candidate_impact_unknown_or_changed")
    if receipt.get("requires_migration") is not False:
        _refuse("candidate_requires_migration")
    if receipt.get("rollback_compatible") is not True:
        _refuse("candidate_not_rollback_compatible")
    evidence = receipt.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        _refuse("impact_evidence_required")
    for reference in evidence:
        _verify_digest_reference(reference, inputs)
    return receipt


def _validate_common(
    operation: str,
    operation_path: Path,
    authorization_path: Path,
    authorization_sha256: str,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, str],
    dict[str, Any],
    Path,
    dict[str, str],
]:
    if operation not in OPERATIONS:
        _refuse("operation_invalid")
    if not isinstance(authorization_sha256, str) or len(authorization_sha256) != 64:
        _refuse("authorization_digest_mismatch")
    authorization_identity = _capture_authorization_identity(
        authorization_path, authorization_sha256,
    )
    authorization = _load_json(authorization_path)
    _assert_authorization_unchanged(authorization_identity)
    plan = _load_json(operation_path)
    if authorization.get("schema_version") != 1 or plan.get("schema_version") != 1:
        _refuse("schema_version_invalid")
    if authorization.get("action") != operation or plan.get("operation") != operation:
        _refuse("authorization_action_mismatch")
    if authorization.get("transaction_id") != plan.get("transaction_id"):
        _refuse("authorization_transaction_mismatch")
    if authorization.get("authorization_id") != plan.get("authorization_id"):
        _refuse("authorization_id_mismatch")
    _parse_expiry(authorization.get("expires_at"))
    inputs = _authorization_inputs(
        authorization,
        authorization_path=authorization_path,
        operation_path=operation_path,
    )
    root = _validate_private_root(authorization.get("deployment_root", {}))
    if not _same_mapping(
        plan.get("deployment_root"), authorization.get("deployment_root"),
    ):
        _refuse("deployment_root_plan_mismatch")
    if plan.get("expected_generation") != authorization.get("expected_generation"):
        _refuse("expected_generation_mismatch")
    if (
        not _same_mapping(plan.get("before"), authorization.get("before"))
        or not _same_mapping(plan.get("after"), authorization.get("after"))
    ):
        _refuse("authorized_release_identity_mismatch")
    evidence_kind = plan.get("evidence_kind")
    authorized_evidence_kind = authorization.get("evidence_kind")
    if (
        not isinstance(evidence_kind, str)
        or not isinstance(authorized_evidence_kind, str)
        or evidence_kind not in {"isolated_fixture", "real_host"}
        or authorized_evidence_kind not in {"isolated_fixture", "real_host"}
    ):
        _refuse("evidence_kind_invalid")
    if evidence_kind != authorized_evidence_kind:
        _refuse("authorized_evidence_kind_mismatch")
    paths = _validate_paths(plan, authorization, root, operation)
    _validate_input_release_separation(
        [authorization_path, *inputs], paths["releases_root"],
    )
    _validate_input_write_separation(
        inputs=inputs,
        authorization_path=authorization_path,
        writes=authorization["allowed_writes"],
        operation=operation,
        journal_path=paths["journal"],
    )
    return (
        plan,
        authorization,
        inputs,
        authorization_identity,
        root,
        paths,
    )


def _open_lock(root_fd: int) -> int:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(".wrr-release.lock", flags, dir_fd=root_fd)
    except OSError:
        _refuse("supervisor_lock_invalid")
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        os.close(descriptor)
        _refuse("supervisor_lock_invalid")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        _refuse("supervisor_lock_busy")
    return descriptor


def _fault(callback: Callable[[str], None] | None, boundary: str) -> None:
    if callback is not None:
        callback(boundary)


def _assert_inputs_unchanged(inputs: Mapping[str, str]) -> None:
    for path, digest in inputs.items():
        if _protected_file_sha256(Path(path))[0] != digest:
            _refuse("authorized_input_digest_mismatch")


def _create_temporary_link(
    root_fd: int, root: Path, path: str | Path, target: str,
) -> None:
    try:
        os.symlink(target, _root_name(root, path), dir_fd=root_fd)
        os.fsync(root_fd)
    except FileExistsError:
        _refuse("transaction_temporary_exists")
    except OSError:
        _refuse("temporary_link_create_failed")


def _journal_update(
    root_fd: int,
    root: Path,
    paths: Mapping[str, str],
    journal: dict[str, Any],
    phase: str,
) -> None:
    journal["phase"] = phase
    _write_atomic_json_at(
        root_fd, root, paths["journal"], paths["journal_tmp"], journal,
    )


def _assert_deployment_invariants(
    *,
    root_fd: int,
    root_identity: Mapping[str, Any],
    authorization_identity: Mapping[str, Any],
    inputs: Mapping[str, str],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    configs: Mapping[str, str],
    expected_active: str,
) -> None:
    _validate_private_root(root_identity)
    _assert_authorization_unchanged(authorization_identity)
    if _active_target_at(root_fd) != expected_active:
        _refuse("deployment_drift")
    _assert_inputs_unchanged(inputs)
    try:
        before_actual = release_identity(
            before["path"], expected_origin=before["origin"],
        )
        after_actual = release_identity(
            after["path"], expected_origin=after["origin"],
        )
    except SupervisorRefusal:
        _refuse("release_identity_drift")
    if before_actual != dict(before) or after_actual != dict(after):
        _refuse("release_identity_drift")
    for path, digest in configs.items():
        if _protected_file_sha256(Path(path))[0] != digest:
            _refuse("config_digest_drift")


def _retire_transaction_record(
    root_fd: int,
    root: Path,
    paths: Mapping[str, str],
    fault_hook: Callable[[str], None] | None,
) -> tuple[str, bool]:
    try:
        os.replace(
            _root_name(root, paths["journal"]),
            _root_name(root, paths["journal_guard"]),
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        _fault(fault_hook, "journal_guard_replaced")
        os.fsync(root_fd)
        _fault(fault_hook, "journal_guard_persisted")
    except OSError:
        _refuse("transaction_committed_recovery_required")
    try:
        os.unlink(_root_name(root, paths["journal_guard"]), dir_fd=root_fd)
        _fault(fault_hook, "journal_guard_unlinked")
    except OSError:
        _refuse("transaction_committed_recovery_required")
    try:
        os.fsync(root_fd)
    except OSError:
        return "durability_uncertain", False
    _fault(fault_hook, "journal_guard_cleanup_persisted")
    return "durable", True


def _transactional_switch(
    *,
    operation: str,
    plan: Mapping[str, Any],
    plan_sha256: str,
    authorization_sha256: str,
    authorization_identity: Mapping[str, Any],
    paths: Mapping[str, str],
    state: Mapping[str, Any],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    configs: Mapping[str, str],
    inputs: Mapping[str, str],
    receipt_fields: Mapping[str, Any],
    fault_hook: Callable[[str], None] | None,
    root_fd: int,
) -> dict[str, Any]:
    root = Path(plan["deployment_root"]["path"])
    generation_before = state["generation"]
    generation_after = generation_before + 1
    transaction_id = plan["transaction_id"]
    receipt_path = Path(paths["output_receipt"])
    journal = {
        "schema_version": 1,
        "operation": operation,
        "transaction_id": transaction_id,
        "phase": "prepared",
        "generation_before": generation_before,
        "generation_after": generation_after,
        "before": dict(before),
        "after": dict(after),
        "config_hashes": dict(configs),
        "plan_sha256": plan_sha256,
        "authorization_sha256": authorization_sha256,
        "receipt_path": str(receipt_path),
        "paths": dict(paths),
    }
    _assert_authorization_unchanged(authorization_identity)
    _write_exclusive_json_at(root_fd, root, paths["journal"], journal)
    _fault(fault_hook, "journal_prepared")
    _validate_private_root(plan["deployment_root"])
    _create_temporary_link(root_fd, root, paths["active_tmp"], after["path"])
    _fault(fault_hook, "temporary_link_persisted")
    _assert_deployment_invariants(
        root_fd=root_fd,
        root_identity=plan["deployment_root"],
        authorization_identity=authorization_identity,
        inputs=inputs,
        before=before,
        after=after,
        configs=configs,
        expected_active=before["path"],
    )
    try:
        os.replace(
            _root_name(root, paths["active_tmp"]),
            _root_name(root, paths["active_link"]),
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        os.fsync(root_fd)
    except OSError:
        _refuse("active_replace_failed")
    _fault(fault_hook, "active_replaced")
    _journal_update(root_fd, root, paths, journal, "switched")
    _fault(fault_hook, "journal_switched")
    _assert_deployment_invariants(
        root_fd=root_fd,
        root_identity=plan["deployment_root"],
        authorization_identity=authorization_identity,
        inputs=inputs,
        before=before,
        after=after,
        configs=configs,
        expected_active=after["path"],
    )
    receipt = {
        "schema_version": 1,
        "operation": operation,
        "transaction_id": transaction_id,
        "plan_sha256": plan_sha256,
        "authorization_sha256": authorization_sha256,
        "generation_before": generation_before,
        "generation_after": generation_after,
        "before": dict(before),
        "after": dict(after),
        "config_hashes": dict(configs),
        "journal_outcome": "receipt_persisted",
        **dict(receipt_fields),
    }
    _write_exclusive_json_at(root_fd, root, receipt_path, receipt)
    _fault(fault_hook, "receipt_persisted")
    _assert_deployment_invariants(
        root_fd=root_fd,
        root_identity=plan["deployment_root"],
        authorization_identity=authorization_identity,
        inputs=inputs,
        before=before,
        after=after,
        configs=configs,
        expected_active=after["path"],
    )
    receipt_sha256 = _sha256_bytes(_canonical_json(receipt))
    next_state = {
        "schema_version": 1,
        "generation": generation_after,
        "last_transaction_id": transaction_id,
        "active_target": after["path"],
        "committed_operation": operation,
        "committed_receipt_path": str(receipt_path),
        "committed_receipt_sha256": receipt_sha256,
    }
    _write_atomic_json_at(
        root_fd, root, paths["generation"], paths["generation_tmp"], next_state,
    )
    _fault(fault_hook, "generation_persisted")
    _assert_deployment_invariants(
        root_fd=root_fd,
        root_identity=plan["deployment_root"],
        authorization_identity=authorization_identity,
        inputs=inputs,
        before=before,
        after=after,
        configs=configs,
        expected_active=after["path"],
    )
    _journal_update(root_fd, root, paths, journal, "receipt_committed")
    _fault(fault_hook, "journal_committed")
    _assert_deployment_invariants(
        root_fd=root_fd,
        root_identity=plan["deployment_root"],
        authorization_identity=authorization_identity,
        inputs=inputs,
        before=before,
        after=after,
        configs=configs,
        expected_active=after["path"],
    )
    cleanup_status, cleanup_durable = _retire_transaction_record(
        root_fd, root, paths, fault_hook,
    )
    return {
        **receipt,
        "receipt_path": str(receipt_path),
        "cleanup_status": cleanup_status,
        "cleanup_durable": cleanup_durable,
    }


def _require_current_activation(
    activation: Mapping[str, Any],
    state: Mapping[str, Any],
    root_fd: int,
    *,
    receipt_path: Path,
    receipt_sha256: str,
) -> None:
    if state.get("generation") != activation.get("generation_after"):
        _refuse("activation_generation_not_current")
    if state.get("last_transaction_id") != activation.get("transaction_id"):
        _refuse("activation_transaction_not_current")
    if (
        state.get("committed_operation") != "activate"
        or state.get("committed_receipt_path") != str(receipt_path)
        or state.get("committed_receipt_sha256") != receipt_sha256
    ):
        _refuse("activation_receipt_not_state_bound")
    expected_fields = {
        "schema_version",
        "operation",
        "transaction_id",
        "plan_sha256",
        "authorization_sha256",
        "generation_before",
        "generation_after",
        "before",
        "after",
        "config_hashes",
        "journal_outcome",
        "status",
        "provisional",
        "fixture_only",
        "real_host_verified",
        "evidence_kind",
        "verification_sha256",
        "impact_sha256",
        "rollback_supported",
    }
    hash_fields = (
        "plan_sha256",
        "authorization_sha256",
        "verification_sha256",
        "impact_sha256",
    )
    generation_before = activation.get("generation_before")
    evidence_kind = activation.get("evidence_kind")
    if (
        set(activation) != expected_fields
        or activation.get("schema_version") != 1
        or activation.get("operation") != "activate"
        or activation.get("status") != "provisional"
        or activation.get("journal_outcome") != "receipt_persisted"
        or not isinstance(activation.get("transaction_id"), str)
        or not isinstance(generation_before, int)
        or isinstance(generation_before, bool)
        or activation.get("generation_after") != generation_before + 1
        or not isinstance(activation.get("before"), Mapping)
        or not isinstance(activation.get("after"), Mapping)
        or not isinstance(activation.get("config_hashes"), Mapping)
        or activation.get("provisional") is not True
        or not isinstance(activation.get("fixture_only"), bool)
        or activation.get("real_host_verified") is not False
        or evidence_kind not in {"isolated_fixture", "real_host"}
        or activation.get("fixture_only") != (evidence_kind == "isolated_fixture")
        or activation.get("rollback_supported") is not True
        or any(
            not isinstance(activation.get(field), str)
            or len(activation[field]) != 64
            for field in hash_fields
        )
    ):
        _refuse("activation_receipt_invalid")
    after = activation["after"]
    if _active_target_at(root_fd) != after.get("path"):
        _refuse("activation_target_not_current")


def _activate(
    plan: Mapping[str, Any], inputs: Mapping[str, str], paths: Mapping[str, str],
    state: Mapping[str, Any], plan_sha256: str, authorization_sha256: str,
    authorization_identity: Mapping[str, Any],
    fault_hook: Callable[[str], None] | None, root_fd: int,
) -> dict[str, Any]:
    releases = Path(paths["releases_root"])
    before = _validate_release(plan.get("before"), releases)
    after = _validate_release(plan.get("after"), releases)
    if before["path"] == after["path"]:
        _refuse("distinct_releases_required")
    if state["active_target"] != before["path"] or _active_target_at(root_fd) != before["path"]:
        _refuse("active_target_mismatch")
    evidence_kind = plan["evidence_kind"]
    verification_path = _verify_digest_reference(plan.get("verification"), inputs)
    _validate_check_receipt(
        verification_path, inputs=inputs, evidence_kind=evidence_kind, candidate=after, post=False,
    )
    impact_path = _verify_digest_reference(plan.get("impact"), inputs)
    _validate_impact(impact_path, inputs=inputs, evidence_kind=evidence_kind, candidate=after)
    configs = _validate_configs(plan, inputs, releases)
    if plan.get("rollback_supported") is not True:
        _refuse("rollback_support_required")
    return _transactional_switch(
        operation="activate",
        plan=plan,
        plan_sha256=plan_sha256,
        authorization_sha256=authorization_sha256,
        authorization_identity=authorization_identity,
        paths=paths,
        state=state,
        before=before,
        after=after,
        configs=configs,
        inputs=inputs,
        receipt_fields={
            "status": "provisional",
            "provisional": True,
            "fixture_only": evidence_kind == "isolated_fixture",
            "real_host_verified": False,
            "evidence_kind": evidence_kind,
            "verification_sha256": plan["verification"]["sha256"],
            "impact_sha256": plan["impact"]["sha256"],
            "rollback_supported": True,
        },
        fault_hook=fault_hook,
        root_fd=root_fd,
    )


def _finalize(
    plan: Mapping[str, Any], inputs: Mapping[str, str], paths: Mapping[str, str],
    state: Mapping[str, Any], plan_sha256: str, authorization_sha256: str,
    authorization_identity: Mapping[str, Any], root_fd: int,
) -> dict[str, Any]:
    activation_path = _verify_digest_reference(plan.get("activation_receipt"), inputs)
    root = Path(plan["deployment_root"]["path"])
    if activation_path.parent == root:
        if _sha256_at(root_fd, root, activation_path) != plan["activation_receipt"]["sha256"]:
            _refuse("authorized_input_digest_mismatch")
        activation = _load_json_at(root_fd, root, activation_path)
    else:
        activation = _load_json(activation_path)
    _require_current_activation(
        activation,
        state,
        root_fd,
        receipt_path=activation_path,
        receipt_sha256=plan["activation_receipt"]["sha256"],
    )
    if not _same_mapping(plan.get("before"), activation.get("after")) or not _same_mapping(plan.get("after"), activation.get("after")):
        _refuse("finalize_release_mismatch")
    releases = Path(paths["releases_root"])
    candidate = _validate_release(activation.get("after"), releases)
    configs = _validate_configs(plan, inputs, releases)
    if configs != activation.get("config_hashes"):
        _refuse("activation_config_mismatch")
    evidence_kind = plan["evidence_kind"]
    if evidence_kind != activation.get("evidence_kind"):
        _refuse("finalize_evidence_kind_mismatch")
    verification_path = _verify_digest_reference(plan.get("verification"), inputs)
    _validate_check_receipt(
        verification_path,
        inputs=inputs,
        evidence_kind=evidence_kind,
        candidate=candidate,
        post=True,
        activation=activation,
    )
    receipt = {
        "schema_version": 1,
        "operation": "finalize",
        "transaction_id": plan["transaction_id"],
        "plan_sha256": plan_sha256,
        "activation_receipt_sha256": plan["activation_receipt"]["sha256"],
        "activation_transaction_id": activation["transaction_id"],
        "activation_generation": activation["generation_after"],
        "authorization_sha256": authorization_sha256,
        "status": "finalized_fixture_only" if evidence_kind == "isolated_fixture" else "finalized_real_host",
        "fixture_only": evidence_kind == "isolated_fixture",
        "real_host_verified": evidence_kind == "real_host",
        "candidate": candidate,
        "config_hashes": configs,
        "verification_sha256": plan["verification"]["sha256"],
    }
    receipt_path = Path(paths["output_receipt"])
    _assert_authorization_unchanged(authorization_identity)
    _write_exclusive_json_at(root_fd, root, receipt_path, receipt)
    return {**receipt, "receipt_path": str(receipt_path)}


def _rollback(
    plan: Mapping[str, Any], inputs: Mapping[str, str], paths: Mapping[str, str],
    state: Mapping[str, Any], plan_sha256: str, authorization_sha256: str,
    authorization_identity: Mapping[str, Any],
    fault_hook: Callable[[str], None] | None, root_fd: int,
) -> dict[str, Any]:
    activation_path = _verify_digest_reference(plan.get("activation_receipt"), inputs)
    root = Path(plan["deployment_root"]["path"])
    if activation_path.parent == root:
        if _sha256_at(root_fd, root, activation_path) != plan["activation_receipt"]["sha256"]:
            _refuse("authorized_input_digest_mismatch")
        activation = _load_json_at(root_fd, root, activation_path)
    else:
        activation = _load_json(activation_path)
    _require_current_activation(
        activation,
        state,
        root_fd,
        receipt_path=activation_path,
        receipt_sha256=plan["activation_receipt"]["sha256"],
    )
    if activation.get("rollback_supported") is not True:
        _refuse("rollback_not_supported")
    if not _same_mapping(plan.get("before"), activation.get("after")) or not _same_mapping(plan.get("after"), activation.get("before")):
        _refuse("rollback_release_mismatch")
    releases = Path(paths["releases_root"])
    before = _validate_release(activation.get("after"), releases)
    after = _validate_release(activation.get("before"), releases)
    configs = _validate_configs(plan, inputs, releases)
    if configs != activation.get("config_hashes"):
        _refuse("activation_config_mismatch")
    return _transactional_switch(
        operation="rollback",
        plan=plan,
        plan_sha256=plan_sha256,
        authorization_sha256=authorization_sha256,
        authorization_identity=authorization_identity,
        paths=paths,
        state=state,
        before=before,
        after=after,
        configs=configs,
        inputs=inputs,
        receipt_fields={
            "status": "rolled_back",
            "activation_transaction_id": activation["transaction_id"],
            "rolled_back": True,
        },
        fault_hook=fault_hook,
        root_fd=root_fd,
    )


def _mark_manual_recovery(
    root_fd: int,
    root: Path,
    paths: Mapping[str, str],
    journal: dict[str, Any],
    code: str = "manual_recovery_required",
) -> None:
    try:
        _journal_update(root_fd, root, paths, journal, "manual_recovery_required")
    except SupervisorRefusal:
        pass
    _refuse(code)


def _cleanup_interrupted_paths(
    plan: Mapping[str, Any],
    interrupted_paths: Mapping[str, Any],
    current_paths: Mapping[str, str],
    journal: dict[str, Any],
    *,
    root_fd: int,
) -> None:
    expected = {
        interrupted_paths.get("active_tmp"),
        interrupted_paths.get("journal_tmp"),
        interrupted_paths.get("generation_tmp"),
    }
    cleanup = plan.get("cleanup_paths")
    if None in expected or not isinstance(cleanup, list) or set(cleanup) != expected:
        _refuse("recovery_cleanup_paths_mismatch")
    root = Path(plan["deployment_root"]["path"])
    for value in cleanup:
        path = _absolute(value, "recovery_cleanup_paths_invalid")
        if path.parent != root or path in {
            Path(current_paths["active_link"]),
            Path(current_paths["journal"]),
            Path(current_paths["journal_guard"]),
            Path(current_paths["generation"]),
            Path(current_paths["output_receipt"]),
        }:
            _refuse("recovery_cleanup_paths_invalid")
        if _exists_at(root_fd, root, path):
            try:
                os.unlink(_root_name(root, path), dir_fd=root_fd)
            except OSError:
                _mark_manual_recovery(root_fd, root, current_paths, journal)
    os.fsync(root_fd)


def _matching_transaction_receipt(
    receipt: Mapping[str, Any], journal: Mapping[str, Any],
) -> bool:
    return (
        receipt.get("schema_version") == 1
        and receipt.get("operation") == journal.get("operation")
        and receipt.get("transaction_id") == journal.get("transaction_id")
        and receipt.get("generation_before") == journal.get("generation_before")
        and receipt.get("generation_after") == journal.get("generation_after")
        and _same_mapping(receipt.get("before"), journal.get("before"))
        and _same_mapping(receipt.get("after"), journal.get("after"))
        and receipt.get("config_hashes") == journal.get("config_hashes")
        and receipt.get("plan_sha256") == journal.get("plan_sha256")
        and receipt.get("authorization_sha256") == journal.get("authorization_sha256")
    )


def _matching_recovery_receipt(
    receipt: Mapping[str, Any], journal: Mapping[str, Any],
) -> bool:
    expected = {
        "schema_version": 1,
        "operation": "recover",
        "transaction_id": journal.get("recovery_transaction_id"),
        "plan_sha256": journal.get("recovery_plan_sha256"),
        "authorization_sha256": journal.get("recovery_authorization_sha256"),
        "status": "conditional",
        "outcome": journal.get("recovery_status"),
        "committed": False,
        "valid_when": {
            "generation": journal.get("generation_after"),
            "last_transaction_id": journal.get("recovery_transaction_id"),
            "active_target": (
                journal.get("before", {}).get("path")
                if isinstance(journal.get("before"), Mapping)
                else None
            ),
            "transaction_records_absent": True,
        },
        "generation_before": journal.get("generation_before"),
        "generation_after": journal.get("generation_after"),
        "interrupted_transaction_id": journal.get("transaction_id"),
        "orphan_receipt": journal.get("recovery_orphan_receipt"),
        "orphan_committed": False,
    }
    return dict(receipt) == expected


def _normalize_recovery_record(
    root_fd: int,
    root: Path,
    paths: Mapping[str, str],
    record_path: Path,
) -> None:
    if record_path == Path(paths["journal"]):
        return
    if record_path != Path(paths["journal_guard"]):
        _refuse("recovery_journal_path_mismatch")
    try:
        os.replace(
            _root_name(root, paths["journal_guard"]),
            _root_name(root, paths["journal"]),
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        os.fsync(root_fd)
    except OSError:
        _refuse("manual_recovery_required")


def _recover(
    plan: Mapping[str, Any], inputs: Mapping[str, str], paths: Mapping[str, str],
    state: Mapping[str, Any], plan_sha256: str, authorization_sha256: str,
    authorization_identity: Mapping[str, Any],
    fault_hook: Callable[[str], None] | None, root_fd: int,
) -> dict[str, Any]:
    root = Path(plan["deployment_root"]["path"])
    journal_path = _verify_digest_reference(plan.get("journal"), inputs)
    if journal_path not in {
        Path(paths["journal"]), Path(paths["journal_guard"]),
    }:
        _refuse("recovery_journal_path_mismatch")
    if _sha256_at(root_fd, root, journal_path) != plan["journal"]["sha256"]:
        _refuse("authorized_input_digest_mismatch")
    journal = _load_json_at(root_fd, root, journal_path)
    required = {
        "schema_version", "operation", "transaction_id", "phase",
        "generation_before", "generation_after", "before", "after",
        "config_hashes", "plan_sha256", "authorization_sha256",
        "receipt_path", "paths",
    }
    optional = {
        "recovery_transaction_id", "recovery_receipt_path",
        "recovery_receipt_sha256", "recovery_plan_sha256",
        "recovery_authorization_sha256", "recovery_status",
        "recovery_orphan_receipt",
    }
    if (
        not required.issubset(journal)
        or set(journal) - required - optional
        or journal.get("schema_version") != 1
        or journal.get("operation") not in {"activate", "rollback"}
    ):
        _refuse("recovery_journal_invalid")
    interrupted_paths = journal.get("paths")
    if not isinstance(interrupted_paths, Mapping):
        _refuse("recovery_journal_invalid")
    if not _same_mapping(plan.get("before"), journal.get("after")) or not _same_mapping(plan.get("after"), journal.get("before")):
        _refuse("recovery_release_mismatch")
    before = journal["before"]
    after = journal["after"]
    releases = Path(paths["releases_root"])
    try:
        _validate_release(before, releases)
        _validate_release(after, releases)
        configs = _validate_configs(plan, inputs, releases)
    except SupervisorRefusal as error:
        _mark_manual_recovery(
            root_fd,
            root,
            paths,
            journal,
            (
                "hard_link_input_refused"
                if error.code == "hard_link_input_refused"
                else "recovery_release_invalid"
            ),
        )
    if configs != journal.get("config_hashes"):
        _mark_manual_recovery(
            root_fd, root, paths, journal, "recovery_release_invalid",
        )
    current_target = _active_target_at(root_fd)
    generation_before = journal["generation_before"]
    generation_after = journal["generation_after"]
    interrupted_receipt_path = Path(journal["receipt_path"])
    interrupted_receipt_exists = _exists_at(root_fd, root, interrupted_receipt_path)
    committed = (
        state["generation"] == generation_after
        and state["last_transaction_id"] == journal["transaction_id"]
        and state["active_target"] == after["path"]
        and state["committed_operation"] == journal["operation"]
        and state["committed_receipt_path"] == str(interrupted_receipt_path)
        and current_target == after["path"]
    )
    recovery_committed = (
        journal.get("recovery_transaction_id") is not None
        and state["generation"] == generation_after
        and state["last_transaction_id"] == journal.get("recovery_transaction_id")
        and state["active_target"] == before["path"]
        and state["committed_operation"] == "recover"
        and state["committed_receipt_path"] == journal.get("recovery_receipt_path")
        and state["committed_receipt_sha256"] == journal.get("recovery_receipt_sha256")
        and current_target == before["path"]
    )
    receipt_path = Path(paths["output_receipt"])
    if committed:
        reference = plan.get("interrupted_receipt")
        if not interrupted_receipt_exists or not isinstance(reference, Mapping):
            _mark_manual_recovery(
                root_fd, root, paths, journal, "committed_receipt_invalid",
            )
        bound_path = _verify_digest_reference(reference, inputs)
        if (
            bound_path != interrupted_receipt_path
            or _sha256_at(root_fd, root, bound_path) != reference.get("sha256")
        ):
            _mark_manual_recovery(
                root_fd, root, paths, journal, "committed_receipt_invalid",
            )
        if state["committed_receipt_sha256"] != reference.get("sha256"):
            _mark_manual_recovery(
                root_fd, root, paths, journal, "committed_receipt_invalid",
            )
        transaction_receipt = _load_json_at(root_fd, root, bound_path)
        if not _matching_transaction_receipt(transaction_receipt, journal):
            _mark_manual_recovery(
                root_fd, root, paths, journal, "committed_receipt_invalid",
            )
        _assert_authorization_unchanged(authorization_identity)
        _normalize_recovery_record(root_fd, root, paths, journal_path)
        _cleanup_interrupted_paths(
            plan, interrupted_paths, paths, journal, root_fd=root_fd,
        )
        receipt = {
            "schema_version": 1,
            "operation": "recover",
            "transaction_id": plan["transaction_id"],
            "plan_sha256": plan_sha256,
            "authorization_sha256": authorization_sha256,
            "status": "conditional",
            "outcome": "committed_cleanup",
            "committed": False,
            "valid_when": {
                "generation": state["generation"],
                "last_transaction_id": state["last_transaction_id"],
                "active_target": current_target,
                "transaction_records_absent": True,
            },
            "interrupted_transaction_id": journal["transaction_id"],
            "orphan_receipt": None,
        }
        try:
            _assert_authorization_unchanged(authorization_identity)
            _write_exclusive_json_at(root_fd, root, receipt_path, receipt)
        except SupervisorRefusal:
            _mark_manual_recovery(root_fd, root, paths, journal)
    elif recovery_committed:
        prior_path_value = journal.get("recovery_receipt_path")
        prior_digest = journal.get("recovery_receipt_sha256")
        if (
            not isinstance(prior_path_value, str)
            or not isinstance(prior_digest, str)
            or len(prior_digest) != 64
        ):
            _mark_manual_recovery(
                root_fd, root, paths, journal, "recovery_receipt_invalid",
            )
        prior_path = Path(prior_path_value)
        if (
            prior_path.parent != root
            or not _exists_at(root_fd, root, prior_path)
            or _sha256_at(root_fd, root, prior_path) != prior_digest
        ):
            _mark_manual_recovery(
                root_fd, root, paths, journal, "recovery_receipt_invalid",
            )
        prior_receipt = _load_json_at(root_fd, root, prior_path)
        if not _matching_recovery_receipt(prior_receipt, journal):
            _mark_manual_recovery(
                root_fd, root, paths, journal, "recovery_receipt_invalid",
            )
        _assert_authorization_unchanged(authorization_identity)
        _normalize_recovery_record(root_fd, root, paths, journal_path)
        _cleanup_interrupted_paths(
            plan, interrupted_paths, paths, journal, root_fd=root_fd,
        )
        receipt = {
            "schema_version": 1,
            "operation": "recover",
            "transaction_id": plan["transaction_id"],
            "plan_sha256": plan_sha256,
            "authorization_sha256": authorization_sha256,
            "status": "conditional",
            "outcome": "recovery_committed_cleanup",
            "committed": False,
            "valid_when": {
                "generation": state["generation"],
                "last_transaction_id": state["last_transaction_id"],
                "active_target": current_target,
                "transaction_records_absent": True,
            },
            "interrupted_transaction_id": journal["transaction_id"],
            "prior_recovery_transaction_id": journal["recovery_transaction_id"],
            "prior_recovery_receipt_sha256": prior_digest,
            "orphan_receipt": journal.get("recovery_orphan_receipt"),
        }
        try:
            _assert_authorization_unchanged(authorization_identity)
            _write_exclusive_json_at(root_fd, root, receipt_path, receipt)
        except SupervisorRefusal:
            _mark_manual_recovery(root_fd, root, paths, journal)
    elif journal.get("recovery_transaction_id") is not None:
        _mark_manual_recovery(
            root_fd, root, paths, journal, "recovery_incomplete",
        )
    elif state["generation"] == generation_before and state["last_transaction_id"] != journal["transaction_id"]:
        _assert_authorization_unchanged(authorization_identity)
        _normalize_recovery_record(root_fd, root, paths, journal_path)
        if current_target == before["path"]:
            status = "aborted"
        elif current_target == after["path"]:
            _create_temporary_link(
                root_fd, root, paths["active_tmp"], before["path"],
            )
            if _active_target_at(root_fd) != after["path"]:
                _mark_manual_recovery(root_fd, root, paths, journal)
            try:
                os.replace(
                    _root_name(root, paths["active_tmp"]),
                    _root_name(root, paths["active_link"]),
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                )
                os.fsync(root_fd)
            except OSError:
                _mark_manual_recovery(root_fd, root, paths, journal)
            if _active_target_at(root_fd) != before["path"]:
                _mark_manual_recovery(root_fd, root, paths, journal)
            status = "recovered"
        else:
            _mark_manual_recovery(root_fd, root, paths, journal)
        _cleanup_interrupted_paths(
            plan, interrupted_paths, paths, journal, root_fd=root_fd,
        )
        orphan = str(interrupted_receipt_path) if interrupted_receipt_exists else None
        receipt = {
            "schema_version": 1,
            "operation": "recover",
            "transaction_id": plan["transaction_id"],
            "plan_sha256": plan_sha256,
            "authorization_sha256": authorization_sha256,
            "status": "conditional",
            "outcome": status,
            "committed": False,
            "valid_when": {
                "generation": generation_after,
                "last_transaction_id": plan["transaction_id"],
                "active_target": before["path"],
                "transaction_records_absent": True,
            },
            "generation_before": generation_before,
            "generation_after": generation_after,
            "interrupted_transaction_id": journal["transaction_id"],
            "orphan_receipt": orphan,
            "orphan_committed": False,
        }
        journal["recovery_transaction_id"] = plan["transaction_id"]
        journal["recovery_receipt_path"] = str(receipt_path)
        journal["recovery_receipt_sha256"] = _sha256_bytes(_canonical_json(receipt))
        journal["recovery_plan_sha256"] = plan_sha256
        journal["recovery_authorization_sha256"] = authorization_sha256
        journal["recovery_status"] = status
        journal["recovery_orphan_receipt"] = orphan
        _assert_authorization_unchanged(authorization_identity)
        try:
            _journal_update(
                root_fd, root, paths, journal, "recovered_pending_state",
            )
        except SupervisorRefusal:
            _mark_manual_recovery(root_fd, root, paths, journal)
        _fault(fault_hook, "recovery_journal_prepared")
        recovery_state = {
            "schema_version": 1,
            "generation": generation_after,
            "last_transaction_id": plan["transaction_id"],
            "active_target": before["path"],
            "committed_operation": "recover",
            "committed_receipt_path": str(receipt_path),
            "committed_receipt_sha256": journal["recovery_receipt_sha256"],
        }
        _assert_authorization_unchanged(authorization_identity)
        try:
            _write_atomic_json_at(
                root_fd, root, paths["generation"], paths["generation_tmp"],
                recovery_state,
            )
        except SupervisorRefusal:
            _mark_manual_recovery(root_fd, root, paths, journal)
        _fault(fault_hook, "recovery_state_persisted")
        _assert_authorization_unchanged(authorization_identity)
        try:
            _write_exclusive_json_at(root_fd, root, receipt_path, receipt)
        except SupervisorRefusal:
            _mark_manual_recovery(root_fd, root, paths, journal)
        _fault(fault_hook, "recovery_receipt_persisted")
        try:
            _journal_update(root_fd, root, paths, journal, "recovery_committed")
        except SupervisorRefusal:
            _mark_manual_recovery(root_fd, root, paths, journal)
        _fault(fault_hook, "recovery_journal_committed")
    else:
        _mark_manual_recovery(root_fd, root, paths, journal)
    _assert_authorization_unchanged(authorization_identity)
    cleanup_status, cleanup_durable = _retire_transaction_record(
        root_fd, root, paths, fault_hook,
    )
    return {
        **receipt,
        "status": receipt["outcome"],
        "receipt_status": receipt["status"],
        "receipt_path": str(receipt_path),
        "cleanup_status": cleanup_status,
        "cleanup_durable": cleanup_durable,
    }


def execute(
    operation: str,
    operation_path: str | Path,
    *,
    authorization_path: str | Path,
    authorization_sha256: str,
    apply: bool = False,
    _fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute one finite supervisor operation under an exact external authorization."""
    if not apply:
        _refuse("explicit_apply_required")
    operation_file = _absolute(str(operation_path), "operation_input_invalid")
    authorization_file = _absolute(str(authorization_path), "authorization_input_invalid")
    if operation_file == authorization_file:
        _refuse("input_path_alias_refused")
    (
        plan,
        authorization,
        inputs,
        authorization_identity,
        root,
        paths,
    ) = _validate_common(
        operation, operation_file, authorization_file, authorization_sha256,
    )
    root_fd = _open_root(root, authorization["deployment_root"])
    try:
        lock_descriptor = _open_lock(root_fd)
        try:
            _validate_private_root(authorization["deployment_root"])
            _assert_authorization_unchanged(authorization_identity)
            _assert_inputs_unchanged(inputs)
            _validate_child_directory(Path(paths["releases_root"]), root, "releases_root_invalid")
            journal_exists = _exists_at(root_fd, root, paths["journal"])
            guard_exists = _exists_at(root_fd, root, paths["journal_guard"])
            if operation == "recover":
                if journal_exists and guard_exists:
                    _refuse("manual_recovery_required")
                if not journal_exists and not guard_exists:
                    _refuse("recovery_record_missing")
            elif journal_exists or guard_exists:
                _refuse("recovery_required")
            state = _state_at(root_fd, root, paths["generation"])
            if state["generation"] != plan.get("expected_generation"):
                _refuse("generation_mismatch")
            plan_sha256 = inputs[str(operation_file)]
            if operation == "activate":
                return _activate(
                    plan, inputs, paths, state, plan_sha256,
                    authorization_sha256, authorization_identity, _fault_hook,
                    root_fd,
                )
            if operation == "finalize":
                return _finalize(
                    plan, inputs, paths, state, plan_sha256,
                    authorization_sha256, authorization_identity, root_fd,
                )
            if operation == "rollback":
                return _rollback(
                    plan, inputs, paths, state, plan_sha256,
                    authorization_sha256, authorization_identity, _fault_hook,
                    root_fd,
                )
            return _recover(
                plan, inputs, paths, state, plan_sha256,
                authorization_sha256, authorization_identity, _fault_hook,
                root_fd,
            )
        finally:
            os.close(lock_descriptor)
    finally:
        os.close(root_fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finite isolated WRR release supervisor")
    parser.add_argument("operation", choices=sorted(OPERATIONS))
    parser.add_argument("operation_input")
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--authorization-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        result = execute(
            arguments.operation,
            arguments.operation_input,
            authorization_path=arguments.authorization,
            authorization_sha256=arguments.authorization_sha256,
            apply=arguments.apply,
        )
    except SupervisorRefusal as error:
        print(json.dumps({"status": "refused", "reason": error.code}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
