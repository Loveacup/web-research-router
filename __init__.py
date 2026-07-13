"""WRR Hermes plugin entrypoint.

plugin.yaml ``entry: __init__.py`` 指向此文件。这是兼容 Hermes plugin loader
的入口模块：顶层保持 import-light（不 import httpx / yaml / wrr.router /
wrr.doctor / wrr.engines.loader），所有重依赖在 ``register(ctx)`` 内部延迟 import。
"""

import logging
import threading
import time

__version__ = "6.1.1"

logger = logging.getLogger("wrr.plugin")

# Bounded exponential backoff for cold DecisionContext activation retries.
# These constants are part of the tested contract — keep them stable.
_COLD_ACTIVATE_INITIAL_BACKOFF_SEC = 1.0
_COLD_ACTIVATE_MAX_BACKOFF_SEC = 30.0
_COLD_ACTIVATE_BACKOFF_MULTIPLIER = 2.0


class _ColdDecisionContextActivator:
    """Best-effort warmer for a CachedDecisionContextProvider.

    Wired as the plugin ``pre_llm_call`` hook and invoked eagerly once at
    register time. It never spawns threads/timers, never holds cross-call
    force-rediscovery state, and never injects LLM context (the hook always
    returns ``None``). The attempt lock is non-blocking: a loser thread that
    cannot take it returns immediately without waiting — but the winning thread
    runs ``refresh()`` synchronously and therefore blocks that one caller for
    the duration of the build. A refresh is attempted at most once per winning
    thread; failures are swallowed with a warning and retried later under
    bounded exponential backoff measured from failure completion.
    """

    def __init__(self, provider, *, clock=time.monotonic):
        self._provider = provider
        self._clock = clock
        # Non-blocking attempt lock: a thread that cannot take it returns
        # immediately rather than waiting behind an in-flight refresh.
        self._attempt_lock = threading.Lock()
        self._backoff_sec = _COLD_ACTIVATE_INITIAL_BACKOFF_SEC
        # None => attempt immediately; otherwise a monotonic deadline.
        self._next_attempt_at = None

    def pre_llm_call(self, *args, **kwargs):
        """Hook entrypoint: warm the cache best-effort, always return ``None``."""
        self._maybe_activate()
        return None

    # Eager register-time activation reuses the exact same path.
    activate = pre_llm_call

    def _maybe_activate(self):
        # Warm fast path: a single get(); once a snapshot is published this is
        # the only work every subsequent call does.
        if self._provider.get() is not None:
            return
        now = self._clock()
        if self._next_attempt_at is not None and now < self._next_attempt_at:
            return  # backoff deadline not reached yet
        # Non-blocking: if another thread already holds the attempt, do not wait.
        if not self._attempt_lock.acquire(blocking=False):
            return
        try:
            # Re-read after winning the lock: a concurrent winner may have just
            # published, in which case skip the duplicate refresh.
            if self._provider.get() is not None:
                return
            try:
                self._provider.refresh()  # winner refreshes at most once
            except Exception as exc:  # noqa: BLE001 - best-effort warmer
                # 退避基准必须是失败*完成*时刻（重读 monotonic），而非 refresh 开始前的
                # ``now``：慢失败（refresh 耗时 > backoff）下用旧 now 会让 deadline 一返回
                # 就已过期，下一 turn 立即重试，破坏 bounded retry。
                self._schedule_retry(self._clock(), exc)
        finally:
            self._attempt_lock.release()

    def _schedule_retry(self, now, exc):
        logger.warning("cold DecisionContext activation failed: %s", exc)
        if self._next_attempt_at is None:
            self._backoff_sec = _COLD_ACTIVATE_INITIAL_BACKOFF_SEC
        else:
            self._backoff_sec = min(
                self._backoff_sec * _COLD_ACTIVATE_BACKOFF_MULTIPLIER,
                _COLD_ACTIVATE_MAX_BACKOFF_SEC,
            )
        self._next_attempt_at = now + self._backoff_sec

_SEARCH_PROVIDERS = [
    "exa", "brave", "community", "academic", "github", "skill", "searxng",
    "local_supermemory", "local_session", "local_qmd", "local_obsidian",
]
_FETCH_PROVIDERS = ["exa", "brave"]
_SIMILAR_PROVIDERS = ["exa"]

# ── OpenAI function schemas（与 wrr/tools/*.py 的 handler 参数对齐）────────
#
# Hermes registry expects tool schemas in OpenAI function format:
# {"name", "description", "parameters": {"type", "properties", "required"}}.
# A bare JSON schema ({"type", "properties", "required"}) is technically
# stored by the registry, but it reaches the model without a `parameters`
# object. In real agent-in-loop E2E this let the model call `web_search` with
# `{}` once before self-correcting. Keep the required fields under
# `parameters` so provider-side schema validation can guide the model before
# the handler runs.
_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": (
        "Search the web through WRR. The `query` field is required; never call "
        "this tool with empty arguments. Prefer omitting `provider` and using "
        "`mode` for WRR routing. If `provider` is set, it must be a concrete "
        "engine name from the enum, never an output/fusion label like "
        "`rrf:grounding`. Returns fused results plus provider, mode, and "
        "fallback_chain metadata when available."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索查询（必填）"},
            "max_results": {"type": "integer", "description": "返回结果上限"},
            "provider": {
                "type": "string",
                "enum": _SEARCH_PROVIDERS,
                "description": (
                    "可选：只在需要强制单引擎时填写具体引擎名。默认应省略，让 WRR 按 mode 路由。"
                    "禁止填写输出里的融合 provider，例如 rrf:grounding / rrf:research。"
                ),
            },
            "mode": {"type": "string", "description": "显式 mode 覆盖自动分类（可选）"},
        },
        "required": ["query"],
    },
}

