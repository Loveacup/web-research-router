"""Exa mode 自动路由单测：classify_query / get_search_mode / get_timeout_for_mode。

验证文档记录的「Exa 四模式自动路由」特性确实按设计工作。
"""
import importlib
from wrr.engines import exa as exa_mod
from wrr.schemas import SearchOptions
from wrr import config


def test_classify_academic():
    assert exa_mod.classify_query("transformer architecture 综述") == "academic"


def test_classify_research():
    assert exa_mod.classify_query("详细比较 A 和 B 的优劣") == "research"


def test_classify_factual():
    assert exa_mod.classify_query("Claude 4.8 release 日期是哪天") == "factual"


def test_classify_standard_default():
    assert exa_mod.classify_query("buy a cheap laptop") == "standard"


def test_get_search_mode_auto_routing():
    assert exa_mod.get_search_mode(SearchOptions("survey of X")) == config.EXA_MODE_ROUTING["academic"]
    assert exa_mod.get_search_mode(SearchOptions("买电脑")) == "auto"   # standard → auto


def test_get_search_mode_explicit_override():
    # 显式 Exa search type 优先于 route/legacy mode 与自动路由
    assert exa_mod.get_search_mode(SearchOptions("survey", mode="fast")) == "fast"
    assert exa_mod.get_search_mode(
        SearchOptions("survey", mode="grounding", route_mode="research", exa_mode="fast")
    ) == "fast"


def test_get_search_mode_maps_wrr_router_modes_to_exa_types():
    # WRR router modes are internal; leaking them into Exa's API `type` field
    # causes HTTP 400. Agent-in-loop E2E hit this with mode="grounding".
    assert exa_mod.get_search_mode(SearchOptions("go release notes", mode="grounding")) == "auto"
    assert exa_mod.get_search_mode(SearchOptions("deep comparison", mode="research")) == "deep-lite"
    assert exa_mod.get_search_mode(SearchOptions("survey", mode="academic")) == "deep"


def test_get_search_mode_unknown_explicit_mode_falls_back_to_auto():
    assert exa_mod.get_search_mode(SearchOptions("anything", mode="not-an-exa-type")) == "auto"


def test_get_timeout_for_mode():
    assert exa_mod.get_timeout_for_mode("deep") == config.EXA_MODE_TIMEOUT["deep"]
    assert exa_mod.get_timeout_for_mode("unknown-mode") == config.EXA_MODE_TIMEOUT["auto"]   # 兜底默认


def test_exa_global_timeout_can_be_overridden_from_env(monkeypatch):
    monkeypatch.setenv("WRR_EXA_TIMEOUT", "42")
    importlib.reload(config)
    import wrr.engines.exa as exa_mod_reloaded
    importlib.reload(exa_mod_reloaded)

    assert config.EXA_TIMEOUT == 42.0
    assert config.ENGINE_TIMEOUT["exa"] == 42.0
    assert exa_mod_reloaded.ExaEngine().timeout == 42.0

    # 清理：恢复默认状态
    monkeypatch.delenv("WRR_EXA_TIMEOUT", raising=False)
    importlib.reload(config)


def test_exa_mode_timeouts_can_be_overridden_from_env(monkeypatch):
    monkeypatch.setenv("WRR_EXA_MODE_FAST_TIMEOUT", "4")
    monkeypatch.setenv("WRR_EXA_MODE_AUTO_TIMEOUT", "6")
    monkeypatch.setenv("WRR_EXA_MODE_DEEPLITE_TIMEOUT", "9")
    monkeypatch.setenv("WRR_EXA_MODE_DEEP_TIMEOUT", "22")

    importlib.reload(config)
    import wrr.engines.exa as exa_mod_reloaded
    importlib.reload(exa_mod_reloaded)

    assert config.EXA_MODE_TIMEOUT["fast"] == 4.0
    assert config.EXA_MODE_TIMEOUT["auto"] == 6.0
    assert config.EXA_MODE_TIMEOUT["deep-lite"] == 9.0
    assert config.EXA_MODE_TIMEOUT["deep"] == 22.0
    assert exa_mod_reloaded.get_timeout_for_mode("deep") == 22.0
    assert exa_mod_reloaded.get_timeout_for_mode("unknown-mode") == 6.0

    # 清理
    monkeypatch.delenv("WRR_EXA_MODE_FAST_TIMEOUT", raising=False)
    monkeypatch.delenv("WRR_EXA_MODE_AUTO_TIMEOUT", raising=False)
    monkeypatch.delenv("WRR_EXA_MODE_DEEPLITE_TIMEOUT", raising=False)
    monkeypatch.delenv("WRR_EXA_MODE_DEEP_TIMEOUT", raising=False)
    importlib.reload(config)


def test_exa_deep_default_is_less_tight_than_previous_10s():
    # 无 env 覆盖状态下 reload
    importlib.reload(config)
    import wrr.engines.exa as exa_mod_reloaded
    importlib.reload(exa_mod_reloaded)

    assert config.EXA_MODE_TIMEOUT["deep"] >= 15.0
    # 严格递增验证
    assert config.EXA_MODE_TIMEOUT["fast"] < config.EXA_MODE_TIMEOUT["auto"]
    assert config.EXA_MODE_TIMEOUT["auto"] < config.EXA_MODE_TIMEOUT["deep-lite"]
    assert config.EXA_MODE_TIMEOUT["deep-lite"] < config.EXA_MODE_TIMEOUT["deep"]
