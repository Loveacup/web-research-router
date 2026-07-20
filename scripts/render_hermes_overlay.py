#!/usr/bin/env python3
"""Render the runtime-owned Hermes overlay for web-research-router.

Pure single-input / single-output transform:

    --core <regular file>  ->  <repo>/hermes-overlay/research/web-research-router/SKILL.md

The renderer embeds an explicit clean-room core (frontmatter + body) verbatim,
records the input SHA-256 in a provenance comment inserted immediately after the
frontmatter's closing delimiter, and appends a small repository-owned Hermes
binding tail. Removing the provenance comment and the binding tail reconstructs
the input core byte-for-byte.

It writes only the fixed repository-relative destination, derives its repository
root only from this file's own location, and creates every path component with
directory-descriptor-relative ``mkdirat`` / ``openat`` semantics under
``O_DIRECTORY | O_NOFOLLOW`` so a symlinked component or destination is rejected
rather than followed. No home expansion, environment-derived target, profile or
config access, subprocess, watcher, or host path literal is used.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys

# Fixed repository-relative output location. No host path, no home, no env.
_OUTPUT_COMPONENTS = ("hermes-overlay", "research", "web-research-router")
_OUTPUT_FILENAME = "SKILL.md"

# Provenance/binding sentinels. Removing the provenance block and truncating at
# the binding marker must reconstruct the input core exactly.
_PROVENANCE_BEGIN = "<!-- wrr-overlay-provenance"
_PROVENANCE_END = "-->\n"
_BINDING_MARKER = "<!-- wrr-overlay-binding -->\n"

_BINDING = _BINDING_MARKER + """## Hermes runtime binding

This overlay runs inside the Hermes runtime, which loads the `wrr` toolset. Use
those tools for external research; the registered live schema owns every optional
argument and the tool's own behavior.

- `web_search` takes a non-empty `query` and returns discovery evidence.
- `web_fetch` retrieves the page at a chosen `url`.
- `web_similar` expands from a reference `url` to related sources.

Tool output and metadata are evidence about one execution, not a conclusion.
Keep the canonical evidence boundary between Confirmed, Inference, and gaps.

Do not use this overlay for local-file work, source inspection, plugin
administration, configuration, or runtime setup. Nothing here chooses tool
arguments beyond the required inputs above; the registered plugin schema and
its implementation own everything else.
"""


class OverlayError(Exception):
    """Raised when the overlay cannot be produced safely."""


def _provenance(digest: str) -> str:
    return (
        f"{_PROVENANCE_BEGIN}\n"
        f"source-sha256: {digest}\n"
        "generated-by: scripts/render_hermes_overlay.py\n"
        "regenerate from the clean-room core; do not edit by hand\n"
        f"{_PROVENANCE_END}"
    )


def render_text(core_bytes: bytes) -> str:
    """Transform the raw core bytes into the overlay text (no filesystem I/O)."""
    core_text = core_bytes.decode("utf-8")
    if not core_text.startswith("---\n"):
        raise OverlayError("core input must begin with a YAML frontmatter delimiter")
    close = core_text.find("\n---\n", 4)
    if close == -1:
        raise OverlayError("core input has no frontmatter closing delimiter")
    fm_end = close + len("\n---\n")
    frontmatter_block = core_text[:fm_end]
    body = core_text[fm_end:]
    digest = hashlib.sha256(core_bytes).hexdigest()
    return frontmatter_block + _provenance(digest) + body + _BINDING


def strip_provenance_and_binding(rendered: str) -> str:
    """Inverse of :func:`render_text`: recover the exact core input."""
    start = rendered.index(_PROVENANCE_BEGIN)
    end = rendered.index(_PROVENANCE_END, start) + len(_PROVENANCE_END)
    without_provenance = rendered[:start] + rendered[end:]
    binding_at = without_provenance.index(_BINDING_MARKER)
    return without_provenance[:binding_at]


def _read_core(core_path) -> bytes:
    """Read the explicit core input, rejecting anything but a regular file."""
    try:
        fd = os.open(os.fspath(core_path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise OverlayError(
            f"core input is not a readable regular file: {exc}"
        ) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OverlayError("core input must be a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _descend(parent_fd: int, name: str) -> int:
    """mkdirat(name) if missing, then openat with O_DIRECTORY|O_NOFOLLOW.

    A symlinked or non-directory component raises (rejected, never followed).
    """
    try:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        return os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
    except OSError as exc:
        raise OverlayError(
            f"refusing unsafe overlay path component {name!r}: {exc}"
        ) from exc


def _write_output(repo_root: str, text: str) -> str:
    data = text.encode("utf-8")
    open_fds: list[int] = []
    root_fd = os.open(repo_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    open_fds.append(root_fd)
    try:
        parent_fd = root_fd
        for name in _OUTPUT_COMPONENTS:
            child_fd = _descend(parent_fd, name)
            open_fds.append(child_fd)
            parent_fd = child_fd
        final_fd = parent_fd

        # Reject a symlinked destination outright.
        try:
            dst_st = os.lstat(_OUTPUT_FILENAME, dir_fd=final_fd)
        except FileNotFoundError:
            dst_st = None
        if dst_st is not None and stat.S_ISLNK(dst_st.st_mode):
            raise OverlayError("refusing to write over a symlinked destination")

        # Create a uniquely named temp file through the final directory fd only.
        tmp_name = None
        tmp_fd = None
        pid = os.getpid()
        for counter in range(10000):
            candidate = f".{_OUTPUT_FILENAME}.tmp.{pid}.{counter}"
            try:
                tmp_fd = os.open(
                    candidate,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=final_fd,
                )
            except FileExistsError:
                continue
            tmp_name = candidate
            break
        if tmp_fd is None:
            raise OverlayError("could not create a unique temporary overlay file")

        try:
            with os.fdopen(tmp_fd, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                tmp_name, _OUTPUT_FILENAME, src_dir_fd=final_fd, dst_dir_fd=final_fd
            )
            tmp_name = None  # replaced; nothing left to clean up
            os.fsync(final_fd)
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name, dir_fd=final_fd)
                except OSError:
                    pass

        return os.path.join(repo_root, *_OUTPUT_COMPONENTS, _OUTPUT_FILENAME)
    finally:
        for fd in reversed(open_fds):
            try:
                os.close(fd)
            except OSError:
                pass


def render_overlay(repo_root, core_path) -> str:
    """Render the overlay under ``repo_root`` from an explicit ``core_path``.

    Testable with a temporary ``repo_root``; the CLI supplies its own repository
    root derived only from this file's location.
    """
    core_bytes = _read_core(core_path)
    text = render_text(core_bytes)
    return _write_output(os.fspath(repo_root), text)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the runtime-owned Hermes overlay from an explicit core."
    )
    parser.add_argument(
        "--core",
        required=True,
        help="Path to the explicit clean-room core SKILL.md (a regular file).",
    )
    args = parser.parse_args(argv)
    out = render_overlay(_repo_root(), args.core)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
