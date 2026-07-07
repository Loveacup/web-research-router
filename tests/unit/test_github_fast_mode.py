"""P1-3: github_fast_mode() 函数化测试。"""
import asyncio
import os
from unittest.mock import patch

import pytest

from conftest import FakeAsyncClient, FakeResponse
from wrr import config
from wrr.engines.github import GitHubEngine


def run(coro):
    return asyncio.run(coro)


class TestGitHubFastModeFunction:
    """测试 config.github_fast_mode() 函数行为。"""

    def test_default_no_env_returns_false(self):
        """默认无环境变量返回 False。"""
        def mock_resolver(key: str):
            return None
        assert config.github_fast_mode(env_resolver=mock_resolver) is False

    def test_github_fast_mode_env_true(self):
        """GITHUB_FAST_MODE=1 返回 True。"""
        def mock_resolver(key: str):
            return "1" if key == "GITHUB_FAST_MODE" else None
        assert config.github_fast_mode(env_resolver=mock_resolver) is True

    def test_wrr_github_fast_mode_env_true(self):
        """WRR_GITHUB_FAST_MODE=1 返回 True。"""
        def mock_resolver(key: str):
            return "1" if key == "WRR_GITHUB_FAST_MODE" else None
        assert config.github_fast_mode(env_resolver=mock_resolver) is True

    def test_both_env_vars_priority(self):
        """两个环境变量同时设置，任一为 True 即返回 True。"""
        def mock_resolver(key: str):
            if key == "GITHUB_FAST_MODE":
                return "0"
            if key == "WRR_GITHUB_FAST_MODE":
                return "1"
            return None
        assert config.github_fast_mode(env_resolver=mock_resolver) is True

    def test_explicit_env_resolver_overrides_os_environ(self, monkeypatch):
        """显式传入 env_resolver 覆盖 os.environ。"""
        monkeypatch.setenv("GITHUB_FAST_MODE", "1")
        def mock_resolver(key: str):
            return None  # 忽略实际环境变量
        assert config.github_fast_mode(env_resolver=mock_resolver) is False

    def test_defaults_to_os_environ_when_no_resolver(self, monkeypatch):
        """未传入 env_resolver 时使用 os.environ。"""
        monkeypatch.setenv("WRR_GITHUB_FAST_MODE", "1")
        assert config.github_fast_mode() is True

    def test_false_string_values(self):
        """测试 false 值字符串（0, false, no, off）。"""
        for false_val in ["0", "false", "False", "no", "off", "OFF"]:
            def mock_resolver(key: str):
                return false_val if key == "GITHUB_FAST_MODE" else None
            assert config.github_fast_mode(env_resolver=mock_resolver) is False


class TestGitHubEngineIntegration:
    """测试 GitHubEngine._fetch_activity 对 github_fast_mode() 的响应。"""

    def test_fast_mode_skips_activity_lookup(self, monkeypatch):
        """fast_mode=True 时跳过 activity lookup，返回全 None 列表。"""
        monkeypatch.setattr(config, "github_fast_mode", lambda: True)
        monkeypatch.setattr(config, "GITHUB_ACTIVITY_LOOKUP", True)

        engine = GitHubEngine()
        FakeAsyncClient.captured = []
        client = FakeAsyncClient()
        headers = {}
        items = [{"full_name": "test/repo1"}, {"full_name": "test/repo2"}]

        result = run(engine._fetch_activity(client, headers, items))

        assert result == [None, None]
        # 验证未发起任何 GET 请求
        get_calls = [c for c in FakeAsyncClient.captured if c.get("method") == "GET"]
        assert len(get_calls) == 0

    def test_fast_mode_false_executes_activity_lookup(self, monkeypatch):
        """fast_mode=False 且 GITHUB_ACTIVITY_LOOKUP=True 时执行 activity lookup。"""
        monkeypatch.setattr(config, "github_fast_mode", lambda: False)
        monkeypatch.setattr(config, "GITHUB_ACTIVITY_LOOKUP", True)
        monkeypatch.setattr(config, "GITHUB_ACTIVITY_LOOKUP_TIMEOUT", 3.0)
        monkeypatch.setattr(config, "GITHUB_ACTIVITY_CONCURRENCY", 5)

        engine = GitHubEngine()
        FakeAsyncClient.captured = []
        FakeAsyncClient.response_data = [{"sha": "abc123"}]
        client = FakeAsyncClient()
        headers = {}
        items = [{"full_name": "test/repo1"}]

        result = run(engine._fetch_activity(client, headers, items))

        assert len(result) == 1
        assert result[0] == 1  # 1 个 commit
        # 验证发起了 GET 请求
        get_calls = [c for c in FakeAsyncClient.captured if c.get("method") == "GET"]
        assert len(get_calls) == 1

    def test_monkeypatch_env_resolver_in_engine_context(self, monkeypatch):
        """通过 monkeypatch config.github_fast_mode 动态改变行为。"""
        # 初始状态：fast_mode=False
        monkeypatch.setattr(config, "github_fast_mode", lambda: False)
        monkeypatch.setattr(config, "GITHUB_ACTIVITY_LOOKUP", True)
        monkeypatch.setattr(config, "GITHUB_ACTIVITY_LOOKUP_TIMEOUT", 3.0)
        monkeypatch.setattr(config, "GITHUB_ACTIVITY_CONCURRENCY", 5)

        engine = GitHubEngine()
        FakeAsyncClient.captured = []
        FakeAsyncClient.response_data = []
        client = FakeAsyncClient()
        items = [{"full_name": "test/repo"}]

        # 第一次调用：fast_mode=False，执行 lookup
        result1 = run(engine._fetch_activity(client, {}, items))
        get_calls_1 = [c for c in FakeAsyncClient.captured if c.get("method") == "GET"]
        assert len(get_calls_1) == 1

        # 动态切换 fast_mode=True
        monkeypatch.setattr(config, "github_fast_mode", lambda: True)

        # 第二次调用：fast_mode=True，跳过 lookup
        FakeAsyncClient.captured = []  # 重置捕获
        result2 = run(engine._fetch_activity(client, {}, items))
        assert result2 == [None]
        get_calls_2 = [c for c in FakeAsyncClient.captured if c.get("method") == "GET"]
        assert len(get_calls_2) == 0  # 未增加调用
