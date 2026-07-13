"""plugin loader 入口契约测试（CC-R2-P1）。

覆盖：
  - 顶层 import-light（不拉起 httpx / yaml / wrr.router / wrr.doctor /
    wrr.engines.loader）；
  - 存在 callable ``register(ctx)``；
  - MockCtx 调用后注册 web_search / web_fetch / web_similar 3 个 tool；
  - 三者 is_async=True、toolset="wrr"；
  - OpenAI function schema 的 parameters.required 正确（query / url / url）。
"""
import asyncio
import importlib.util
import re
import subprocess
import sys
import threading
from pathlib import Path

ENTRY = Path(__file__).resolve().parents[2] / "__init__.py"
PLUGIN_MANIFEST = ENTRY.parent / "plugin.yaml"
PYPROJECT = ENTRY.parent / "pyproject.toml"
PACKAGE_INIT = ENTRY.parent / "wrr" / "__init__.py"

FORBIDDEN = ["httpx", "yaml", "wrr.router", "wrr.doctor", "wrr.engines.loader"]


def _load_entry():
    """以独立模块名加载 root __init__.py，避免与包导入冲突。"""
    spec = importlib.util.spec_from_file_location("wrr_plugin_entry", ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MockCtx:
    def __init__(self):
        self.tools = {}
        self.hooks = []

    def register_tool(self, name, handler, schema, toolset, is_async, override=False):
        self.tools[name] = {
            "handler": handler,
            "schema": schema,
            "toolset": toolset,
            "is_async": is_async,
            "override": override,
        }

    def register_hook(self, event, handler):
        self.hooks.append({"event": event, "handler": handler})


def _version_from(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None, f"version declaration missing from {path}"
    return match.group(1)


def test_canonical_versions_match():
    """Plugin entry、manifest、package metadata 必须声明同一版本。"""
    sources = {
        "plugin_entry": _load_entry().__version__,
        "plugin_manifest": _version_from(PLUGIN_MANIFEST, r"^version:\s*(\S+)\s*$"),
        "pyproject": _version_from(PYPROJECT, r'^version\s*=\s*"([^"]+)"\s*$'),
        "package": _version_from(PACKAGE_INIT, r'^__version__\s*=\s*"([^"]+)"\s*$'),
    }
    assert len(set(sources.values())) == 1, sources


def test_top_level_import_is_light():
    """子进程纯净导入 entry，断言重依赖未被顶层拉起。"""
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('e', r'{ENTRY}')\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        f"bad = [n for n in {FORBIDDEN!r} if n in sys.modules]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "OK" in proc.stdout


def test_register_loads_from_non_repo_cwd():
    """模拟真实 Hermes discovery：cwd=/tmp，spec_from_file_location 加载 root entry，
    plugin_dir 不在 sys.path。register(ctx) 必须自行修复 sys.path 并成功注册 3 个 tool。

    当测试本身以 editable install 运行时，``wrr`` 可能通过 pth/egg-link 在 /tmp
    也可解析；前置断言会过强。此处在把 plugin_dir 从 sys.path 移除后验证
    register 仍然能成功注册三个 tool，核心契约保持不变。
    """
    plugin_dir = str(ENTRY.parent)
    code = (
        "import importlib.util, sys, os\n"
        # 移除 repo 路径，让 wrr 至少不通过本地 sys.path 解析；editable install
        # 仍可能通过 site-packages 的 .pth 解析，这是测试环境差异，不破坏契约。
        f"sys.path[:] = [p for p in sys.path if os.path.realpath(p) != os.path.realpath({plugin_dir!r})]\n"
        # Hermes loader 风格：submodule_search_locations=[plugin_dir]，但不动 sys.path
        f"spec = importlib.util.spec_from_file_location('wrr_plugin_entry', r'{ENTRY}', submodule_search_locations=[{plugin_dir!r}])\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "class Ctx:\n"
        "    def __init__(self): self.tools = {}; self.hooks = []\n"
        "    def register_tool(self, name, handler, schema, toolset, is_async, override=False):\n"
        "        self.tools[name] = {'toolset': toolset, 'is_async': is_async, 'override': override}\n"
        "    def register_hook(self, event, handler): self.hooks.append(event)\n"
        "ctx = Ctx()\n"
        "m.register(ctx)\n"
        "assert set(ctx.tools) == {'web_search', 'web_fetch', 'web_similar'}, ctx.tools\n"
        "assert ctx.tools['web_search']['override'] is True\n"
        "assert 'pre_llm_call' in ctx.hooks, ctx.hooks\n"
        "assert all(t['is_async'] is True and t['toolset'] == 'wrr' for t in ctx.tools.values())\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd="/tmp",
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "OK" in proc.stdout


def test_register_is_callable():
    mod = _load_entry()
    assert callable(getattr(mod, "register", None))


def test_register_registers_three_tools():
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)
    assert set(ctx.tools) == {"web_search", "web_fetch", "web_similar"}


def test_tools_are_async_and_wrr_toolset():
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)
    for name, t in ctx.tools.items():
        assert t["is_async"] is True, name
        assert t["toolset"] == "wrr", name
        assert callable(t["handler"]), name


def test_web_search_overrides_builtin():
    """web_search 与内建同名，必须 override=True；另两个保持 False。"""
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)
    assert ctx.tools["web_search"]["override"] is True
    assert ctx.tools["web_fetch"]["override"] is False
    assert ctx.tools["web_similar"]["override"] is False


