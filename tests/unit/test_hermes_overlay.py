"""Contract tests for the runtime-owned Hermes overlay renderer.

Covers frontmatter position and fields, provenance SHA-256, exact source
reconstruction, canonical inclusion, the Hermes binding contract, forbidden
policy terms, trigger / do-not-trigger wording, descriptor-relative symlink
rejection at every writable component, a destination symlink substituted right
before replacement, regular-file input enforcement, and the CLI's single
canonical invocation using its own repository root.
"""
import hashlib
import importlib.util
import os
import re
import stat
from pathlib import Path

import pytest
import yaml

RENDERER = Path(__file__).resolve().parents[2] / "scripts" / "render_hermes_overlay.py"


def _load():
    spec = importlib.util.spec_from_file_location("render_hermes_overlay", RENDERER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()

# A self-contained core fixture (host-independent). The frontmatter mirrors the
# canonical clean-room core so trigger wording tests exercise the real policy.
CORE = (
    "---\n"
    "name: web-research-router\n"
    'description: "Use when a question needs fresh external evidence, '
    "source-backed comparison, fact verification, or a structured research "
    "brief. Do not use for local-file operations or direct source "
    'inspection."\n'
    "type: routine\n"
    "license: MIT\n"
    "---\n"
    "\n"
    "# Web Research\n"
    "\n"
    "Gather claim-level evidence and keep an explicit evidence boundary\n"
    "between Confirmed, Inference, and Conflicts & gaps.\n"
)

COMPONENTS = ["hermes-overlay", "research", "web-research-router", "SKILL.md"]


def _write_core(tmp_path):
    core = tmp_path / "core.md"
    core.write_bytes(CORE.encode("utf-8"))
    return core


def _render(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    core = _write_core(tmp_path)
    out = mod.render_overlay(repo, core)
    return repo, out, Path(out).read_text(encoding="utf-8")


def _frontmatter(text):
    assert text.startswith("---\n"), "frontmatter delimiter not at byte zero"
    end = text.index("\n---\n", 4)
    block = text[4 : end + 1]
    docs = list(yaml.safe_load_all(block))
    assert len(docs) == 1, "expected exactly one frontmatter mapping"
    assert isinstance(docs[0], dict)
    return docs[0]


# ── frontmatter / fields ─────────────────────────────────────────────────────

def test_frontmatter_byte_zero_and_canonical_fields(tmp_path):
    _, _, text = _render(tmp_path)
    fm = _frontmatter(text)
    assert fm["name"] == "web-research-router"
    assert fm["type"] == "routine"
    assert fm["license"] == "MIT"
    assert "fresh external evidence" in fm["description"]
    # explicit do-not trigger present in the description itself
    assert "Do not use" in fm["description"]


def test_overlay_stays_small_and_has_no_forbidden_global_terms(tmp_path):
    _, _, text = _render(tmp_path)
    assert len(text.splitlines()) <= 130
    lowered = text.lower()
    for forbidden in ("/users/", "~/.agents", ".agents", "external_dirs"):
        assert forbidden not in lowered, forbidden
    for forbidden in ("credential", "deploy"):
        assert forbidden not in lowered, forbidden


# ── provenance + exact reconstruction ────────────────────────────────────────

def test_provenance_carries_input_sha256(tmp_path):
    _, _, text = _render(tmp_path)
    digest = hashlib.sha256(CORE.encode("utf-8")).hexdigest()
    assert f"source-sha256: {digest}" in text


def test_exact_source_reconstruction(tmp_path):
    _, _, text = _render(tmp_path)
    recovered = mod.strip_provenance_and_binding(text)
    assert recovered == CORE
    assert recovered.encode("utf-8") == CORE.encode("utf-8")


def test_canonical_body_included_verbatim(tmp_path):
    _, _, text = _render(tmp_path)
    assert "# Web Research" in text
    assert "explicit evidence boundary" in text


# ── binding contract ─────────────────────────────────────────────────────────

def test_binding_contract_and_line_budget():
    binding = mod._BINDING
    assert len(binding.splitlines()) <= 35
    for name in ("web_search", "web_fetch", "web_similar"):
        assert name in binding
    assert "query" in binding
    assert "url" in binding
    # evidence boundary statement + explicit do-not scope
    assert "evidence" in binding.lower()
    assert "not a conclusion" in binding
    assert "Do not use this overlay" in binding


def test_binding_has_no_policy_terms():
    binding = mod._BINDING.lower()
    # word-boundary policy terms that must live only in the plugin schema
    for term in ("mode", "provider", "route", "routing", "fallback", "default", "engine"):
        assert re.search(rf"\b{term}\b", binding) is None, term
    for engine in ("brave", "exa", "tavily", "searxng", "serpapi", "duckduckgo", "grounding"):
        assert re.search(rf"\b{engine}\b", binding) is None, engine
    for literal in (".agents", "/users/", "external_dirs", "credential", "deploy"):
        assert literal not in binding, literal


# ── trigger / do-not-trigger wording ─────────────────────────────────────────

_POS_KEYS = ("fresh external evidence", "source-backed", "fact verification",
             "research brief", "verification", "evidence", "comparison", "research")
_NEG_KEYS = ("local", "inspect", "the file", "directory listing", "grep", "edit ", "rename")

TRIGGERS = [
    "I need fresh external evidence on the new EU AI regulation",
    "Give me a source-backed comparison of two vector databases",
    "Do a fact verification on this viral revenue claim",
    "Produce a structured research brief about solid-state batteries",
    "Find fresh external evidence for the current CEO of that company",
    "I want a source-backed comparison of the two pricing tiers",
    "Run fact verification against primary sources for this quote",
    "Draft a research brief on the semiconductor market trend",
]

DO_NOT = [
    "Read the local config in my repo",
    "Open the file main.py and summarize it",
    "Inspect this source file directly",
    "Give me a directory listing of the project",
    "Grep the repository for TODO markers",
    "Edit the local README and commit it",
    "Rename a local module and fix imports",
    "Inspect the local logs on disk",
]


def _classify(phrase):
    low = phrase.lower()
    if any(k in low for k in _NEG_KEYS):
        return "no"
    if any(k in low for k in _POS_KEYS):
        return "trigger"
    return "unknown"


def test_trigger_and_do_not_keys_are_grounded_in_description(tmp_path):
    _, _, text = _render(tmp_path)
    desc = _frontmatter(text)["description"].lower()
    # positive contract language
    assert "fresh external evidence" in desc
    assert "source-backed comparison" in desc
    assert "fact verification" in desc
    assert "structured research brief" in desc
    # negative contract language
    assert "local-file operations" in desc
    assert "direct source inspection" in desc


def test_eight_trigger_phrases_match():
    assert len(TRIGGERS) == 8
    for phrase in TRIGGERS:
        assert _classify(phrase) == "trigger", phrase


def test_eight_do_not_trigger_phrases_reject():
    assert len(DO_NOT) == 8
    for phrase in DO_NOT:
        assert _classify(phrase) == "no", phrase


# ── descriptor-relative symlink rejection ────────────────────────────────────

@pytest.mark.parametrize("depth", range(4))
def test_symlink_component_is_rejected_without_external_write(tmp_path, depth):
    repo = tmp_path / "repo"
    repo.mkdir()
    core = _write_core(tmp_path)

    # Build real intermediate directories for components before `depth`.
    parent = repo
    for name in COMPONENTS[:depth]:
        parent = parent / name
        parent.mkdir()

    target_name = COMPONENTS[depth]
    if depth < 3:
        external = tmp_path / "external_dir"
        external.mkdir()
        (external / "sentinel").write_text("SENTINEL")
    else:
        external = tmp_path / "external_file"
        external.write_text("EXTERNAL")

    os.symlink(external, parent / target_name)

    with pytest.raises(mod.OverlayError):
        mod.render_overlay(repo, core)

    if depth < 3:
        # rejection happened before descending: no overlay written through the link
        assert list(external.iterdir()) == [external / "sentinel"]
        assert not (external / "SKILL.md").exists()
    else:
        assert external.read_text() == "EXTERNAL"
        assert os.path.islink(parent / target_name)


def test_destination_symlink_substituted_before_replace(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    core = _write_core(tmp_path)
    external = tmp_path / "external_file"
    external.write_text("EXTERNAL")

    dest = repo / "hermes-overlay" / "research" / "web-research-router" / "SKILL.md"
    real_replace = mod.os.replace
    state = {"injected": False}

    def racing_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        # Slip a symlink into the destination name after the pre-check, right
        # before the atomic rename. renameat replaces the link itself.
        if not state["injected"]:
            state["injected"] = True
            os.symlink(external, dest)
        return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(mod.os, "replace", racing_replace)

    out = mod.render_overlay(repo, core)

    assert state["injected"] is True
    # external target untouched; destination is now a real file, not the link
    assert external.read_text() == "EXTERNAL"
    assert not os.path.islink(out)
    assert Path(out).read_text(encoding="utf-8").startswith("---\n")


# ── input must be a regular file ─────────────────────────────────────────────

def test_symlinked_core_input_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    real = _write_core(tmp_path)
    link = tmp_path / "link_core.md"
    os.symlink(real, link)
    with pytest.raises(mod.OverlayError):
        mod.render_overlay(repo, link)


def test_directory_core_input_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    a_dir = tmp_path / "a_dir"
    a_dir.mkdir()
    with pytest.raises(mod.OverlayError):
        mod.render_overlay(repo, a_dir)


# ── CLI canonical single invocation ──────────────────────────────────────────

def test_cli_invokes_render_once_with_own_repo_root(tmp_path, monkeypatch):
    core = _write_core(tmp_path)
    calls = []

    def fake_render(repo_root, core_path):
        calls.append((repo_root, core_path))
        return "OUT"

    monkeypatch.setattr(mod, "render_overlay", fake_render)
    rc = mod.main(["--core", str(core)])

    assert rc == 0
    assert len(calls) == 1
    repo_root, core_path = calls[0]
    expected_root = os.path.dirname(os.path.dirname(os.path.realpath(mod.__file__)))
    assert repo_root == expected_root
    assert core_path == str(core)
