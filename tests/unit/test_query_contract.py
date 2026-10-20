"""Cross-host observable query policy; providers are deterministic and offline."""
import asyncio
import json

import pytest

from conftest import FakeEngine, mk_results
from wrr import _cli
from wrr.registry import EngineRegistry


def test_default_search_has_same_policy_in_cli_and_hermes(monkeypatch, capsys):
    from wrr.core import hermes_bridge, inventory, judge, sources
    from wrr.core.contracts import (
        Health, Kind, PrivacyClass, Provision, SourceDescriptor, Transport,
    )
    from wrr.core.judge import ReplayJudge

    descriptors = [
        SourceDescriptor(
            sid, Kind.WEB, Transport.API, sid, PrivacyClass.PUBLIC,
            provision=Provision.AUTHORIZED, health=Health.HEALTHY, primary=True,
        )
        for sid in ("exa", "deepseek")
    ]
    calls = []

    class Adapter:
        async def search(self, query, count, *, deadline):
            calls.append((query, count))
            return mk_results(2)

    def respond(state, questions):
        return {
            key: (
                {"type": "choice", "choice": selected, "confidence": .9,
                 "probabilities": {option: float(option == selected)
                                   for option in question.criteria}}
                if question.type == "choice"
                else {"type": "noul", "noul": .1 if key.startswith("inj.") else .9}
            )
            for key, question in questions.items()
            for selected in ["overview" if key == "evidence_shape" else "accept"]
        }

    monkeypatch.setattr(inventory, "build_inventory", lambda **kwargs: descriptors)
    monkeypatch.setattr(sources, "build_adapters", lambda *args, **kwargs:
                        {descriptor.id: Adapter() for descriptor in descriptors})
    monkeypatch.setattr(judge, "resolve_judge", lambda **kwargs: ReplayJudge(respond))
    assert _cli.main(["search", "needle", "--count", "3", "--json"]) == 0
    cli_result = json.loads(capsys.readouterr().out)
    hermes_result = hermes_bridge.search_sync("needle", count=3, runtime="hermes")
    assert cli_result["plan"] == hermes_result["plan"]
    assert cli_result["plan"]["sources"] == ["exa", "deepseek"]
    assert cli_result["plan"]["judge"] == "replay"
    assert cli_result["quality"] == hermes_result["quality"]
    assert calls == [("needle", 3)] * 4


def test_cli_does_not_probe_another_hosts_credentials(monkeypatch, tmp_path):
    import builtins
    import os

    home = tmp_path / "home"
    host_env = home / ".hermes" / ".env"
    host_env.parent.mkdir(parents=True)
    host_env.write_text("WRR_CREDENTIAL_SENTINEL=private-host-value\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("WRR_ENV", raising=False)
    monkeypatch.delenv("WRR_CREDENTIAL_SENTINEL", raising=False)
    accesses = []
    original_open = builtins.open
    original_stat = os.stat

    def tracked_open(path, *args, **kwargs):
        if str(path) == str(host_env):
            accesses.append("open")
        return original_open(path, *args, **kwargs)

    def tracked_stat(path, *args, **kwargs):
        if str(path) == str(host_env):
            accesses.append("stat")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracked_open)
    monkeypatch.setattr(os, "stat", tracked_stat)
    _cli.load_env()
    assert accesses == []
    assert "WRR_CREDENTIAL_SENTINEL" not in os.environ