def test_schema_required_fields():
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)
    assert ctx.tools["web_search"]["schema"]["parameters"]["required"] == ["query"]
    assert ctx.tools["web_fetch"]["schema"]["parameters"]["required"] == ["url"]
    assert ctx.tools["web_similar"]["schema"]["parameters"]["required"] == ["url"]


def test_schemas_are_openai_function_format():
    """Hermes registry forwards schema as-is; parameters must be nested.

    A bare JSON schema reaches the model without an argument schema and can
    produce empty tool calls (observed in Agent-in-loop E2E). Keep this as a
    regression test for plugin tools that override Hermes built-ins.
    """
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)
    for name, t in ctx.tools.items():
        schema = t["schema"]
        assert schema["name"] == name
        assert isinstance(schema.get("description"), str) and schema["description"]
        assert schema.get("parameters", {}).get("type") == "object", name
        assert "properties" in schema["parameters"], name


def test_schema_properties_align_with_handlers():
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)
    assert set(ctx.tools["web_search"]["schema"]["parameters"]["properties"]) == {
        "query", "max_results", "provider", "mode",
    }
    assert set(ctx.tools["web_fetch"]["schema"]["parameters"]["properties"]) == {
        "url", "max_characters", "provider",
    }
    assert set(ctx.tools["web_similar"]["schema"]["parameters"]["properties"]) == {
        "url", "max_results", "provider",
    }


def test_provider_schema_rejects_fusion_labels():
    """`provider` is input-only and must be a concrete engine name.

    Agent-in-loop E2E showed the model copying output provider labels such as
    `rrf:grounding` back into the input provider field. The enum and wording
    should make that invalid at schema level.
    """
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)
    search_provider = ctx.tools["web_search"]["schema"]["parameters"]["properties"]["provider"]
    assert "enum" in search_provider
    assert "exa" in search_provider["enum"]
    assert "brave" in search_provider["enum"]
    assert "rrf:grounding" not in search_provider["enum"]
    assert "rrf:" in search_provider["description"]

    fetch_provider = ctx.tools["web_fetch"]["schema"]["parameters"]["properties"]["provider"]
    assert fetch_provider["enum"] == ["exa", "brave"]

    similar_provider = ctx.tools["web_similar"]["schema"]["parameters"]["properties"]["provider"]
    assert similar_provider["enum"] == ["exa"]


# ── web_search 显式依赖执行 seam 合同 ─────────────────────────────────────

