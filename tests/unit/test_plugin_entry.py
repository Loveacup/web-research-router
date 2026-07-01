"""plugin loader 入口契约测试（CC-R2-P1）。

覆盖：
  - 顶层 import-light（不拉起 httpx / yaml / wrr.router / wrr.doctor /
    wrr.engines.loader）；
  - 存在 callable ``register(ctx)``；
  - MockCtx 调用后注册 web_search / web_fetch / web_similar 3 个 tool；
  - 三者 is_async=True、toolset="wrr"；
  - OpenAI function schema 的 parameters.required 正确（query / url / url）。
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

ENTRY = Path(__file__).resolve().parents[2] / "__init__.py"

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

    def register_tool(self, name, handler, schema, toolset, is_async, override=False):
        self.tools[name] = {
            "handler": handler,
            "schema": schema,
            "toolset": toolset,
            "is_async": is_async,
            "override": override,
        }


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
    """
    plugin_dir = str(ENTRY.parent)
    code = (
        "import importlib.util, sys, os\n"
        # 清理可能从父进程继承的 repo 路径，确保 'wrr' 初始不可导入
        f"sys.path[:] = [p for p in sys.path if os.path.realpath(p) != os.path.realpath({plugin_dir!r})]\n"
        # Hermes loader 风格：submodule_search_locations=[plugin_dir]，但不动 sys.path
        f"spec = importlib.util.spec_from_file_location('wrr_plugin_entry', r'{ENTRY}', submodule_search_locations=[{plugin_dir!r}])\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        # 前置断言：此刻 wrr 不应已可解析（证明 register 确实承担了修复职责）\n"
        "assert importlib.util.find_spec('wrr') is None, 'wrr unexpectedly importable before register'\n"
        "class Ctx:\n"
        "    def __init__(self): self.tools = {}\n"
        "    def register_tool(self, name, handler, schema, toolset, is_async, override=False):\n"
        "        self.tools[name] = {'toolset': toolset, 'is_async': is_async, 'override': override}\n"
        "ctx = Ctx()\n"
        "m.register(ctx)\n"
        "assert set(ctx.tools) == {'web_search', 'web_fetch', 'web_similar'}, ctx.tools\n"
        "assert ctx.tools['web_search']['override'] is True\n"
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
