"""Memory pipes exercise the same NDJSON seam as the OMP extension."""
import asyncio
import json

import pytest

from wrr.core.contracts import Health, Kind, PrivacyClass, Provision, SourceDescriptor, Transport
from wrr.core.judge import JudgeError, noul
from wrr.core.rpc import RpcChannel, RpcHostJudge, RpcHostSearch, serve_rpc
from wrr.core.sources import AdapterError


def feed(reader, message):
    reader.feed_data((json.dumps(message) + "\n").encode())


def answers(questions):
    result = {}
    for key, question in questions.items():
        if question["type"] == "choice":
            labels = list(question["criteria"])
            selected = "accept" if "accept" in labels else "overview" if "overview" in labels else labels[0]
            result[key] = {"type": "choice", "choice": selected, "confidence": .95,
                           "probabilities": {label: .95 if label == selected else .05 / (len(labels) - 1) for label in labels}}
        else:
            result[key] = {"type": "noul", "noul": .01 if key.startswith("inj.") else .95}
    return {"answers": result, "model": "jev-1.13.0", "usage": {"input_tokens": 10, "output_tokens": 0}}


def test_rpc_search_roundtrip_host_judge_provider_and_evidence():
    async def scenario():
        reader = asyncio.StreamReader()
        messages = []
        def emit(message):
            messages.append(message)
            if message.get("method") == "judge":
                feed(reader, {"id": message["id"], "result": answers(message["params"]["questions"])})
            elif message.get("method") == "host_search":
                feed(reader, {"id": message["id"], "result": {"provider": "exa", "results": [
                    {"title": "asyncio", "url": "https://docs.python.org/3/library/asyncio.html", "snippet": "Asynchronous I/O"}]}})
        feed(reader, {"query": "Python asyncio", "count": 2, "deadline_ms": 2000,
                      "host": {"judge": True, "search": True}})
        inventory = []
        def factory(hosts):
            inventory.extend(hosts)
            return inventory, {}
        code = await serve_rpc(reader, emit, inventory_factory=factory, env={})
        evidence = messages[-1]["result"]
        assert code == 0
        assert evidence["plan"]["judge"] == "host"
        assert evidence["plan"]["sources"] == ["host:web"]
        assert evidence["diagnostics"]["judge"]["calls"] == 3
        assert evidence["diagnostics"]["judge"]["posts"] == 3
        assert evidence["diagnostics"]["judge"]["model"] == "jev-1.13.0"
        assert evidence["diagnostics"]["judge"]["usage"]["input_tokens"] == 30
        assert evidence["quality"]["independent_families"] == 1
        assert evidence["results"][0]["url"] == "https://docs.python.org/3/library/asyncio.html"
        assert inventory[0].upstream_family == "exa"
        assert inventory[0].transport == Transport.HOST
        assert inventory[0].privacy_class == PrivacyClass.PUBLIC
        assert all(q["type"] != "bool" for m in messages if m.get("method") == "judge" for q in m["params"]["questions"].values())
    asyncio.run(scenario())


@pytest.mark.parametrize("sources", [None, ["host:web"], ["missing"], ["one", "two", "three", "four", "five"]])
def test_rpc_sensitive_query_returns_blocked_without_host_requests(sources):
    async def scenario():
        query = "Contact alice@example.com"
        reader = asyncio.StreamReader()
        feed(reader, {"query": query, "sources": sources, "host": {"judge": True, "search": True}})
        messages = []
        code = await serve_rpc(reader, messages.append,
                               inventory_factory=lambda hosts: (hosts, {}), env={})
        assert code == 1
        assert len(messages) == 1
        evidence = messages[0]["result"]
        assert evidence["query"] == query
        assert evidence["plan"]["gate"] == "sensitive_query"
        assert evidence["plan"]["judge"] == "skipped:sensitive_query"
        assert evidence["plan"]["sources"] == []
        assert evidence["plan"]["reasons"] == {}
        assert evidence["quality"]["executed"] == "blocked"
        assert evidence["quality"]["relevance"] == "skipped"
        assert evidence["diagnostics"]["judge"]["calls"] == 0
        assert evidence["diagnostics"]["judge"]["posts"] == 0
        assert evidence["runs"] == evidence["results"] == []
        assert query not in json.dumps(evidence["diagnostics"])
    asyncio.run(scenario())