async def _spy_route_factory(calls):
    async def _route(options, registry, *, descriptor_registry_factory=None,
                     decision_context=None, shadow_evaluated_at=None,
                     stage_s_enabled=None, decision_evidence_sink=None):
        calls.append({
            "options": options, "registry": registry,
            "decision_context": decision_context, "stage_s_enabled": stage_s_enabled,
            "decision_evidence_sink": decision_evidence_sink,
        })
        return object()
    return _route


def test_execute_web_search_seam_passes_explicit_deps(monkeypatch):
    """execute_web_search 把显式 registry / decision_context / stage_s_enabled /
    decision_evidence_sink 原样透传给 route_search_v5，并复用 format 逻辑。"""
    import wrr.tools.web_search as ws

    calls = []
    async def _route(options, registry, *, descriptor_registry_factory=None,
                     decision_context=None, shadow_evaluated_at=None,
                     stage_s_enabled=None, decision_evidence_sink=None):
        calls.append({"registry": registry, "decision_context": decision_context,
                      "stage_s_enabled": stage_s_enabled, "query": options.query,
                      "decision_evidence_sink": decision_evidence_sink})
        return object()
    monkeypatch.setattr(ws, "route_search_v5", _route)
    monkeypatch.setattr(ws, "format_search", lambda result, query: f"FORMATTED:{query}")

    reg = object()
    dctx = object()
    sink = object()
    out = asyncio.run(ws.execute_web_search(
        {"query": "hi"}, registry=reg, decision_context=dctx, stage_s_enabled=True,
        decision_evidence_sink=sink))

    assert out == "FORMATTED:hi"
    assert calls[-1]["registry"] is reg
    assert calls[-1]["decision_context"] is dctx
    assert calls[-1]["stage_s_enabled"] is True
    assert calls[-1]["query"] == "hi"
    # sink 身份原样透传给 router（exact object，无中转/复制）。
    assert calls[-1]["decision_evidence_sink"] is sink


def test_handle_web_search_defaults_to_get_registry_and_legacy(monkeypatch):
    """兼容入口默认 get_registry()，不注入 Stage S 依赖（context None / stage None）。"""
    import wrr.tools.web_search as ws

    sentinel_registry = object()
    calls = []
    async def _route(options, registry, *, descriptor_registry_factory=None,
                     decision_context=None, shadow_evaluated_at=None,
                     stage_s_enabled=None, decision_evidence_sink=None):
        calls.append({"registry": registry, "decision_context": decision_context,
                      "stage_s_enabled": stage_s_enabled,
                      "decision_evidence_sink": decision_evidence_sink})
        return object()
    monkeypatch.setattr(ws, "route_search_v5", _route)
    monkeypatch.setattr(ws, "format_search", lambda result, query: "OK")
    monkeypatch.setattr(ws, "get_registry", lambda: sentinel_registry)

    out = asyncio.run(ws.handle_web_search({"query": "hi"}))

    assert out == "OK"
    assert calls[-1]["registry"] is sentinel_registry
    assert calls[-1]["decision_context"] is None
    assert calls[-1]["stage_s_enabled"] is None
    # 兼容入口从不注入 sink。
    assert calls[-1]["decision_evidence_sink"] is None


def test_handle_web_search_ignores_magic_kwargs_as_deps(monkeypatch):
    """**kwargs 只为签名兼容，不得被当作依赖注入通道。"""
    import wrr.tools.web_search as ws

    sentinel_registry = object()
    calls = []
    async def _route(options, registry, *, descriptor_registry_factory=None,
                     decision_context=None, shadow_evaluated_at=None,
                     stage_s_enabled=None, decision_evidence_sink=None):
        calls.append({"registry": registry, "decision_context": decision_context,
                      "stage_s_enabled": stage_s_enabled,
                      "decision_evidence_sink": decision_evidence_sink})
        return object()
    monkeypatch.setattr(ws, "route_search_v5", _route)
    monkeypatch.setattr(ws, "format_search", lambda result, query: "OK")
    monkeypatch.setattr(ws, "get_registry", lambda: sentinel_registry)

    asyncio.run(ws.handle_web_search(
        {"query": "hi"}, decision_context=object(), stage_s_enabled=False,
        registry=object(), decision_evidence_sink=object()))

    assert calls[-1]["registry"] is sentinel_registry
    assert calls[-1]["decision_context"] is None
    assert calls[-1]["stage_s_enabled"] is None
    # magic kwargs（含 decision_evidence_sink）不得成为注入通道。
    assert calls[-1]["decision_evidence_sink"] is None