_FETCH_SCHEMA = {
    "name": "web_fetch",
    "description": "Fetch/extract a page through WRR. The `url` field is required.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抽取的页面 URL（必填）"},
            "max_characters": {"type": "integer", "description": "抽取正文字符上限"},
            "provider": {
                "type": "string",
                "enum": _FETCH_PROVIDERS,
                "description": "可选：强制单引擎抽取。只能是具体引擎名，不能是 rrf:* 融合标签。",
            },
        },
        "required": ["url"],
    },
}

_SIMILAR_SCHEMA = {
    "name": "web_similar",
    "description": "Find pages similar to a reference URL through WRR. The `url` field is required.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "参考 URL（必填）"},
            "max_results": {"type": "integer", "description": "返回相似页面上限"},
            "provider": {
                "type": "string",
                "enum": _SIMILAR_PROVIDERS,
                "description": "可选：强制单引擎 similar。只能是具体引擎名，不能是 rrf:* 融合标签。",
            },
        },
        "required": ["url"],
    },
}


def register(ctx) -> None:
    """Hermes plugin loader 入口：注册 wrr toolset 的 3 个异步工具。

    重依赖（handler 链路会拉起 wrr.router / httpx 等）在此处延迟 import，
    确保模块顶层 import 轻量。
    """
    # Hermes loader 用 spec_from_file_location(submodule_search_locations=[plugin_dir])
    # 加载本 root entry，但不会把 plugin_dir 注入 sys.path；下面的 `from wrr...`
    # 是绝对 import，从非 repo cwd（如 /tmp）加载时会 ModuleNotFoundError。
    # 在延迟 import 前确保 plugin 根目录在 sys.path 中。
    import sys
    from pathlib import Path

    plugin_dir = str(Path(__file__).resolve().parent)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)

    from wrr.tools.web_search import execute_web_search
    from wrr.tools.web_fetch import handle_web_fetch
    from wrr.tools.web_similar import handle_web_similar
    from wrr.registry import get_registry
    from wrr.runtime.decision_context_assembly import (
        build_control_plane_decision_context,
    )
    from wrr.runtime.decision_context_provider import CachedDecisionContextProvider
    from wrr.runtime.decision_evidence import (
        JsonlDecisionEvidenceSink,
        NoopDecisionEvidenceSink,
    )

    # 唯一 execution legacy registry：冷态与暖态都用同一个对象执行，get_registry()
    # 恰好调用一次。
    legacy_registry = get_registry()

    def _build_control_plane_context():
        # builder 闭包把同一个 legacy object 桥接进 assembly（内部不做第二次 discovery）。
        return build_control_plane_decision_context(legacy_registry)

    provider = CachedDecisionContextProvider(_build_control_plane_context)
    activator = _ColdDecisionContextActivator(provider)

    # 组合层拥有唯一 sink：每次 register 恰构造一个 Jsonl sink（含路径检测），所有
    # 请求复用同一对象，绝不 per-request 构造/探路。构造失败（如 runtime detect /
    # 路径解析抛错）不得让注册崩溃：吞掉并降级为恰一个 Noop fallback。warning 只带
    # 构造异常本身，不含 query / context 等其它信息。
    try:
        decision_evidence_sink = JsonlDecisionEvidenceSink()
    except Exception as exc:  # noqa: BLE001 - 组合层降级，注册必须存活
        logger.warning("decision evidence sink construction failed: %s", exc)
        decision_evidence_sink = NoopDecisionEvidenceSink()

    async def _bound_web_search(args, **kwargs):
        # 每请求只 get() 一次，直接读快照（冷态 None / 暖态 context）；不 refresh /
        # discovery / report / bridge。Stage S 恒开，强制 caller legacy registry 执行。
        # 复用组合层拥有的同一个 sink 对象（显式注入，不 per-request 构造）。
        decision_context = provider.get()
        return await execute_web_search(
            args,
            registry=legacy_registry,
            decision_context=decision_context,
            stage_s_enabled=True,
            decision_evidence_sink=decision_evidence_sink,
        )

    # web_search 与内建工具（toolset="web"）同名，Hermes registry 会拒绝跨
    # toolset 重名注册，必须显式 override=True 才能让插件版接管。
    ctx.register_tool(
        name="web_search",
        handler=_bound_web_search,
        schema=_SEARCH_SCHEMA,
        toolset="wrr",
        is_async=True,
        override=True,
    )
    ctx.register_tool(
        name="web_fetch",
        handler=handle_web_fetch,
        schema=_FETCH_SCHEMA,
        toolset="wrr",
        is_async=True,
    )
    ctx.register_tool(
        name="web_similar",
        handler=handle_web_similar,
        schema=_SIMILAR_SCHEMA,
        toolset="wrr",
        is_async=True,
    )
    ctx.register_hook("pre_llm_call", activator.pre_llm_call)

    # Eager best-effort 暖机：任何 assembly / refresh failure 都在此本地吞掉，
    # 不得逃出 register（activator 内部已捕获 refresh 异常，这里再兜一层防御）。
    try:
        activator.activate()
    except Exception:  # noqa: BLE001 - register 不因暖机失败而崩
        logger.warning("eager DecisionContext activation raised; continuing", exc_info=True)
