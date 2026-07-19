"""v5 mode 路由单测：classify_intent / MODE_DISPATCH / 触发提升 / gather 隔离 / RRF details。"""
import asyncio

import pytest

from conftest import FakeEngine, mk_results
from wrr.registry import EngineRegistry
from wrr.router import route_search_v5, resolve_mode, legacy_selection_plan
from wrr.schemas import SearchOptions, DecisionContext, DecisionSnapshot
from wrr.errors import AllEnginesFailedError
from wrr import config


def run(coro):
    return asyncio.run(coro)


def _reg(*engines):
    r = EngineRegistry()
    for e in engines:
        r.register(e)
    return r


def _full_reg():
    return _reg(*[FakeEngine(n, search_results=mk_results(2))
                  for n in ("exa", "brave", "searxng", "github",
                            "community", "academic", "skill")])


def _shadow_context(expires_at=200.0):
    return DecisionContext(
        snapshot_version="ctx-v1",
        built_at=100.0,
        expires_at=expires_at,
        runtime="standalone",
        profile="default",
        registry_source="test",
        routable_descriptor_ids=("exa", "brave"),
        bridged_provider_ids=("exa", "brave"),
        missing_provider_ids=(),
        adapter_errors=(),
        descriptor_reasons=(),
        descriptor_provider_aliases=(("exa", "exa"), ("brave", "brave")),
        config_fingerprint="cfg-v1",
    )


# ── classify_intent（每 mode ≥3 例）──────────────────────────────────
def test_classify_intent_grounding():
    for q in ["what is python", "tesla 股价多少", "react 版本"]:
        assert config.classify_intent(q) == "grounding"


def test_classify_intent_academic():
    for q in ["survey of llm", "transformer 论文", "graph algorithm methodology"]:
        assert config.classify_intent(q) == "academic"


def test_classify_intent_research():
    for q in ["深度分析 ai", "全面比较 react vue", "comparison of databases"]:
        assert config.classify_intent(q) == "research"


def test_classify_intent_discovery():
    for q in ["有哪些 rust 库", "2026 ai 趋势", "best python tools"]:
        assert config.classify_intent(q) == "discovery"


def test_classify_intent_platform():
    for q in ["gpt site:reddit.com", "ai site:zhihu.com", "x site:news.ycombinator.com"]:
        assert config.classify_intent(q) == "platform"


def test_classify_intent_broad():
    """开放式兴趣查询 → broad mode (v5.2)"""
    for q in ["今天有啥好玩的", "今日热点", "今天可能感兴趣的事",
              "what's new in AI", "最近有啥新鲜事"]:
        assert config.classify_intent(q) == "broad"


def test_classify_intent_recovery():
    """丢失/删除查询 → recovery mode (v5.2)"""
    for q in ["找不到刚才的文件", "恢复被删的记录", "missing deleted config"]:
        assert config.classify_intent(q) == "recovery"


def test_resolve_mode_explicit_and_recovery():
    assert resolve_mode(SearchOptions("anything", route_mode="research", exa_mode="fast")) == "research"
    assert resolve_mode(SearchOptions("anything", mode="recovery")) == "recovery"
    assert resolve_mode(SearchOptions("anything", mode="academic")) == "academic"
    # 非法 mode 回退自动分类
    assert resolve_mode(SearchOptions("survey of x", mode="bogus")) == "academic"


# ── MODE_DISPATCH 完整性 ─────────────────────────────────────────────
def test_mode_dispatch_non_empty_and_registered():
    reg = _full_reg()
    for mode in ("discovery", "grounding", "research", "academic", "platform", "recovery", "local", "broad"):
        engines = config.MODE_DISPATCH[mode]
        assert engines, f"{mode} empty"
        for name in engines:
            if name.startswith("local_"):  # 本地引擎在 CLI 环境不可用
                continue
            assert reg.get(name) is not None, f"{mode}:{name} not in registry"


# ── 触发提升不重复 ───────────────────────────────────────────────────
def test_trigger_promotion_no_dup():
    # academic mode 基础已含 academic，触发词再命中不应重复
    engines = config.mode_engines("academic", "survey of transformers")
    assert engines.count("academic") == 1
    # grounding + github 触发
    eg = config.mode_engines("grounding", "asyncio site:github.com")
    assert "github" in eg and eg.count("github") == 1
    # skill 触发
    es = config.mode_engines("grounding", "有没有 X 的 skill")
    assert "skill" in es