def test_missing_explicit_env_never_uses_default_credentials(monkeypatch, tmp_path, capsys):
    import os

    default = tmp_path / ".config" / "wrr" / "env.d" / "default.env"
    default.parent.mkdir(parents=True)
    default.write_text("WRR_CREDENTIAL_SENTINEL=wrong-account\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("WRR_ENV", raising=False)
    monkeypatch.delenv("WRR_CREDENTIAL_SENTINEL", raising=False)
    with pytest.raises(FileNotFoundError):
        _cli.load_env(str(tmp_path / "missing.env"))
    assert "WRR_CREDENTIAL_SENTINEL" not in os.environ
    assert _cli.main(["search", "needle", "--env", str(tmp_path / "missing.env"), "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"] == "configuration_error"
    assert "wrong-account" not in json.dumps(output)




def test_explicit_source_failure_never_falls_back_across_hosts(monkeypatch, capsys):
    from wrr.core import inventory, judge, sources
    from wrr.core.contracts import (
        Health, Kind, PrivacyClass, Provision, SourceDescriptor, Transport,
    )
    from wrr.core.judge import ReplayJudge

    calls = []

    class Adapter:
        def __init__(self, source_id):
            self.source_id = source_id

        async def search(self, query, count, *, deadline):
            calls.append(self.source_id)
            if self.source_id == "exa":
                raise RuntimeError("provider unavailable")
            return mk_results(2)

    descriptors = [
        SourceDescriptor(
            source_id, Kind.WEB, transport, source_id, PrivacyClass.PUBLIC,
            provision=Provision.AUTHORIZED, health=Health.HEALTHY,
        )
        for source_id, transport in [("exa", Transport.API), ("host:web", Transport.HOST)]
    ]

    def request_recovery(state, questions):
        return {
            key: (
                {"type": "choice", "choice": "need_fresher", "confidence": .9,
                 "probabilities": {option: float(option == "need_fresher")
                                   for option in question.criteria}}
                if question.type == "choice" else {"type": "noul", "noul": .1}
            )
            for key, question in questions.items()
        }

    monkeypatch.setattr(inventory, "build_inventory", lambda **kwargs: descriptors)
    monkeypatch.setattr(sources, "build_adapters", lambda *args, **kwargs: {
        descriptor.id: Adapter(descriptor.id) for descriptor in descriptors
    })
    monkeypatch.setattr(judge, "resolve_judge", lambda **kwargs: ReplayJudge(request_recovery))
    assert _cli.main(["search", "needle", "--sources", "exa", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"]["sources"] == ["exa"]
    assert [(run["source_id"], run["status"]) for run in payload["runs"]] == [
        ("exa", "error")
    ]
    assert payload["quality"]["executed"] == "failed"
    assert payload["quality"]["recovered"] is False
    assert payload["quality"]["sufficiency"]["next_action"] == "need_fresher"
    assert payload["quality"]["sufficiency"]["enough"] == .1
    assert payload["results"] == []
    assert calls == ["exa"]


@pytest.mark.parametrize("operation", ["fetch", "similar"])
def test_nonsearch_operations_share_provider_fallback_and_quality(operation, monkeypatch, capsys):
    import wrr.registry as registry_module
    from wrr.tools import web_fetch, web_similar

    registry = EngineRegistry()
    if operation == "fetch":
        registry.register(FakeEngine("exa", error="primary unavailable"))
        registry.register(FakeEngine("brave", extract_text="A deterministic article body."))
        host = web_fetch
        handler = host.handle_web_fetch
    else:
        registry.register(FakeEngine("exa", similar_results=mk_results(2)))
        host = web_similar
        handler = host.handle_web_similar
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    monkeypatch.setattr(host, "get_registry", lambda: registry)
    args = _cli.build_parser().parse_args([operation, "https://fixture.invalid/article", "--json"])
    assert args.func(args) == 0
    cli_result = json.loads(capsys.readouterr().out)
    hermes_result = json.loads(asyncio.run(handler({"url": "https://fixture.invalid/article"})))
    assert hermes_result["success"] is True
    details = hermes_result["details"]
    assert cli_result["provider"] == details["provider"]
    assert cli_result["fallback_chain"] == details["fallback_chain"]
    assert cli_result["quality"] == details["quality"]
    if operation == "fetch":
        assert cli_result["result"]["text"] == "A deterministic article body."
        assert cli_result["result"]["text"] in hermes_result["content"]
    else:
        fields = ("title", "url", "snippet", "highlights", "source_tag")
        assert [
            {key: item[key] for key in fields} for item in cli_result["result"]
        ] == [
            {key: item[key] for key in fields} for item in details["results"]
        ]
