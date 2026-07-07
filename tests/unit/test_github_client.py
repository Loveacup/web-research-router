"""GitHubClient 单测：超时配置优先级和传递验证。"""
import asyncio
import os

from wrr.engines._github_client import GitHubClient
from wrr import config


def run(coro):
    return asyncio.run(coro)


def test_github_client_default_timeout_uses_config(monkeypatch):
    import importlib
    monkeypatch.setenv("WRR_GITHUB_CLIENT_TIMEOUT", "6.5")
    importlib.reload(config)
    from wrr.engines import _github_client
    importlib.reload(_github_client)
    client = _github_client.GitHubClient(token="t")
    assert client.timeout == 6.5
    importlib.reload(config)
    importlib.reload(_github_client)


def test_github_client_explicit_timeout_still_wins(monkeypatch):
    import importlib
    monkeypatch.setenv("WRR_GITHUB_CLIENT_TIMEOUT", "6.5")
    importlib.reload(config)
    from wrr.engines import _github_client
    importlib.reload(_github_client)
    client = _github_client.GitHubClient(token="t", timeout=2.0)
    assert client.timeout == 2.0
    importlib.reload(config)
    importlib.reload(_github_client)


def test_code_search_uses_client_timeout(monkeypatch):
    import importlib
    from wrr.engines import _github_client

    captured_timeout = []

    class FakeAsyncClient:
        def __init__(self, timeout=None, **kwargs):
            captured_timeout.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, params=None, headers=None, **kwargs):
            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"items": [], "total_count": 0, "incomplete_results": False}
            return R()

    monkeypatch.setattr(_github_client.httpx, "AsyncClient", FakeAsyncClient)
    os.environ["GITHUB_TOKEN"] = "tkn"
    client = _github_client.GitHubClient(token="t", timeout=4.0)
    run(client.code_search("x"))
    assert captured_timeout == [4.0]
