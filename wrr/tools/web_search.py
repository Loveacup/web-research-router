"""web_search 工具 handler：v5 mode 路由（classify_intent → 并行引擎 → RRF 融合）。

显式 provider 仍走单引擎（v4 兼容）；显式 mode 覆盖自动分类。
"""
from .. import config
from ..registry import get_registry
from ..router import route_search_v5
from ..schemas import SearchOptions
from ..errors import AllEnginesFailedError
from ..formatters import format_search, format_error


async def execute_web_search(
    args,
    *,
    registry,
    decision_context=None,
    decision_context_observation=None,
    decision_evidence_version=1,
    stage_s_enabled=None,
    decision_evidence_sink=None,
    request_key=None,
) -> str:
    """显式依赖执行 seam：调用方注入 registry / Stage S 依赖，复用解析·format·error 逻辑。

    ``registry`` 为要执行的引擎注册表（显式，无默认）；``decision_context`` /
    ``stage_s_enabled`` / ``decision_evidence_sink`` 直接透传给 ``route_search_v5``，
    保持其三态归一化合同与 sink 所有权语义。root wiring 用此 seam 注入 Stage S 与
    组合层拥有的 sink 对象；``handle_web_search`` 保持旧行为（不注入 sink）。
    """
    query = args.get("query", "")
    if not query:
        return format_error("web_search", "", ValueError("'query' is required"))
    count = min(
        int(args.get("max_results", config.DEFAULT_SEARCH_COUNT) or config.DEFAULT_SEARCH_COUNT),
        config.MAX_SEARCH_COUNT,
    )
    provider = args.get("provider")
    options = SearchOptions(query=query, count=count, provider=provider,
                            mode=args.get("mode"))
    # Preserve the legacy router call shape for callers not opting in.
    evidence_kwargs = {}
    if decision_context_observation is not None:
        evidence_kwargs["decision_context_observation"] = decision_context_observation
    if decision_evidence_version != 1:
        evidence_kwargs["decision_evidence_version"] = decision_evidence_version
    if request_key is not None:
        evidence_kwargs["request_key"] = request_key
    try:
        result = await route_search_v5(
            options,
            registry,
            **evidence_kwargs,
            decision_context=decision_context,
            stage_s_enabled=stage_s_enabled,
            decision_evidence_sink=decision_evidence_sink,
        )
        return format_search(result, query)
    except AllEnginesFailedError as e:
        return format_error("web_search", query, e)


async def handle_web_search(args, **kwargs) -> str:
    """兼容入口：默认 ``get_registry()`` + legacy 模式（不注入 Stage S 依赖）。

    ``**kwargs`` 仅为向后兼容签名保留，不作为依赖注入通道。
    """
    return await execute_web_search(args, registry=get_registry())