# ── root register wiring：同源 legacy registry / provider / hook ──────────

_SENTINEL_REGISTRY = object()
_SENTINEL_CONTEXT = object()
_RAISE = object()


class _FakeProvider:
    """Records builder identity and get()/refresh() call counts for register tests."""

    def __init__(self, builder):
        self.builder = builder
        self.get_calls = 0
        self.refresh_calls = 0
        self.snapshot = None
        self.refresh_result = None

    def get(self):
        self.get_calls += 1
        return self.snapshot

    def refresh(self):
        self.refresh_calls += 1
        if self.refresh_result is _RAISE:
            raise RuntimeError("assembly boom")
        self.snapshot = self.refresh_result
        return self.snapshot


def _install_register_fakes(monkeypatch, *, snapshot=None, refresh_result=_RAISE):
    """Patch register()'s lazy deps; return (created_providers, build_calls)."""
    import wrr.registry
    import wrr.runtime.decision_context_assembly as dca
    import wrr.runtime.decision_context_provider as dcp

    monkeypatch.setattr(wrr.registry, "get_registry", lambda: _SENTINEL_REGISTRY)

    build_calls = []
    def _fake_build(legacy, **kwargs):
        build_calls.append(legacy)
        return _SENTINEL_CONTEXT
    monkeypatch.setattr(dca, "build_control_plane_decision_context", _fake_build)

    created = []
    def _factory(builder):
        p = _FakeProvider(builder)
        p.snapshot = snapshot
        p.refresh_result = refresh_result
        created.append(p)
        return p
    monkeypatch.setattr(dcp, "CachedDecisionContextProvider", _factory)

    return created, build_calls


def _install_exec_spy(monkeypatch):
    import wrr.tools.web_search as ws
    exec_calls = []
    async def _fake_exec(args, *, registry, decision_context=None, stage_s_enabled=None,
                         decision_evidence_sink=None):
        exec_calls.append({"registry": registry, "decision_context": decision_context,
                           "stage_s_enabled": stage_s_enabled,
                           "decision_evidence_sink": decision_evidence_sink})
        return "OK"
    monkeypatch.setattr(ws, "execute_web_search", _fake_exec)
    return exec_calls


def _install_sink_fakes(monkeypatch, *, jsonl_raises=False):
    """Patch register()'s lazy sink classes; return (jsonl_attempts, jsonl_created, noop_created)."""
    import wrr.runtime.decision_evidence as de

    jsonl_attempts = []
    jsonl_created = []
    noop_created = []

    class _FakeJsonl:
        def __init__(self, *args, **kwargs):
            jsonl_attempts.append(self)
            if jsonl_raises:
                raise RuntimeError("sink construction boom")
            jsonl_created.append(self)

    class _FakeNoop:
        def __init__(self, *args, **kwargs):
            noop_created.append(self)

    monkeypatch.setattr(de, "JsonlDecisionEvidenceSink", _FakeJsonl)
    monkeypatch.setattr(de, "NoopDecisionEvidenceSink", _FakeNoop)
    return jsonl_attempts, jsonl_created, noop_created