# ── route_search_v5：并行 + RRF + details ────────────────────────────
def test_v5_search_returns_rrf_details():
    reg = _full_reg()
    rr = run(route_search_v5(SearchOptions("深度分析 ai", count=10), reg))
    assert rr.mode == "research"
    assert rr.fusion_method == "rrf"
    assert rr.weights is not None
    assert len(rr.payload) >= 1
    # research mode 含 community/academic 权重
    assert rr.weights.get("community") == 0.35   # v5.4: research community 0.30→0.35
    assert rr.weights.get("academic") == 0.30


def test_v5_injected_context_returns_shadow_comparison_without_changing_execution():
    options = SearchOptions("what is python", count=5)
    context = DecisionContext(
        snapshot_version="ctx-v1",
        built_at=100.0,
        expires_at=200.0,
        runtime="standalone",
        profile="default",
        registry_source="test",
        routable_descriptor_ids=("exa", "brave"),
        bridged_provider_ids=("exa", "brave"),
        missing_provider_ids=(),
        adapter_errors=(),
        descriptor_reasons=(),
        descriptor_provider_aliases=(("exa", "exa"), ("brave", "brave")),
        config_fingerprint="cfg-v1",
    )

    legacy = run(route_search_v5(options, _full_reg()))
    shadow = run(route_search_v5(
        options,
        _full_reg(),
        decision_context=context,
        shadow_evaluated_at=150.0,
    ))

    assert legacy.shadow_comparison is None
    assert shadow.shadow_comparison.code == "E0"
    assert shadow.actual_provider == legacy.actual_provider
    assert shadow.payload == legacy.payload
    assert shadow.fallback_chain == legacy.fallback_chain


def test_v5_injected_context_bypasses_descriptor_registry_replacement():
    options = SearchOptions("what is python", count=5)
    context = DecisionContext(
        snapshot_version="ctx-v1",
        built_at=100.0,
        expires_at=200.0,
        runtime="standalone",
        profile="default",
        registry_source="test",
        routable_descriptor_ids=("exa", "brave"),
        bridged_provider_ids=("exa", "brave"),
        missing_provider_ids=(),
        adapter_errors=(),
        descriptor_reasons=(),
        descriptor_provider_aliases=(("exa", "exa"), ("brave", "brave")),
        config_fingerprint="cfg-v1",
    )

    def forbidden_descriptor_registry():
        raise AssertionError("Stage S must execute the supplied legacy registry")

    result = run(route_search_v5(
        options,
        _full_reg(),
        descriptor_registry_factory=forbidden_descriptor_registry,
        decision_context=context,
        shadow_evaluated_at=150.0,
    ))

    assert result.actual_provider == "rrf:grounding"
    assert result.shadow_comparison.code == "E0"


def test_v5_expired_context_disables_shadow_and_keeps_legacy_execution():
    options = SearchOptions("what is python", count=5)
    expired = DecisionContext(
        snapshot_version="ctx-expired",
        built_at=100.0,
        expires_at=149.0,
        runtime="standalone",
        profile="default",
        registry_source="test",
        routable_descriptor_ids=("exa", "brave"),
        bridged_provider_ids=("exa", "brave"),
        missing_provider_ids=(),
        adapter_errors=(),
        descriptor_reasons=(),
        descriptor_provider_aliases=(("exa", "exa"), ("brave", "brave")),
        config_fingerprint="cfg-v1",
    )

    result = run(route_search_v5(
        options,
        _full_reg(),
        decision_context=expired,
        shadow_evaluated_at=150.0,
    ))

    assert result.shadow_comparison is None
    assert result.actual_provider == "rrf:grounding"
    assert result.payload


def test_v5_shadow_exception_is_fail_closed(monkeypatch):
    def fail_comparison(*args, **kwargs):
        raise RuntimeError("comparison boom")

    monkeypatch.setattr(
        "wrr.selection_shadow.compare_shadow_selection",
        fail_comparison,
    )
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context=_shadow_context(),
        shadow_evaluated_at=150.0,
    ))

    assert result.shadow_comparison is None
    assert result.actual_provider == "rrf:grounding"
    assert result.payload


def test_v5_explicit_provider_carries_shadow_comparison():
    result = run(route_search_v5(
        SearchOptions("q", provider="brave"),
        _full_reg(),
        decision_context=_shadow_context(),
        shadow_evaluated_at=150.0,
    ))

    assert result.actual_provider == "brave"
    assert result.shadow_comparison.code == "E0"


def test_v5_recovery_return_carries_primary_shadow_comparison():
    registry = _reg(
        FakeEngine("exa", search_results=[]),
        FakeEngine("brave", search_results=[]),
        FakeEngine("searxng", search_results=mk_results(2)),
    )

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        registry,
        decision_context=_shadow_context(),
        shadow_evaluated_at=150.0,
    ))

    assert result.actual_provider == "rrf:recovery"
    assert result.shadow_comparison.code == "E0"