@pytest.mark.parametrize("provider,expected", [(None, "host-web:opaque"), ("exa", "exa"),
                                                   ("none", "host-web:opaque"), ("bad key value", "host-web:opaque")])
def test_host_search_provider_family_metadata(provider, expected):
    async def scenario():
        reader = asyncio.StreamReader()
        channel = RpcChannel(reader, lambda m: feed(reader, {"id": m["id"], "result": {
            "provider": provider, "results": [{"url": "https://example.org", "title": "Example"}]}}))
        adapter = RpcHostSearch(channel)
        adapter.inventory = [adapter.descriptor]
        listener = asyncio.create_task(channel.listen())
        try:
            rows = await adapter.search("public", 1, deadline=asyncio.get_running_loop().time() + 1)
            assert rows[0].source_tag == "host:web"
            assert adapter.inventory[0].upstream_family == expected
        finally:
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass
    asyncio.run(scenario())


def test_host_judge_timeout_sends_request_cancel_without_retry():
    async def scenario():
        messages = []
        channel = RpcChannel(asyncio.StreamReader(), messages.append)
        judge = RpcHostJudge(channel, soft_budget=.01)
        with pytest.raises(JudgeError) as failure:
            await judge.judge({"public": True}, {"ok": noul("Is it public?")})
        assert failure.value.reason == "timeout"
        assert failure.value.posts == 1
        assert [m["method"] for m in messages] == ["judge", "cancel"]
        assert messages[0]["id"] == messages[1]["id"]
    asyncio.run(scenario())


def test_host_judge_expired_budget_does_not_send_request():
    async def scenario():
        messages = []
        judge = RpcHostJudge(RpcChannel(asyncio.StreamReader(), messages.append))
        with pytest.raises(JudgeError) as failure:
            await judge.judge({}, {"ok": noul("Check")}, deadline=asyncio.get_running_loop().time() - 1)
        assert failure.value.reason == "timeout"
        assert failure.value.posts == 0
        assert messages == []
    asyncio.run(scenario())


@pytest.mark.parametrize("reply,reason", [({"error": {"reason": "authentication"}}, "authentication"),
                                          ({"error": {"reason": "secret text"}}, "unavailable"),
                                          ({"error": {"reason": ["not a reason"]}}, "unavailable"),
                                          ({"result": {"answers": {}}}, "malformed_response"),
                                          ({"result": {"answers": {"ok": {"type": "noul", "noul": True}}}}, "malformed_response")])
def test_host_judge_error_and_schema_semantics(reply, reason):
    async def scenario():
        reader = asyncio.StreamReader()
        channel = RpcChannel(reader, lambda m: feed(reader, {"id": m["id"], **reply}))
        listener = asyncio.create_task(channel.listen())
        try:
            with pytest.raises(JudgeError) as failure:
                await RpcHostJudge(channel).judge({}, {"ok": noul("Check")})
            assert failure.value.reason == reason
        finally:
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["judge", "host_search"])
def test_rpc_cancel_is_cancelled_evidence_not_fail_open(phase):
    async def scenario():
        reader = asyncio.StreamReader()
        messages = []
        def emit(message):
            messages.append(message)
            if message.get("method") == phase:
                feed(reader, {"method": "cancel"})
            elif message.get("method") == "judge":
                feed(reader, {"id": message["id"], "result": answers(message["params"]["questions"])})
        feed(reader, {"query": "Python asyncio", "host": {"judge": True, "search": True}})
        code = await serve_rpc(reader, emit, inventory_factory=lambda hosts: (hosts, {}), env={})
        assert code == 130
        assert messages[-1]["result"]["quality"]["executed"] == "cancelled"
        assert messages[-1]["result"]["results"] == []
    asyncio.run(scenario())


