"""P1-7: recovery runtime gate 测试。"""
from conftest import FakeEngine, mk_results
from wrr import config
from wrr.errors import AllEnginesFailedError
from wrr.registry import EngineRegistry
from wrr.router import route_search_v5
from wrr.schemas import SearchOptions


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_recovery_allowed_defaults():
    """默认允许 hermes / claude_code / codex / omp；不允许 standalone / unknown。"""
    assert config.recovery_allowed("hermes") is True
    assert config.recovery_allowed("claude_code") is True
    assert config.recovery_allowed("codex") is True
    assert config.recovery_allowed("omp") is True
    assert config.recovery_allowed("standalone") is False
    assert config.recovery_allowed("unknown") is False


def test_recovery_allowed_env_override(monkeypatch):
    """WRR_RECOVERY_ALLOWED_RUNTIMES 覆盖默认集合。"""
    monkeypatch.setenv("WRR_RECOVERY_ALLOWED_RUNTIMES", "standalone,hermes")
    assert config.recovery_allowed("standalone") is True
    assert config.recovery_allowed("hermes") is True
    assert config.recovery_allowed("codex") is False


def _mock_mode_engines(mode, query=""):
    """让主 mode 与 recovery 使用不同引擎，便于隔离测试。"""
    if mode == "recovery":
        return ["brave"]
    return ["exa"]


def test_recovery_runs_when_allowed(monkeypatch):
    """允许的 runtime 下主 mode 失败会触发 recovery 兜底。"""
    monkeypatch.setattr(config, "recovery_allowed", lambda runtime_name=None: True)
    monkeypatch.setattr(config, "mode_engines", _mock_mode_engines)
    reg = EngineRegistry()
    reg.register(FakeEngine("exa", search_results=[]))
    reg.register(FakeEngine("brave", search_results=mk_results(1)))
    rr = run(route_search_v5(SearchOptions("test"), reg))
    assert rr.mode == "recovery"
    assert rr.diagnostics.mode_reason == "recovery_fallback"


def test_recovery_blocked_when_disallowed(monkeypatch):
    """不允许的 runtime 下主 mode 失败直接抛 AllEnginesFailedError。"""
    monkeypatch.setattr(config, "recovery_allowed", lambda runtime_name=None: False)
    monkeypatch.setattr(config, "mode_engines", _mock_mode_engines)
    reg = EngineRegistry()
    reg.register(FakeEngine("exa", search_results=[]))
    reg.register(FakeEngine("brave", search_results=mk_results(1)))
    try:
        run(route_search_v5(SearchOptions("test"), reg))
        assert False, "should raise AllEnginesFailedError"
    except AllEnginesFailedError as e:
        assert "recovery is blocked" in str(e).lower()