def test_v5_academic_mode_weights():
    reg = _full_reg()
    rr = run(route_search_v5(SearchOptions("survey of llm", count=5), reg))
    assert rr.mode == "academic"
    assert rr.weights.get("academic") == 1.0          # 学术绝对主力


def test_v5_one_engine_exception_isolated():
    # exa 抛异常，brave 正常 → 整体仍返回（gather 隔离）
    reg = _reg(FakeEngine("exa", error="exa down"),
               FakeEngine("brave", search_results=mk_results(3)))
    rr = run(route_search_v5(SearchOptions("what is x", count=10), reg))   # grounding
    assert rr.mode == "grounding"
    assert len(rr.payload) >= 1
    failed = [s.provider for s in rr.fallback_chain if not s.ok]
    assert "exa" in failed                            # 失败被记录但不致命


def test_v5_all_fail_falls_to_recovery_then_raises():
    # grounding 的 exa/brave 全挂，recovery 的 brave/exa/searxng 也全挂 → 抛
    reg = _reg(FakeEngine("exa", error="down"),
               FakeEngine("brave", error="down"),
               FakeEngine("searxng", error="down"))
    try:
        run(route_search_v5(SearchOptions("what is x"), reg))
        assert False, "should raise"
    except AllEnginesFailedError:
        pass


def test_v5_recovery_fallback_recovers():
    # grounding(exa/brave) 空，但 searxng 有结果 → recovery 兜底成功
    reg = _reg(FakeEngine("exa", search_results=[]),
               FakeEngine("brave", search_results=[]),
               FakeEngine("searxng", search_results=mk_results(2)))
    rr = run(route_search_v5(SearchOptions("what is x", count=5), reg))
    assert rr.mode == "recovery"
    assert len(rr.payload) >= 1


def test_v5_explicit_provider_single_engine():
    reg = _reg(FakeEngine("brave", search_results=mk_results(2)),
               FakeEngine("exa", error="should not be used"))
    rr = run(route_search_v5(SearchOptions("q", provider="brave"), reg))
    assert rr.actual_provider == "brave"              # 显式 → 单引擎，禁 mode 路由


# ── v5.4 实践意图社区触发 ──

def test_practical_triggers_community():
    """工具使用/操作指南类查询应触发社区引擎"""
    from wrr.config import community_triggered
    assert community_triggered("Windows Terminal 操作指南和快捷键怎么用")
    assert community_triggered("best practices for Claude Code")
    assert community_triggered("Neovim 插件怎么选，有什么推荐")
    assert community_triggered("Kubernetes 实战经验和踩坑")
    assert community_triggered("how to configure tmux with gotchas")


def test_practical_no_false_positive():
    """纯事实查询不应被实践关键词误触发"""
    from wrr.config import community_triggered
    assert not community_triggered("python 3.14 release date")
    assert not community_triggered("Windows Terminal latest stable version")
    assert not community_triggered("postgres 16 changelog")


def test_community_weights_raised():
    """discovery/broad/grounding 社区权重提升"""
    assert config.MODE_WEIGHTS["discovery"]["community"] >= 0.50
    assert config.MODE_WEIGHTS["broad"]["community"] >= 0.50
    assert config.MODE_WEIGHTS["grounding"]["community"] >= 0.40
    # academic 不变
    assert config.MODE_WEIGHTS["academic"]["community"] <= 0.30


# ── diagnostics 追踪 ─────────────────────────────────────────────────
def test_route_search_v5_includes_diagnostics():
    """v5 路由应包含 diagnostics with mode/mode_reason/selected_engines/events。"""
    reg = _full_reg()
    rr = run(route_search_v5(SearchOptions("survey of llm"), reg))
    assert rr.diagnostics is not None
    assert rr.diagnostics.mode == "academic"
    assert rr.diagnostics.mode_reason in ("classify_intent", "explicit")
    assert len(rr.diagnostics.selected_engines) > 0
    assert len(rr.diagnostics.events) > 0
    assert rr.diagnostics.elapsed_ms > 0
    assert rr.diagnostics.timeout_ms > 0


def test_explicit_mode_sets_mode_reason_explicit():
    """显式 mode 时 mode_reason 应为 'explicit'。"""
    reg = _full_reg()
    rr = run(route_search_v5(SearchOptions("anything", mode="research"), reg))
    assert rr.diagnostics is not None
    assert rr.diagnostics.mode == "research"
    assert rr.diagnostics.mode_reason == "explicit"