def test_rpc_no_host_uses_no_judge_and_no_host_source():
    async def scenario():
        reader = asyncio.StreamReader()
        feed(reader, {"query": "Python asyncio", "host": {"judge": False, "search": False}})
        messages = []
        def factory(hosts):
            assert hosts == []
            return [], {}
        assert await serve_rpc(reader, messages.append, inventory_factory=factory, env={}) == 1
        evidence = messages[-1]["result"]
        assert evidence["plan"]["judge"] == "none"
        assert evidence["diagnostics"]["judge"]["calls"] == 0
        assert evidence["plan"]["sources"] == []
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [{}, {"query": "x", "count": True}, {"query": "x", "deadline_ms": 0},
                                     {"query": "x", "host": {"judge": "yes"}}, {"query": "x", "sources": "exa"}])
def test_rpc_invalid_initial_message_is_safe_error(payload):
    async def scenario():
        reader = asyncio.StreamReader()
        feed(reader, payload)
        messages = []
        code = await serve_rpc(reader, messages.append, inventory_factory=lambda hosts: (hosts, {}), env={})
        assert code == 2
        assert messages == [{"error": {"reason": "invalid_request"}}]
    asyncio.run(scenario())


def test_host_search_rejects_malformed_rows():
    async def scenario():
        reader = asyncio.StreamReader()
        channel = RpcChannel(reader, lambda m: feed(reader, {"id": m["id"], "result": {"results": [{"url": 1}]}}))
        listener = asyncio.create_task(channel.listen())
        try:
            with pytest.raises(AdapterError) as failure:
                await RpcHostSearch(channel).search("public", 1, deadline=asyncio.get_running_loop().time() + 1)
            assert failure.value.status.value == "schema_error"
        finally:
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass
    asyncio.run(scenario())


def test_rpc_multiplexes_out_of_order_replies():
    async def scenario():
        reader = asyncio.StreamReader()
        requests = []
        async def emit(message):
            requests.append(message)
            if len(requests) == 2:
                for request in reversed(requests):
                    feed(reader, {"id": request["id"], "result": request["params"]["value"]})
        channel = RpcChannel(reader, emit)
        listener = asyncio.create_task(channel.listen())
        try:
            deadline = asyncio.get_running_loop().time() + 1
            result = await asyncio.gather(
                channel.request("judge", {"value": "first"}, deadline=deadline),
                channel.request("host_search", {"value": "second"}, deadline=deadline),
            )
            assert result == ["first", "second"]
        finally:
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass
    asyncio.run(scenario())


@pytest.mark.parametrize("termination", ["eof", "invalid_id"])
def test_rpc_transport_loss_cancels_inflight_search(termination):
    async def scenario():
        reader = asyncio.StreamReader()
        messages = []
        def emit(message):
            messages.append(message)
            if message.get("method") == "host_search":
                if termination == "eof":
                    reader.feed_eof()
                else:
                    feed(reader, {"id": {}, "result": {}})
        feed(reader, {"query": "Python asyncio", "host": {"search": True}})
        code = await serve_rpc(reader, emit, inventory_factory=lambda hosts: (hosts, {}), env={})
        assert code == 130
        assert messages[-1]["result"]["quality"]["executed"] == "cancelled"
    asyncio.run(scenario())