def test_register_constructs_one_jsonl_sink_reused_across_calls(monkeypatch):
    """组合层拥有唯一 sink：register 恰构造一个 Jsonl sink（无 Noop fallback），
    bound handler 每次请求复用同一个 sink 对象（exact identity）。"""
    _install_register_fakes(monkeypatch, snapshot=None, refresh_result=_RAISE)
    exec_calls = _install_exec_spy(monkeypatch)
    jsonl_attempts, jsonl_created, noop_created = _install_sink_fakes(monkeypatch)
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)

    assert len(jsonl_attempts) == 1        # exactly one Jsonl construction per register
    assert len(jsonl_created) == 1
    assert noop_created == []              # constructor succeeded → no fallback
    the_sink = jsonl_created[0]

    handler = ctx.tools["web_search"]["handler"]
    asyncio.run(handler({"query": "a"}))
    asyncio.run(handler({"query": "b"}))

    # 两次以上请求复用同一个 sink 对象，绝不 per-request 构造。
    assert len(jsonl_attempts) == 1
    assert exec_calls[0]["decision_evidence_sink"] is the_sink
    assert exec_calls[1]["decision_evidence_sink"] is the_sink


def test_register_sink_constructor_failure_falls_back_to_noop(monkeypatch):
    """Jsonl 构造抛错时 register 存活：恰一个 Noop fallback，bound handler 拿到该 fallback。"""
    _install_register_fakes(monkeypatch, snapshot=None, refresh_result=_RAISE)
    exec_calls = _install_exec_spy(monkeypatch)
    jsonl_attempts, jsonl_created, noop_created = _install_sink_fakes(
        monkeypatch, jsonl_raises=True)
    mod = _load_entry()
    ctx = MockCtx()

    mod.register(ctx)  # must not raise despite sink constructor failure

    assert set(ctx.tools) == {"web_search", "web_fetch", "web_similar"}
    assert len(jsonl_attempts) == 1        # attempted exactly once
    assert jsonl_created == []             # constructor raised
    assert len(noop_created) == 1         # exactly one Noop fallback
    the_fallback = noop_created[0]

    asyncio.run(ctx.tools["web_search"]["handler"]({"query": "x"}))
    assert exec_calls[-1]["decision_evidence_sink"] is the_fallback


def test_register_survives_eager_refresh_failure(monkeypatch):
    """eager refresh 抛错时 register 不抛，三工具 + pre_llm_call hook 仍注册。"""
    created, _ = _install_register_fakes(monkeypatch, snapshot=None, refresh_result=_RAISE)
    mod = _load_entry()
    ctx = MockCtx()

    mod.register(ctx)  # must not raise despite refresh failure

    assert set(ctx.tools) == {"web_search", "web_fetch", "web_similar"}
    assert [h["event"] for h in ctx.hooks] == ["pre_llm_call"]
    assert created[0].refresh_calls == 1  # eager attempted exactly once


def test_register_builder_and_handler_share_exact_legacy_registry(monkeypatch):
    """builder 桥接、handler 执行都用 get_registry() 返回的同一个 legacy object。"""
    created, build_calls = _install_register_fakes(
        monkeypatch, snapshot=None, refresh_result=_RAISE)
    exec_calls = _install_exec_spy(monkeypatch)
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)

    assert len(created) == 1
    provider = created[0]

    # builder 闭包桥接的 legacy registry 身份一致。
    provider.builder()
    assert build_calls == [_SENTINEL_REGISTRY]

    # handler 执行传入的 registry 身份一致。
    asyncio.run(ctx.tools["web_search"]["handler"]({"query": "x"}))
    assert exec_calls[-1]["registry"] is _SENTINEL_REGISTRY


def test_bound_handler_cold_passes_none_and_stage_s_true(monkeypatch):
    """冷态：handler 每请求 get() 恰一次，传 decision_context=None + stage_s_enabled=True，
    且不 refresh。"""
    created, _ = _install_register_fakes(monkeypatch, snapshot=None, refresh_result=_RAISE)
    exec_calls = _install_exec_spy(monkeypatch)
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)
    provider = created[0]

    get_before = provider.get_calls
    refresh_before = provider.refresh_calls
    asyncio.run(ctx.tools["web_search"]["handler"]({"query": "x"}))

    assert provider.get_calls - get_before == 1          # exactly one get() per request
    assert provider.refresh_calls == refresh_before       # handler never refreshes
    assert exec_calls[-1]["decision_context"] is None
    assert exec_calls[-1]["stage_s_enabled"] is True


