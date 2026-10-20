"""Package metadata and host-independent fetch contracts."""
import asyncio
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_canonical_package_version_matches_metadata():
    import tomllib
    from wrr import __version__

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__


def test_dev_extra_declares_schema_dependencies_without_requiring_mcp():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    def names(requirements):
        return {
            re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].lower().replace("_", "-")
            for requirement in requirements
        }

    extras = project["optional-dependencies"]
    assert {"jsonschema", "referencing"} <= names(extras["dev"])
    assert "mcp" not in names(project["dependencies"])
    assert "mcp" not in names(extras["dev"])
    assert extras["mcp"] == ["mcp>=2.0,<3"]


def test_fetch_honors_request_scoped_private_origin_grant(monkeypatch):
    import httpx
    from httpx._client import AsyncClient
    from wrr.engines.brave import BraveEngine
    from wrr.registry import EngineRegistry
    from wrr.tools import web_fetch

    requests = []

    async def transport(request):
        requests.append(str(request.url))
        return httpx.Response(200, text="<p>Authorized local fixture</p>")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: AsyncClient(
        **kwargs, transport=httpx.MockTransport(transport)))
    registry = EngineRegistry()
    registry.register(BraveEngine())
    monkeypatch.setattr(web_fetch, "get_registry", lambda: registry)
    args = {"url": "http://127.0.0.1:8080/fixture", "provider": "brave"}
    before = json.loads(asyncio.run(web_fetch.handle_web_fetch(args)))
    authorized = json.loads(asyncio.run(web_fetch.handle_web_fetch({
        **args, "allow_private_origin": "http://127.0.0.1:8080",
    })))
    after = json.loads(asyncio.run(web_fetch.handle_web_fetch(args)))
    assert "denied" in before["error"] and "denied" in after["error"]
    assert authorized["success"] is True
    assert "Authorized local fixture" in authorized["content"]
    assert len(requests) == 1