@pytest.mark.parametrize("provider,expected_families", [("exa", 1), (None, 1)])
def test_rpc_host_family_overlap_does_not_double_count(provider, expected_families):
    async def scenario():
        reader = asyncio.StreamReader()
        messages = []
        url = "https://docs.python.org/3/library/asyncio.html"
        def emit(message):
            messages.append(message)
            if message.get("method") == "host_search":
                feed(reader, {"id": message["id"], "result": {
                    "provider": provider, "results": [{"title": "asyncio", "url": url}],
                }})
        class Exa:
            async def search(self, query, count, *, deadline):
                from wrr.schemas import SearchResult
                return [SearchResult(title="asyncio", url=url)]
        source = SourceDescriptor(
            id="exa", kind=Kind.WEB, transport=Transport.API, upstream_family="exa",
            privacy_class=PrivacyClass.PUBLIC, provision=Provision.AUTHORIZED, health=Health.HEALTHY,
        )
        feed(reader, {"query": "Python asyncio", "sources": ["exa", "host:web"],
                      "host": {"search": True}})
        code = await serve_rpc(reader, emit, inventory_factory=lambda hosts: ([source, *hosts], {"exa": Exa()}), env={})
        evidence = messages[-1]["result"]
        assert code == 0
        assert evidence["quality"]["independent_families"] == expected_families
        assert len(evidence["results"]) == 1
        assert len(evidence["results"][0]["contributions"]) == 2
        if provider == "exa":
            assert evidence["results"][0]["rrf_score"] == .016393
    asyncio.run(scenario())


def test_rpc_rejects_query_over_4096_characters():
    async def scenario():
        reader = asyncio.StreamReader()
        feed(reader, {"query": "x" * 4097})
        messages = []
        assert await serve_rpc(reader, messages.append, inventory_factory=lambda hosts: ([], {}), env={}) == 2
        assert messages == [{"error": {"reason": "query_too_long"}}]
    asyncio.run(scenario())


def test_rpc_truncates_explicit_sources_with_evidence_reason():
    async def scenario():
        reader = asyncio.StreamReader()
        feed(reader, {"query": "地球四季", "sources": ["a", "b", "c", "d", "e"]})
        messages = []
        inventory = [SourceDescriptor(
            id=source, kind=Kind.HOST, transport=Transport.HOST,
            upstream_family=source, privacy_class=PrivacyClass.PUBLIC,
            provision=Provision.AUTHORIZED, health=Health.HEALTHY,
        ) for source in ("a", "b", "c", "d")]
        await serve_rpc(reader, messages.append, inventory_factory=lambda hosts: (inventory, {}), env={})
        evidence = messages[-1]["result"]
        assert evidence["plan"]["reasons"]["e"] == "dropped:max_sources"
        assert "e" not in evidence["plan"]["sources"]
    asyncio.run(scenario())


def test_stdio_rpc_emits_non_ascii_evidence_on_ascii_stdout(monkeypatch):
    import io
    import sys
    from wrr.core.rpc import stdio_rpc

    class AsciiOutput(io.StringIO):
        def write(self, text):
            text.encode("ascii")
            return super().write(text)

    async def scenario():
        output = AsciiOutput()
        monkeypatch.setattr(sys, "stdout", output)
        loop = asyncio.get_running_loop()

        class Transport:
            def close(self):
                pass

        async def connect(factory, pipe):
            protocol = factory()
            feed(protocol._stream_reader, {"query": "地球四季"})
            return Transport(), protocol

        monkeypatch.setattr(loop, "connect_read_pipe", connect)
        await stdio_rpc(inventory_factory=lambda hosts: ([], {}), env={})
        assert json.loads(output.getvalue())["result"]["query"] == "地球四季"
    asyncio.run(scenario())


def test_rpc_site_syntax_error_is_visible_without_host_requests():
    async def scenario():
        reader = asyncio.StreamReader()
        feed(reader, {"query": "-site:python.org private-marker",
                      "host": {"judge": True, "search": True}})
        messages = []
        code = await serve_rpc(reader, messages.append,
                               inventory_factory=lambda hosts: (hosts, {}), env={})
        assert code == 2
        assert len(messages) == 1
        assert messages[0]["error"]["reason"] == "site_constraint_syntax"
        assert "only positive site:hostname" in messages[0]["error"]["message"]
        assert "private-marker" not in json.dumps(messages)
    asyncio.run(scenario())
