"""Real-wire regressions for the final protocol acceptance boundaries."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from wrr.engines.configured_sources import load_source_config
from wrr.errors import EngineError


def _server(tmp_path, mode):
    server = tmp_path / "server.py"
    receipt = tmp_path / "called.json"
    server.write_text('''import json,os,signal,subprocess,sys,time
from pathlib import Path
mode,receipt=sys.argv[1:]
for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request: continue
    response={'jsonrpc':'2.0','id':request['id']}
    method=request.get('method')
    if method=='initialize':
        response['result']={'protocolVersion':request['params'].get('protocolVersion','2025-11-25'),'capabilities':{'tools':{}},'serverInfo':{'name':'residual-fixture','version':'1'}}
    elif method=='tools/list':
        response['result']={'tools':[{'name':'catalog_search','inputSchema':{'type':'object','properties':{'query':{'type':'string'},'count':{'type':'integer'}}},'annotations':{'readOnlyHint':True}}]}
    elif method=='tools/call':
        if mode=='timeout':
            response['error']={'code':-32001,'message':'controlled request timeout'}
        else:
            child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            Path(receipt).write_text(json.dumps({'server':os.getpid(),'child':child.pid,'group':os.getpgrp()}))
            if mode=='response_pending':
                response['result']={'content':[{'type':'text','text':json.dumps({'results':[{'title':'Mandarin Guide','url':'https://fixture.invalid/mandarin'}]})}]}
                print(json.dumps(response),flush=True)
            time.sleep(60)
    else: response['result']={}
    print(json.dumps(response),flush=True)
''')
    cfg = {"kind": "mcp", "authorized": True,
           "command": [sys.executable, str(server), mode, str(receipt)],
           "tool": "catalog_search", "shared_env_keys": []}
    return cfg, receipt


def test_server_request_timeout_retains_public_source_classification(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from wrr.engines.community import CommunityEngine, CommunitySearchError
    from wrr.schemas import SearchOptions
    cfg, _ = _server(tmp_path, "timeout")
    cfg.pop("authorized")
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"schema_version": 1, "sources": {"catalog": cfg},
                               "default_sources": ["catalog"], "authorized_sources": ["catalog"]}))
    path.chmod(0o600)
    monkeypatch.setenv("WRR_COMMUNITY_CONFIG", str(path))
    with pytest.raises(CommunitySearchError) as raised:
        asyncio.run(CommunityEngine().search(SearchOptions("Mandarin", provider="community")))
    source = raised.value.details["sources"][0]
    assert source["outcome"] == "timeout"
    assert source["detail"] == "mcp_stdio.timeout"


def test_oversized_engagement_limit_is_safe_configuration_error(tmp_path, monkeypatch):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"schema_version": 1, "sources": {
        "catalog": {"kind": "opencli", "command": [sys.executable], "eng_max": 10 ** 400}},
        "default_sources": ["catalog"], "authorized_sources": ["catalog"]}))
    path.chmod(0o600)
    monkeypatch.setenv("WRR_COMMUNITY_CONFIG", str(path))
    with pytest.raises(EngineError) as raised:
        load_source_config(reserved=())
    assert str(10 ** 400) not in str(raised.value)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("deadline,cancel_at,mode", [
    (20, 0, "pending"), (3, 2.8, "pending"), (3, 3.2, "pending"),
    (20, 1.2, "response_pending"), (1.2, 1.4, "response_pending"),
])
def test_repeated_native_cancellation_reaps_noncooperative_server(tmp_path, deadline, cancel_at, mode):
    pytest.importorskip("mcp")
    cfg, receipt = _server(tmp_path, mode)
    config = tmp_path / "driver-config.json"
    config.write_text(json.dumps(cfg))
    driver = '''import asyncio,json,os,sys,time
from pathlib import Path
from wrr.engines.mcp_source import McpSourceAdapter
from wrr.schemas import SearchOptions
async def main():
    cfg=json.loads(Path(sys.argv[1]).read_text())
    receipt=Path(sys.argv[2])
    launched=time.monotonic()
    task=asyncio.create_task(McpSourceAdapter().fetch_result(cfg,SearchOptions('Mandarin',count=1),None,float(sys.argv[3])))
    async with asyncio.timeout(6):
        while not receipt.exists():
            if task.done(): raise RuntimeError('server did not enter call')
            await asyncio.sleep(.01)
    await asyncio.sleep(max(0,float(sys.argv[4])-(time.monotonic()-launched)))
    started=time.monotonic()
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(.1)
    done,_=await asyncio.wait({task},timeout=max(0,8-(time.monotonic()-started)))
    recorded=json.loads(receipt.read_text())
    alive=[]
    for key,pid in recorded.items():
        if key=='group':continue
        try:os.kill(pid,0);alive.append(pid)
        except ProcessLookupError:pass
    observation={'driver_alive':True,'task_done':bool(done),'seconds':time.monotonic()-started,'survivors_before_intervention':alive}
    if not done:
        print(json.dumps(observation),flush=True)
        os._exit(2)
    try:
        await task
    except asyncio.CancelledError:
        observation['cancelled']=True
        print(json.dumps(observation),flush=True)
    else:
        raise RuntimeError('cancellation was swallowed')
asyncio.run(main())
'''
    process = subprocess.Popen(
        [sys.executable, "-c", driver, str(config), str(receipt), str(deadline), str(cancel_at)],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ, PYTHONPATH="."),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timed_out = False
    living = []
    try:
        try:
            stdout, stderr = process.communicate(timeout=18)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout, stderr = process.communicate(timeout=3)
        recorded = json.loads(receipt.read_text()) if receipt.exists() else {}
        deadline = time.monotonic() + 1
        while recorded and time.monotonic() < deadline:
            living = [pid for key, pid in recorded.items() if key != "group" and _alive(pid)]
            if not living:
                break
            time.sleep(.01)
        assert not timed_out, "MCP cancellation failed to finish within the outer harness deadline"
        assert process.returncode == 0, stdout + stderr
        result = json.loads(stdout)
        assert result["cancelled"] is True
        assert result["seconds"] < 8
        assert result["survivors_before_intervention"] == []
        assert recorded and living == []
    finally:
        # Reclaim only this fixture's live group after observation; never count
        # harness cleanup as product teardown success.
        if receipt.exists():
            recorded = json.loads(receipt.read_text())
            if _alive(recorded["server"]):
                try:
                    if os.getpgid(recorded["server"]) == recorded["group"]:
                        os.killpg(recorded["group"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