def test_bound_handler_warm_passes_context_and_stage_s_true(monkeypatch):
    """暖态：handler get() 一次拿到快照，传 decision_context=context + stage_s_enabled=True，
    仍不 refresh。"""
    created, _ = _install_register_fakes(
        monkeypatch, snapshot=_SENTINEL_CONTEXT, refresh_result=_SENTINEL_CONTEXT)
    exec_calls = _install_exec_spy(monkeypatch)
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)
    provider = created[0]

    assert provider.refresh_calls == 0  # warm at register: eager get() short-circuits
    get_before = provider.get_calls
    asyncio.run(ctx.tools["web_search"]["handler"]({"query": "x"}))

    assert provider.get_calls - get_before == 1
    assert provider.refresh_calls == 0
    assert exec_calls[-1]["decision_context"] is _SENTINEL_CONTEXT
    assert exec_calls[-1]["stage_s_enabled"] is True


def test_register_hook_returns_none(monkeypatch):
    """pre_llm_call hook 恒返回 None，不注入 LLM context。"""
    _install_register_fakes(monkeypatch, snapshot=_SENTINEL_CONTEXT, refresh_result=_SENTINEL_CONTEXT)
    _install_exec_spy(monkeypatch)
    mod = _load_entry()
    ctx = MockCtx()
    mod.register(ctx)

    hook = ctx.hooks[0]["handler"]
    assert hook() is None


# ── cold activator 合同（fake monotonic / 并发 / 二次 get）────────────────

class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_cold_activator_backoff_deadline_and_cap():
    """fake monotonic：首次立即、deadline 前跳过、到期一次、失败指数退避封顶。"""
    mod = _load_entry()
    clock = _Clock(0.0)

    class P:
        def __init__(self):
            self.refresh_calls = 0
        def get(self):
            return None  # never warms
        def refresh(self):
            self.refresh_calls += 1
            raise RuntimeError("boom")

    p = P()
    act = mod._ColdDecisionContextActivator(p, clock=clock)
    INIT = mod._COLD_ACTIVATE_INITIAL_BACKOFF_SEC
    MULT = mod._COLD_ACTIVATE_BACKOFF_MULTIPLIER
    CAP = mod._COLD_ACTIVATE_MAX_BACKOFF_SEC

    # 首次（deadline None）立即触发，失败 → next = 0 + INIT
    act.activate()
    assert p.refresh_calls == 1
    assert act._next_attempt_at == INIT

    # deadline 前跳过
    clock.t = INIT - 0.001
    act.activate()
    assert p.refresh_calls == 1

    # 逐次到期触发，退避指数增长并封顶
    expected = INIT
    deadline = INIT
    calls = 1
    for _ in range(8):
        clock.t = deadline
        act.activate()
        calls += 1
        expected = min(expected * MULT, CAP)
        deadline = clock.t + expected
        assert p.refresh_calls == calls
        assert act._next_attempt_at == deadline
        assert act._backoff_sec == expected
    assert act._backoff_sec == CAP  # capped


