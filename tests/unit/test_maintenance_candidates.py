"""Candidate preparation must never mutate the declared dependency target."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from wrr.cli.update import update
from wrr.engines.loader import load_engine_manifest


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


def snapshot(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*") if p.is_file()
    }


@pytest.fixture
def repo_case(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    source = tmp_path / "origin"
    git("init", source)
    git("-C", source, "config", "user.email", "fixture@example.invalid")
    git("-C", source, "config", "user.name", "Fixture")
    (source / "body.txt").write_text("version-one\n")
    (source / ".gitignore").write_text("ignored.txt\n")
    git("-C", source, "add", ".")
    git("-C", source, "commit", "-m", "one")
    pin = git("-C", source, "rev-parse", "HEAD")
    (source / "body.txt").write_text("version-two\n")
    git("-C", source, "commit", "-am", "two")
    target = tmp_path / "target"
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    manifest = tmp_path / "plugins" / "engines" / "sample" / "engine.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "health": {"checks": []},
        "schema_version": 1, "id": "sample", "name": "Sample", "kind": "local_cli",
        "adapter": "wrr.engines.brave:BraveEngine", "capabilities": {"actions": ["search"]},
        "routing": {"modes": ["auto"]}, "requires_capabilities": {},
        "requirements": {"repos": [{"name": "sample", "path": str(target), "remote": str(source), "pin": pin}]},
    }))
    discovery = load_engine_manifest(manifest, source="project", trust_level="project")
    assert discovery.valid
    return source, target, candidates, pin, discovery


def prepare(case, **kwargs):
    source, target, candidates, pin, discovery = case
    options = dict(dry_run=False, trust_project=True, discoveries=[discovery],
                   owned_paths=[target], candidate_root=candidates)
    options.update(kwargs)
    return update(**options).to_dict()


def with_requirement(case, **changes):
    source, target, candidates, pin, discovery = case
    requirement = replace(discovery.manifest.repo_requirements[0], **changes)
    return source, target, candidates, pin, replace(discovery, manifest=replace(
        discovery.manifest, repo_requirements=(requirement,)))


@pytest.mark.parametrize("existing", [False, True])
def test_apply_needs_exact_invocation_ownership_and_source_trust(repo_case, existing):
    source, target, candidates, _, _ = repo_case
    if existing:
        git("clone", source, target)
    before = snapshot(target)
    for options in ({"owned_paths": []}, {"trust_project": False}):
        report = prepare(repo_case, **options)
        assert report["summary"]["status"] == "fail"
        assert snapshot(target) == before
        assert target.exists() is existing
        assert list(candidates.iterdir()) == []


@pytest.mark.parametrize("dirty", ["tracked", "untracked", "ignored"])
def test_dirty_target_is_untouched_before_candidate_creation(repo_case, dirty):
    source, target, candidates, _, _ = repo_case
    git("clone", source, target)
    name = {"tracked": "body.txt", "untracked": "local.txt", "ignored": "ignored.txt"}[dirty]
    (target / name).write_text("local work must survive\n")
    before = snapshot(target)
    report = prepare(repo_case)
    assert report["repos"][0]["status"] == "refused"
    assert snapshot(target) == before
    assert list(candidates.iterdir()) == []


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_index_flags_cannot_hide_modified_target_content(repo_case, index_flag):
    source, target, candidates, _, _ = repo_case
    git("clone", source, target)
    git("-C", target, "update-index", index_flag, "body.txt")
    (target / "body.txt").write_text("hidden local work must survive\n")
    before = snapshot(target)
    report = prepare(repo_case)
    assert report["repos"][0]["status"] == "refused"
    assert snapshot(target) == before
    assert list(candidates.iterdir()) == []


@pytest.mark.parametrize("existing", [False, True])
def test_candidate_is_pinned_without_installing_or_activating_target(repo_case, existing):
    source, target, candidates, pin, _ = repo_case
    if existing:
        git("clone", source, target)
    before = snapshot(target)
    report = prepare(repo_case)
    result = report["repos"][0]
    assert result["status"] == "downloaded"
    candidate = Path(result["candidate_path"])
    assert candidate.is_relative_to(candidates)
    assert git("-C", candidate, "rev-parse", "HEAD") == pin
    assert (candidate / "body.txt").read_text() == "version-one\n"
    assert result["candidate_revision"] == pin
    assert result["revision_verified"] is True
    assert result["activated"] is False
    assert snapshot(target) == before
    assert target.exists() is existing


@pytest.mark.parametrize("pin", [None, "abc123", "0" * 40])
def test_missing_mutable_or_unresolvable_pin_never_changes_target(repo_case, pin):
    source, target, candidates, _, _ = repo_case
    git("clone", source, target)
    keep = candidates / "unrelated.txt"
    keep.write_text("not owned by this invocation")
    before = snapshot(target)
    report = prepare(with_requirement(repo_case, pin=pin))
    assert report["summary"]["status"] == "fail"
    assert snapshot(target) == before
    assert list(candidates.iterdir()) == [keep]
    assert keep.read_text() == "not owned by this invocation"


def test_wrong_origin_refuses_before_download(repo_case):
    source, target, candidates, _, _ = repo_case
    git("clone", source, target)
    git("-C", target, "remote", "set-url", "origin", str(source.parent / "other"))
    before = snapshot(target)
    assert prepare(repo_case)["repos"][0]["status"] == "refused"
    assert snapshot(target) == before
    assert list(candidates.iterdir()) == []


@pytest.mark.parametrize("alias", ["target", "parent", "nested_output"])
def test_path_aliases_do_not_bypass_target_or_output_ownership(repo_case, alias):
    source, target, candidates, _, _ = repo_case
    git("clone", source, target)
    before = snapshot(target)
    options = {}
    case = repo_case
    if alias == "nested_output":
        options["candidate_root"] = target
    else:
        link = source.parent / "alias"
        link.symlink_to(target if alias == "target" else target.parent, target_is_directory=True)
        selected = link if alias == "target" else link / target.name
        case = with_requirement(repo_case, path=str(selected))
        options["owned_paths"] = [selected]
    assert prepare(case, **options)["summary"]["status"] == "fail"
    assert snapshot(target) == before
    assert list(candidates.iterdir()) == []


def test_ambient_git_hooks_and_secrets_are_not_used(repo_case, monkeypatch):
    source, target, candidates, _, _ = repo_case
    marker = source.parent / "hook-ran"
    hooks = source.parent / "hooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    hostile_config = source.parent / "ambient.gitconfig"
    hostile_config.write_text(f"[core]\n\thooksPath = {hooks}\n[url \"ext::sh -c touch-{marker}\"]\n\tinsteadOf = {source}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    monkeypatch.setenv("WRR_MAINTENANCE_SENTINEL", "private-sentinel-no-echo")
    report = prepare(repo_case)
    assert report["repos"][0]["status"] == "downloaded"
    assert not marker.exists()
    assert "private-sentinel-no-echo" not in json.dumps(report)
    assert not target.exists()


def test_explicit_selection_does_not_authorize_other_discovered_targets(repo_case):
    source, target, candidates, _, discovery = repo_case
    second = with_requirement(repo_case, name="other", path=str(source.parent / "other"))[-1]
    report = prepare(repo_case, discoveries=[discovery, second])
    assert report["summary"]["status"] == "ok"
    assert len(report["repos"]) == 1
    assert not (source.parent / "other").exists()
    assert not target.exists()


def test_unknown_selection_is_not_silent_success(repo_case):
    source, target, candidates, _, _ = repo_case
    report = prepare(repo_case, owned_paths=[source.parent / "not-a-declared-target"])
    assert report["summary"]["status"] == "fail"
    assert not target.exists()
    assert list(candidates.iterdir()) == []


def test_admission_never_executes_repository_local_clean_filters(repo_case):
    source, target, candidates, _, _ = repo_case
    git("clone", source, target)
    marker = source.parent / "filter-executed"
    included = target / ".git" / "filter-config"
    git("config", "--file", included, "filter.probe.clean", f"touch '{marker}'; cat")
    git("-C", target, "config", "include.path", str(included))
    (target / ".git" / "info" / "attributes").write_text("body.txt filter=probe\n")
    (target / "body.txt").write_text("version-bad\n")
    before = snapshot(target)
    report = prepare(repo_case)
    assert not marker.exists()
    assert report["repos"][0]["status"] == "refused"
    assert snapshot(target) == before
    assert list(candidates.iterdir()) == []


def test_ignored_submodule_content_is_not_certified_clean(repo_case):
    source, target, candidates, _, _ = repo_case
    git("-C", source, "-c", "protocol.file.allow=always", "submodule", "add", source, "nested")
    git("-C", source, "commit", "-am", "local submodule fixture")
    git("-c", "protocol.file.allow=always", "clone", "--recurse-submodules", source, target)
    (target / "nested" / "ignored.txt").write_text("private local content\n")
    before = snapshot(target)
    report = prepare(repo_case)
    assert report["repos"][0]["status"] == "refused"
    assert snapshot(target) == before
    assert list(candidates.iterdir()) == []


def test_output_never_overlaps_an_unselected_discovered_target(repo_case):
    source, target, candidates, _, discovery = repo_case
    other = source.parent / "other"
    git("clone", source, other)
    second = with_requirement(repo_case, name="other", path=str(other))[-1]
    before = snapshot(other)
    report = prepare(repo_case, discoveries=[discovery, second], candidate_root=other)
    assert report["summary"]["status"] == "fail"
    assert snapshot(other) == before
    assert not target.exists()


def test_output_directory_rebinding_never_writes_the_live_target(repo_case, monkeypatch):
    from importlib import import_module
    updater = import_module("wrr.cli.update")
    source, target, candidates, _, _ = repo_case
    git("clone", source, target)
    before = snapshot(target)
    preserved = candidates / "unrelated.txt"
    preserved.write_text("preserve")
    original_git = updater._git
    writes_to_target = []
    moved = candidates.with_name("moved-candidates")

    def rebind_before_clone(*args, **kwargs):
        if args[0] == "clone":
            candidates.rename(moved)
            candidates.symlink_to(target, target_is_directory=True)
            result = original_git(*args, **kwargs)
            writes_to_target.append(any(p.name.startswith("wrr-candidate-") for p in target.iterdir()))
            return result
        return original_git(*args, **kwargs)

    monkeypatch.setattr(updater, "_git", rebind_before_clone)
    report = prepare(repo_case)
    assert writes_to_target == [False]
    assert report["summary"]["status"] == "fail"
    assert snapshot(target) == before
    assert sorted(p.name for p in moved.iterdir()) == ["unrelated.txt"]
    assert (moved / "unrelated.txt").read_text() == "preserve"


def test_text_cli_identifies_target_candidate_and_exact_pin(repo_case):
    source, target, candidates, pin, _ = repo_case
    root = Path(__file__).resolve().parents[2]
    empty_env = source.parent / "empty.env"
    empty_env.write_text("")
    env = dict(os.environ, WRR_ENV=str(empty_env), PYTHONPATH=str(root),
               WRR_CACHE_DIR=str(source.parent / "cache"))
    completed = subprocess.run(
        [sys.executable, str(root / "wrr-cli.py"), "update", "--apply", "--trust-project",
         "--owned-repo", str(target), "--candidate-root", str(candidates)],
        cwd=source.parent, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=30,
    )
    assert completed.returncode == 0
    candidate, = candidates.iterdir()
    assert str(target) in completed.stdout
    assert str(candidate) in completed.stdout
    assert pin in completed.stdout
    assert not target.exists()
