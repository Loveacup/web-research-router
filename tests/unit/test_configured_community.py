"""Shared CommunityEngine lifecycle and configured-source consumer behavior."""

import asyncio
import os
import json
import subprocess
from pathlib import Path
import sys

import pytest

from wrr import config
from wrr.engines import community
from wrr.schemas import SearchOptions


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_caller_cancellation_reaps_active_source_subprocess(tmp_path, monkeypatch):
    monkeypatch.delenv("WRR_COMMUNITY_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    program = tmp_path / "blocking_catalog.py"
    pidfile = tmp_path / "backend.pid"
    program.write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(60)\n"
    )
    monkeypatch.setattr(config, "COMMUNITY_DEFAULT_SOURCES", ("cancel_probe",))
    monkeypatch.setitem(community.COMMUNITY_SOURCES, "cancel_probe", {
        "kind": "opencli", "cli": [sys.executable, str(program), str(pidfile)],
        "title": "title", "url": "url",
    })

    async def exercise():
        before = set(asyncio.all_tasks())
        request = asyncio.create_task(community.CommunityEngine().search(
            SearchOptions("Mandarin", provider="community")))
        try:
            async with asyncio.timeout(5):
                while not pidfile.exists():
                    if request.done():
                        await request
                    await asyncio.sleep(0.01)
            pid = int(pidfile.read_text())
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            # Observe before test-only cleanup; a leaked backend must fail this assertion.
            assert not _alive(pid)
        finally:
            owned = set(asyncio.all_tasks()) - before
            for task in owned:
                task.cancel()
            if owned:
                await asyncio.gather(*owned, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["mcp", "opencli"])
def test_configured_catalog_without_optional_mappings_reaches_cli(tmp_path, kind):
    root = Path(__file__).resolve().parents[1]
    project = root.parent
    fixture = root / "fixtures" / (
        "mcp_catalog_server.py" if kind == "mcp" else "opencli_catalog.py")
    source = {"kind": kind, "command": [sys.executable, str(fixture)]}
    if kind == "mcp":
        pytest.importorskip("mcp")
        source["tool"] = "catalog_search"
        source["command"] += ["--mode", "success", "--events", str(tmp_path / "events")]
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "schema_version": 1, "sources": {"catalog": source},
        "default_sources": ["catalog"], "authorized_sources": ["catalog"],
    }))
    path.chmod(0o600)
    envfile = tmp_path / "empty.env"
    envfile.touch()
    env = dict(os.environ, WRR_COMMUNITY_CONFIG=str(path),
               WRR_ENV=str(envfile), TYPESAFE_API_KEY="")
    inventory_run = subprocess.run(
        [sys.executable, str(project / "wrr-cli.py"), "inventory", "--json"],
        env=env, capture_output=True, text=True, timeout=15)
    assert inventory_run.returncode == 0, inventory_run.stderr
    inventory = json.loads(inventory_run.stdout)
    catalog = next(source for source in inventory["sources"] if source["id"] == "catalog")
    assert catalog["id"] in inventory["routable"], catalog
    run = subprocess.run(
        [sys.executable, str(project / "wrr-cli.py"), "search", "Mandarin",
         "--sources", catalog["id"], "--count", "1", "--json"],
        env=env, capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, (run.stdout, run.stderr)
    payload = json.loads(run.stdout)
    assert "https://fixture.invalid/mandarin" in {
        result["url"] for result in payload["results"]
    }
    assert [(item["source_id"], item["status"]) for item in payload["runs"]] == [
        (catalog["id"], "ok")
    ]
