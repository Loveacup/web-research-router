"""Hermes backend registration; does not replace any built-in tool."""


def register(ctx):
    from .provider import WrrWebSearchProvider

    ctx.register_web_search_provider(WrrWebSearchProvider())