def test_cold_activator_backoff_measured_from_failure_completion():
    """慢失败：退避基准是 refresh *失败完成* 时刻（重读 clock），不是 refresh 开始前的 now。

    回归：若沿用 refresh 前的 now，当 refresh 耗时 > backoff 时 deadline 一返回就过期，
    下一 turn 会立即重试，破坏 bounded retry。
    """
    mod = _load_entry()
    clock = _Clock(0.0)
    INIT = mod._COLD_ACTIVATE_INITIAL_BACKOFF_SEC
    SLOW = 100.0  # refresh 期间流逝的时间，远大于 INIT backoff

    class P:
        def __init__(self):
            self.refresh_calls = 0
        def get(self):
            return None  # never warms
        def refresh(self):
            self.refresh_calls += 1
            clock.t += SLOW  # 模拟慢失败：refresh 内推进 clock
            raise RuntimeError("boom")

    p = P()
    act = mod._ColdDecisionContextActivator(p, clock=clock)

    # 首次触发：refresh 开始 now=0，失败完成时 clock=SLOW → deadline = SLOW + INIT
    act.activate()
    assert p.refresh_calls == 1
    assert act._next_attempt_at == SLOW + INIT

    # deadline 前不重试（修复前 deadline=0+INIT 早已过期，会在此立即重试）
    clock.t = SLOW + INIT - 0.001
    act.activate()
    assert p.refresh_calls == 1

    # 到期后再触发一次
    clock.t = SLOW + INIT
    act.activate()
    assert p.refresh_calls == 2


def test_cold_activator_success_stops_cold_retries():
    """成功 refresh 后 warm fast path 只 get()，不再冷态重试。"""
    mod = _load_entry()
    clock = _Clock(0.0)
    published = object()

    class P:
        def __init__(self):
            self.refresh_calls = 0
            self.snapshot = None
            self.fail = True
        def get(self):
            return self.snapshot
        def refresh(self):
            self.refresh_calls += 1
            if self.fail:
                raise RuntimeError("boom")
            self.snapshot = published
            return self.snapshot

    p = P()
    act = mod._ColdDecisionContextActivator(p, clock=clock)

    act.activate()                       # fail → schedules retry
    assert p.refresh_calls == 1
    p.fail = False
    clock.t = act._next_attempt_at       # due → success publishes snapshot
    act.activate()
    assert p.refresh_calls == 2
    assert p.snapshot is published

    clock.t += 10_000                    # warm: no further refresh regardless of clock
    act.activate()
    act.activate()
    assert p.refresh_calls == 2


def test_cold_activator_concurrent_single_refresh_no_wait():
    """并发 hook：只有一个 refresh，抢不到锁的线程不等待。"""
    mod = _load_entry()
    started = threading.Event()
    release = threading.Event()

    class P:
        def __init__(self):
            self.refresh_calls = 0
            self.snapshot = None
        def get(self):
            return self.snapshot
        def refresh(self):
            self.refresh_calls += 1
            started.set()
            assert release.wait(5)
            self.snapshot = object()

    p = P()
    act = mod._ColdDecisionContextActivator(p)  # real monotonic; next_attempt None

    a = threading.Thread(target=act.pre_llm_call)
    a.start()
    assert started.wait(5)  # A is inside refresh, holding the attempt lock

    b_done = threading.Event()
    def run_b():
        act.pre_llm_call()  # must return immediately, not block behind A
        b_done.set()
    b = threading.Thread(target=run_b)
    b.start()
    assert b_done.wait(2), "second hook thread blocked behind in-flight refresh"
    assert p.refresh_calls == 1  # B did not start a second refresh

    release.set()
    a.join(5)
    b.join(5)
    assert p.refresh_calls == 1


def test_cold_activator_winner_second_get_skips_duplicate_refresh():
    """抢锁 winner 再 get() 一次，若已发布则跳过重复 refresh。"""
    mod = _load_entry()

    class P:
        def __init__(self):
            self.get_calls = 0
            self.refresh_calls = 0
            self._published = object()
        def get(self):
            self.get_calls += 1
            # 顶层 get 返回 None；抢锁后二次 get 已见到发布的快照。
            return None if self.get_calls == 1 else self._published
        def refresh(self):
            self.refresh_calls += 1

    p = P()
    act = mod._ColdDecisionContextActivator(p)
    act.activate()

    assert p.get_calls == 2       # top get + post-lock re-check
    assert p.refresh_calls == 0   # second get saw a snapshot → no duplicate refresh
