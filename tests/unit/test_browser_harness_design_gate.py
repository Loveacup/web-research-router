"""Browser-harness fallback design gate tests.

These tests enforce that the browser-harness fallback remains a design contract
only — not implemented in v6.1 runtime code.

If any test here fails, it means implementation has leaked into hot-path code
without going through L1/L2 design review.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_or_empty(relpath: str) -> str:
    p = REPO_ROOT / relpath
    if p.is_file():
        return p.read_text()
    return ""


def test_design_spec_exists():
    """设计文档 references/browser-harness-fallback-design.md 必须存在。"""
    p = REPO_ROOT / "references" / "browser-harness-fallback-design.md"
    assert p.is_file(), "browser-harness design spec 不存在"
    content = p.read_text()
    assert "Interface" in content, "设计文档缺失 Interface 术语"
    assert "Adapter" in content, "设计文档缺失 Adapter 术语"
    assert "Seam" in content, "设计文档缺失 Seam 术语"


def test_no_browser_harness_in_community_engine():
    """community.py 不应包含 BrowserHarnessSourceAdapter 或 browser_harness 引用。"""
    content = _read_or_empty("wrr/engines/community.py")
    assert "BrowserHarnessSourceAdapter" not in content, (
        "community.py 不应引用 BrowserHarnessSourceAdapter（v6.x 路线图项）"
    )
    assert "browser_harness" not in content, (
        "community.py 不应引用 browser_harness（v6.x 路线图项）"
    )


def test_no_fallback_in_fetch_opencli():
    """_fetch_opencli 不应包含 fallback 接线。"""
    content = _read_or_empty("wrr/engines/community.py")
    assert "fallback" not in content or content.count("fallback") <= 0, (
        "community.py search hot path 不应包含 fallback 接线"
    )


def test_no_browser_harness_in_registry_or_doctor():
    """registry.py、doctor.py、router.py 不应包含 browser_harness 引用。"""
    for fname in ("wrr/engines/registry.py", "wrr/doctor.py", "wrr/router.py"):
        content = _read_or_empty(fname)
        assert "browser_harness" not in content, (
            f"{fname} 不应引用 browser_harness"
        )
        assert "BrowserHarness" not in content, (
            f"{fname} 不应引用 BrowserHarness"
        )


def test_no_browser_harness_in_manifest():
    """community engine.yaml manifest 不应包含 browser_harness。"""
    content = _read_or_empty("wrr/engines/builtin/community/engine.yaml")
    assert "browser_harness" not in content, (
        "community manifest 不应包含 browser_harness"
    )


# ── Slice 1: adapter seam + disabled policy scaffold ─────────────────
def test_community_policy_disabled_by_default():
    """community_policy 的浏览器兜底策略默认禁用。"""
    from wrr.engines import community_policy as cp
    assert cp.is_browser_harness_enabled() is False
    assert cp.DEFAULT_BROWSER_HARNESS_POLICY.enabled is False


def test_community_policy_has_no_browser_automation_imports():
    """community_policy.py 不得引入浏览器自动化依赖。"""
    content = _read_or_empty("wrr/engines/community_policy.py")
    assert content, "community_policy.py 必须存在"
    lowered = content.lower()
    for banned in ("playwright", "puppeteer", "selenium"):
        assert banned not in lowered, f"community_policy.py 不应引用 {banned}"


def test_community_sources_has_no_browser_automation():
    """community_sources.py 仅是 CLI 适配器接缝，不得含浏览器自动化。"""
    content = _read_or_empty("wrr/engines/community_sources.py")
    assert content, "community_sources.py 必须存在"
    lowered = content.lower()
    for banned in ("playwright", "puppeteer", "selenium"):
        assert banned not in lowered, f"community_sources.py 不应引用 {banned}"


def test_community_engine_does_not_import_policy():
    """search 热路径（community.py）不得引用禁用策略脚手架。"""
    content = _read_or_empty("wrr/engines/community.py")
    assert "community_policy" not in content, (
        "community.py 不应导入 community_policy（Slice 1 无热路径接线）"
    )
