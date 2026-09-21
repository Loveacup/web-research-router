"""WRR Hermes plugin entrypoint.

plugin.yaml ``entry: __init__.py`` 指向此文件。这是兼容 Hermes plugin loader
的入口模块：顶层保持 import-light（不 import httpx / yaml / wrr.router /
wrr.doctor / wrr.engines.loader），所有重依赖在 ``register(ctx)`` 内部延迟 import。
"""

import json
import logging
import threading
import time
import uuid
from contextlib import nullcontext
from typing import ContextManager, cast

__version__ = "6.1.2"

logger = logging.getLogger("wrr.plugin")

# Bounded exponential backoff for cold/expired DecisionContext refresh retries.
# These constants are part of the tested contract — keep them stable.
_COLD_ACTIVATE_INITIAL_BACKOFF_SEC = 1.0
_COLD_ACTIVATE_MAX_BACKOFF_SEC = 30.0
_COLD_ACTIVATE_BACKOFF_MULTIPLIER = 2.0


class _ColdDecisionContextActivator:
    """Best-effort cold/expired refresher for a CachedDecisionContextProvider.

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

    def __init__(self, provider, *, clock=time.monotonic, wall_clock=time.time):
        self._provider = provider
        self._clock = clock
        self._wall_clock = wall_clock
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
        # Fresh snapshots are reusable; expired snapshots need a control-plane
        # refresh. The search handler itself never invokes this path.
        snapshot = self._provider.get()
        if snapshot is not None and self._wall_clock() < snapshot.expires_at:
            return
        now = self._clock()
        deadline = self._next_attempt_at
        if deadline is not None and now < deadline:
            return  # backoff deadline not reached yet
        # Non-blocking: if another thread already holds the attempt, do not wait.
        if not self._attempt_lock.acquire(blocking=False):
            return
        try:
            # Another caller may have failed between our optimistic deadline
            # check and acquisition. The latest deadline is authoritative here.
            if self._next_attempt_at is not None and self._clock() < self._next_attempt_at:
                return
            # Re-read after winning the lock: a concurrent winner may have just
            # published, in which case skip the duplicate refresh.
            snapshot = self._provider.get()
            if snapshot is not None and self._wall_clock() < snapshot.expires_at:
                return
            try:
                self._provider.refresh()  # winner refreshes at most once
                self._next_attempt_at = None
                self._backoff_sec = _COLD_ACTIVATE_INITIAL_BACKOFF_SEC
            except Exception as exc:  # noqa: BLE001 - best-effort warmer
                # 退避基准必须是失败*完成*时刻（重读 monotonic），而非 refresh 开始前的
                # ``now``：慢失败（refresh 耗时 > backoff）下用旧 now 会让 deadline 一返回
                # 就已过期，下一 turn 立即重试，破坏 bounded retry。
                self._schedule_retry(self._clock(), exc)
        finally:
            self._attempt_lock.release()

    def _schedule_retry(self, now, exc):
        logger.warning("DecisionContext activation failed: %s", exc)
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
            "campaign_id": {
                "type": "string",
                "description": (
                    "可选：显式 campaign 标签。仅当本插件启用了固定 evidence campaign 且"
                    "该值与声明 campaign 完全匹配时才计入 admission；否则按普通搜索处理。"
                ),
            },
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


class _CampaignEvidenceSink:
    """Request-local sink that joins one admission to its v2 evidence."""

    def __init__(self, ledger, admission, downstream):
        self._ledger = ledger
        self._admission = admission
        self._downstream = downstream
        self._finished = False

    def record(self, evidence):
        finish_error = None
        downstream_error = None
        guard_factory = getattr(self._ledger, "terminal_guard", None)
        guard = cast(
            ContextManager[None],
            guard_factory() if callable(guard_factory) else nullcontext(),
        )
        with guard:
            try:
                self._ledger.finish(self._admission.token, evidence)
            except Exception as exc:  # noqa: BLE001 - user search must remain fail-open
                finish_error = exc
                try:
                    self._ledger.record_fault("evidence_finish_failed")
                except Exception:  # noqa: BLE001 - best-effort durable poison
                    pass
            else:
                self._finished = True

            try:
                persisted = self._downstream.record(evidence)
                if persisted is False:
                    downstream_error = RuntimeError(
                        "downstream evidence was not persisted"
                    )
            except Exception as exc:  # noqa: BLE001 - preserve user-search fail-open
                downstream_error = exc
            if downstream_error is not None:
                try:
                    self._ledger.record_fault("downstream_evidence_failed")
                except Exception:  # noqa: BLE001 - best-effort durable poison
                    pass

        if finish_error is not None:
            raise finish_error
        if downstream_error is not None:
            raise downstream_error

    def drop_pending(self):
        if self._finished:
            return
        try:
            self._ledger.drop(self._admission.token)
        except Exception:  # noqa: BLE001 - campaign faults never break user search
            try:
                self._ledger.record_fault("admission_drop_failed")
            except Exception:  # noqa: BLE001 - best-effort durable poison
                pass


def register(ctx, *, campaign_ledger=None) -> None:
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
    from wrr.runtime.campaign_activation import (
        CampaignController,
        parse_campaign_config,
    )
    from wrr.runtime.campaign_sampler import (
        parse_campaign_sampler_config,
        start_campaign_sampler,
    )
    from wrr.runtime.campaign_ledger import (
        CampaignDeclaration,
        CampaignLedger,
        CampaignMismatch,
    )

    # 唯一 execution legacy registry：冷态与暖态都用同一个对象执行，get_registry()
    # 恰好调用一次。
    legacy_registry = get_registry()

    # 固定 evidence campaign 是显式 opt-in（默认关闭）。配置读取/校验失败只关闭
    # campaign，绝不阻断三个工具的注册。
    campaign_config = None
    get_config = getattr(ctx, "get_config", None)
    if callable(get_config):
        try:
            campaign_config = parse_campaign_config(get_config("campaign"))
        except ValueError as exc:
            logger.warning("campaign config rejected; campaign disabled: %s", exc)
            campaign_config = None
    sampler_config = None
    if campaign_config is not None and callable(get_config):
        try:
            sampler_config = parse_campaign_sampler_config(get_config("campaign_sampler"))
        except ValueError as exc:
            logger.warning("campaign sampler config rejected; sampler disabled: %s", exc)

    def _build_control_plane_context():
        # builder 闭包把同一个 legacy object 桥接进 assembly（内部不做第二次 discovery）。
        # 启用 campaign 时绑定一个长 TTL 的固定 snapshot，保证 24h 窗口内 cohort 不变。
        if campaign_config is None:
            return build_control_plane_decision_context(legacy_registry)
        return build_control_plane_decision_context(
            legacy_registry, ttl_sec=campaign_config.context_ttl_sec,
        )

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

    # 生产 campaign controller 在 register 末尾（eager 暖机之后）构建；此处先声明
    # 以便 handler 闭包绑定该 cell（闭包按调用时解析，注册完成前 controller 已是终值）。
    controller = None

    async def _bound_web_search(args, **kwargs):
        # 每请求只 observe() 一次，原子拿 (context, status, cohort_id)；不 refresh /
        # discovery / report / bridge。Stage S 恒开，强制 caller legacy registry 执行。
        # 复用组合层拥有的同一个 sink 对象（显式注入，不 per-request 构造）。
        observation = provider.observe()
        stage_s_for_request = True
        try:
            request_key = str(uuid.uuid4())
        except Exception:  # noqa: BLE001 - identity failure must not break search
            request_key = None
            if campaign_ledger is not None:
                try:
                    campaign_ledger.record_fault("identity_mint_failed")
                except Exception:  # noqa: BLE001 - best-effort durable poison
                    pass
            if controller is not None:
                try:
                    controller.record_fault("identity_mint_failed")
                except Exception:  # noqa: BLE001 - best-effort durable poison
                    pass
        request_sink = decision_evidence_sink
        campaign_sink = None
        admitted_controller = None
        if controller is not None and request_key is not None:
            # 生产路径：只有显式 campaign 标签 + mode + 无 provider 的请求才准入；
            # 不匹配时返回 None，该请求仍按普通搜索执行，不消耗额度。
            admitted = controller.admit(
                request_key,
                campaign_id=args.get("campaign_id") if isinstance(args, dict) else None,
                mode=args.get("mode") if isinstance(args, dict) else None,
                provider=args.get("provider") if isinstance(args, dict) else None,
                context_status=observation.status,
                context_cohort_id=observation.cohort_id,
                context_expires_at=(
                    observation.context.expires_at
                    if observation.context is not None
                    else None
                ),
                context_now=time.time(),
            )
            if admitted is not None:
                admitted_controller = controller
                campaign_sink = _CampaignEvidenceSink(
                    controller, admitted, decision_evidence_sink,
                )
                request_sink = campaign_sink
        elif campaign_ledger is not None and request_key is not None:
            atomic_begin = getattr(campaign_ledger, "begin_or_fault", None)
            try:
                begin = atomic_begin or campaign_ledger.begin
                admission = begin(request_key)
            except Exception:  # noqa: BLE001 - campaign is fail-open for user search
                admission = None
                if atomic_begin is None:
                    try:
                        campaign_ledger.record_fault("admission_failed")
                    except Exception:  # noqa: BLE001 - best-effort durable poison
                        pass
            if admission is not None:
                campaign_sink = _CampaignEvidenceSink(
                    campaign_ledger, admission, decision_evidence_sink,
                )
                request_sink = campaign_sink

        try:
            return await execute_web_search(
                args,
                registry=legacy_registry,
                decision_context_observation=(
                    observation if stage_s_for_request else None
                ),
                decision_evidence_version=2,
                stage_s_enabled=stage_s_for_request,
                decision_evidence_sink=request_sink,
                request_key=request_key,
            )
        finally:
            if campaign_sink is not None:
                campaign_sink.drop_pending()
            if admitted_controller is not None:
                # 每个准入请求终态完成后递减 in-flight；达到容量与 24h 双门槛后
                # 才允许 clean close。
                admitted_controller.on_terminal()

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
    start_sampler_once = lambda: None

    def _pre_llm_call(**_kwargs):
        activator.pre_llm_call()
        if controller is not None:
            observation = provider.observe()
            controller.tick(
                context_status=observation.status,
                context_cohort_id=observation.cohort_id,
            )
        start_sampler_once()
        return None

    ctx.register_hook("pre_llm_call", _pre_llm_call)

    # Eager best-effort 暖机发生在 hook 注册之后，保留既有冷态注册语义。
    # activation failure 不得阻断工具注册。
    try:
        activator.activate()
    except Exception:  # noqa: BLE001 - register 不因暖机失败而崩
        logger.warning("eager DecisionContext activation raised; continuing", exc_info=True)

    # 仅在显式配置且暖机成功时创建生产 controller。绑定本次实际 cohort；
    # ledger/open/config 任一失败都只关闭 campaign，不影响普通搜索。
    if campaign_config is not None:
        try:
            observation = provider.observe()
            if observation.status == "available" and observation.cohort_id is not None:
                declaration = CampaignDeclaration(
                    policy_version=campaign_config.policy_version,
                    requested_modes=(campaign_config.mode,),
                    accepted_context_cohort_id=observation.cohort_id,
                    build_manifest_id=campaign_config.build_manifest_id,
                )
                ledger = CampaignLedger.open(
                    campaign_config.ledger_path,
                    campaign_id=campaign_config.campaign_id,
                    capacity=campaign_config.capacity,
                    declaration=declaration,
                )
                controller = CampaignController(
                    campaign_config, ledger, observation.cohort_id,
                )
            else:
                logger.warning("campaign activation skipped: decision context not available")
        except CampaignMismatch as exc:
            logger.warning(
                "%s",
                json.dumps(
                    {
                        "event": "campaign_activation_mismatch",
                        "reason": exc.reason,
                        **exc.diagnostic,
                    },
                    sort_keys=True,
                ),
            )
            controller = None
        except Exception as exc:  # noqa: BLE001 - campaign is opt-in and fail-safe
            logger.warning("campaign activation failed; campaign disabled: %s", exc)
            controller = None

    # 可选 D7-S5 sampler 延迟到首次 pre_llm_call 才启动，避免 register 阶段产生
    # 搜索/ledger 副作用。启动受锁保护且只尝试一次；unload 先停止 writer，再把
    # 未完整 campaign dirty-close 并释放 SQLite handle。
    if controller is not None and campaign_config is not None:
        on_unload = getattr(ctx, "on_unload", None)
        if callable(on_unload):
            sampler_lock = threading.Lock()
            sampler_stop = None
            sampler_start_attempted = False
            sampler_unloaded = False

            def start_sampler_once():
                nonlocal sampler_stop, sampler_start_attempted
                if sampler_config is None or campaign_config.capacity != 50:
                    return
                with sampler_lock:
                    if (
                        sampler_unloaded
                        or sampler_start_attempted
                        or controller.state != "active"
                    ):
                        return
                    sampler_start_attempted = True
                    try:
                        sampler_stop = start_campaign_sampler(
                            _bound_web_search,
                            controller,
                            campaign_id=campaign_config.campaign_id,
                            config=sampler_config,
                        )
                    except Exception as exc:  # noqa: BLE001 - hook must stay fail-open
                        try:
                            controller.record_fault("sampler_start_failed")
                        except Exception:  # noqa: BLE001 - best-effort durable poison
                            pass
                        logger.warning(
                            "campaign sampler start failed; sampler disabled: %s", exc,
                        )

            def stop_sampler_and_shutdown():
                nonlocal sampler_unloaded
                with sampler_lock:
                    sampler_unloaded = True
                    stop = sampler_stop
                try:
                    if stop is not None:
                        stop()
                finally:
                    controller.shutdown()

            on_unload(stop_sampler_and_shutdown)