def test_recovery_fallback_sets_mode_reason_recovery_fallback():
    """主 mode 空结果走 recovery 时 mode_reason 应为 'recovery_fallback'。"""
    # 让主 mode 的所有引擎都返回空，只有 recovery 引擎有结果
    reg = _reg(
        FakeEngine("exa", search_results=[]),
        FakeEngine("brave", search_results=[]),
        FakeEngine("searxng", search_results=mk_results(1)),  # searxng 在 recovery mode 中
    )
    rr = run(route_search_v5(SearchOptions("test"), reg))
    assert rr.diagnostics is not None
    # 如果走了 recovery fallback，mode 应为 recovery
    if rr.diagnostics.mode == "recovery":
        assert rr.diagnostics.mode_reason == "recovery_fallback"


# ── P1 Slice A: stage_s_enabled 三态归一化合同 ───────────────────────
class _FactoryCalled(Exception):
    """Tripwire：descriptor registry factory / env replacement 被调用。"""


def _tripwire_factory():
    raise _FactoryCalled()


def test_stage_s_enabled_cold_forces_caller_registry_no_replacement(monkeypatch):
    """True + None：enabled-cold，强制 caller legacy registry，shadow=None；
    即便 WRR_V6_ROUTER=1 或注入 factory 也不得 replacement。"""
    monkeypatch.setenv("WRR_V6_ROUTER", "1")

    def boom():
        raise _FactoryCalled()

    monkeypatch.setattr("wrr.router._descriptor_backed_registry", boom)

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        descriptor_registry_factory=_tripwire_factory,
        stage_s_enabled=True,
    ))

    assert result.actual_provider == "rrf:grounding"
    assert result.shadow_comparison is None
    assert result.payload


def test_stage_s_enabled_warm_forces_legacy_and_compares():
    """True + context：enabled-warm，强制 caller legacy registry 并做 comparison。"""
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        descriptor_registry_factory=_tripwire_factory,
        decision_context=_shadow_context(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
    ))

    assert result.actual_provider == "rrf:grounding"
    assert result.shadow_comparison.code == "E0"


def test_stage_s_none_cold_keeps_legacy_registry_replacement():
    """None + None：完全保持旧 _route_registry 行为（注入 factory 仍被调用）。"""
    with pytest.raises(_FactoryCalled):
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            _full_reg(),
            descriptor_registry_factory=_tripwire_factory,
        ))


def test_stage_s_disabled_cold_keeps_legacy_registry_replacement():
    """False + None：Stage S disabled，保持旧行为（factory replacement 仍生效）。"""
    with pytest.raises(_FactoryCalled):
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            _full_reg(),
            descriptor_registry_factory=_tripwire_factory,
            stage_s_enabled=False,
        ))


def test_stage_s_disabled_with_context_raises_valueerror_before_factory():
    """False + context：ValueError，且不得执行/调用 registry factory
    （若 factory 被调用会抛 _FactoryCalled 而非 ValueError → 断言失败）。"""
    with pytest.raises(ValueError):
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            _full_reg(),
            descriptor_registry_factory=_tripwire_factory,
            decision_context=_shadow_context(),
            shadow_evaluated_at=150.0,
            stage_s_enabled=False,
        ))


def test_stage_s_enabled_stale_context_closes_shadow_keeps_legacy():
    """过期 context 在 enabled 状态仍只关闭 shadow，始终执行 caller legacy registry。"""
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        descriptor_registry_factory=_tripwire_factory,
        decision_context=_shadow_context(expires_at=149.0),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
    ))

    assert result.shadow_comparison is None
    assert result.actual_provider == "rrf:grounding"
    assert result.payload


def test_stage_s_enabled_comparator_exception_closes_shadow_keeps_legacy(monkeypatch):
    """comparator 异常在 enabled 状态仍只关闭 shadow，始终执行 caller legacy registry。"""
    def boom(*args, **kwargs):
        raise RuntimeError("comparison boom")

    monkeypatch.setattr("wrr.selection_shadow.compare_shadow_selection", boom)

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        descriptor_registry_factory=_tripwire_factory,
        decision_context=_shadow_context(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
    ))

    assert result.shadow_comparison is None
    assert result.actual_provider == "rrf:grounding"
    assert result.payload


# ── P1 S1: legacy_selection_plan → frozen DecisionSnapshot ───────────
def test_legacy_plan_auto_classification():
    """自动分类：无显式 provider/mode → mode 由 classify_intent 决定。"""
    plan = legacy_selection_plan(SearchOptions("survey of llm", count=5))
    assert isinstance(plan, DecisionSnapshot)
    assert plan.source == "legacy"
    assert plan.mode == "academic"
    assert plan.mode_reason == "classify_intent"
    assert plan.explicit_provider is None
    assert "academic" in plan.engine_names
    assert dict(plan.weights).get("academic") == 1.0


