"""WRR Hermes plugin entrypoint.

plugin.yaml ``entry: __init__.py`` 指向此文件。这是兼容 Hermes plugin loader
的入口模块：顶层保持 import-light（不 import httpx / yaml / wrr.router /
wrr.doctor / wrr.engines.loader），所有重依赖在 ``register(ctx)`` 内部延迟 import。
"""

__version__ = "6.0.0"

# ── 最小 JSON schema（与 wrr/tools/*.py 的 handler 参数对齐）──────────────
_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索查询"},
        "max_results": {"type": "integer", "description": "返回结果上限"},
        "provider": {"type": "string", "description": "显式指定引擎（可选）"},
        "mode": {"type": "string", "description": "显式 mode 覆盖自动分类（可选）"},
    },
    "required": ["query"],
}

_FETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "要抽取的页面 URL"},
        "max_characters": {"type": "integer", "description": "抽取正文字符上限"},
        "provider": {"type": "string", "description": "显式指定引擎（可选）"},
    },
    "required": ["url"],
}

_SIMILAR_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "参考 URL"},
        "max_results": {"type": "integer", "description": "返回相似页面上限"},
        "provider": {"type": "string", "description": "显式指定引擎（可选）"},
    },
    "required": ["url"],
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

    from wrr.tools.web_search import handle_web_search
    from wrr.tools.web_fetch import handle_web_fetch
    from wrr.tools.web_similar import handle_web_similar

    # web_search 与内建工具（toolset="web"）同名，Hermes registry 会拒绝跨
    # toolset 重名注册，必须显式 override=True 才能让插件版接管。
    ctx.register_tool(
        name="web_search",
        handler=handle_web_search,
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