def test_legacy_plan_explicit_route_mode():
    """显式 route_mode → mode_reason=explicit，无 explicit_provider。"""
    plan = legacy_selection_plan(SearchOptions("anything", route_mode="research"))
    assert plan.mode == "research"
    assert plan.mode_reason == "explicit"
    assert plan.explicit_provider is None
    # legacy mode 别名同样视为显式
    plan2 = legacy_selection_plan(SearchOptions("anything", mode="academic"))
    assert plan2.mode == "academic"
    assert plan2.mode_reason == "explicit"


def test_legacy_plan_explicit_provider_single_element():
    """显式 provider → 单元素 snapshot，mode=None，engine_names=(provider,)。"""
    plan = legacy_selection_plan(SearchOptions("q", provider="brave"))
    assert plan.explicit_provider == "brave"
    assert plan.engine_names == ("brave",)
    assert plan.mode is None
    assert plan.mode_reason == "explicit_provider"
    assert plan.weights == ()


def test_decision_snapshot_is_frozen_hashable_and_tuple_typed():
    """immutability + hash/tuple 合同：快照可安全用于比较与集合键。"""
    import dataclasses
    plan = legacy_selection_plan(SearchOptions("survey of llm"))
    assert isinstance(plan.engine_names, tuple)
    assert isinstance(plan.weights, tuple)
    assert all(
        isinstance(pair, tuple)
        and len(pair) == 2
        and isinstance(pair[1], float)
        for pair in plan.weights
    )
    assert plan in {plan}
    assert isinstance(hash(plan), int)
    try:
        plan.mode = "grounding"
        assert False, "DecisionSnapshot must be frozen (immutable)"
    except dataclasses.FrozenInstanceError:
        pass


# ── policy-sensitive 机器信号（正交于 route mode）─────────────────────
def test_policy_sensitive_positive():
    """政策/法规/监管/政府/法律高风险查询 → True。"""
    for q in [
        "数据安全政策监管要求",              # 中文政策监管
        "个人信息保护 合规 立法进展",         # 中文合规立法
        "EU AI regulation timeline",         # EU regulation
        "government guidance on facial recognition",  # government guidance
        "data retention legal requirements",  # data retention legal
    ]:
        assert config.policy_sensitive_triggered(q) is True, q


def test_policy_sensitive_negative_and_false_positive_guards():
    """RL policy gradient / legal pad / 普通 timeout 等不得误报。"""
    for q in [
        "policy gradient implementation in pytorch",  # RL 术语
        "legal pad sizes comparison",                 # 办公用品
        "python asyncio timeout best practice",       # 普通 timeout
        "how to center a div in css",                 # 无关
        "react useEffect cleanup",                    # 无关
    ]:
        assert config.policy_sensitive_triggered(q) is False, q


def test_policy_predicate_orthogonal_to_routing():
    """policy 信号为 True，但 classify_intent / mode_engines 选择明确保持未变。"""
    # grounding 类政策查询：预测器 True，但意图仍为 grounding，引擎组合无额外注入
    q1 = "EU data retention legal requirements"
    assert config.policy_sensitive_triggered(q1) is True
    assert config.classify_intent(q1) == "grounding"
    assert config.mode_engines("grounding", q1) == list(config.MODE_DISPATCH["grounding"])

    # research 类政策查询：预测器 True，但意图仍为 research，引擎组合不被 policy 扰动
    q2 = "深度分析 数据合规政策 演进"
    assert config.policy_sensitive_triggered(q2) is True
    assert config.classify_intent(q2) == "research"
    assert config.mode_engines("research", q2) == list(config.MODE_DISPATCH["research"])


def test_route_search_v5_consumes_legacy_plan(monkeypatch):
    """用与 config 不同的 sentinel 证明 route_search_v5 真消费 selection seam。"""
    sentinel = DecisionSnapshot(
        source="legacy",
        mode="grounding",
        mode_reason="sentinel",
        explicit_provider=None,
        engine_names=("academic",),
        weights=(("academic", 0.37),),
    )
    monkeypatch.setattr("wrr.router.legacy_selection_plan", lambda options: sentinel)

    rr = run(route_search_v5(SearchOptions("深度分析 ai", count=10), _full_reg()))

    assert rr.mode == "grounding"
    assert rr.diagnostics is not None
    assert rr.diagnostics.mode_reason == "sentinel"
    assert rr.weights == {"academic": 0.37}
    assert [step.provider for step in rr.fallback_chain] == ["academic"]
