"""Fetch authorization protects the actual local HTTP boundary."""
import asyncio
import json

import httpx
import pytest

from wrr.engines.brave import BraveEngine
from wrr.schemas import ExtractOptions


def test_ungranted_loopback_never_reaches_transport(monkeypatch):
    requested = []

    async def transport(request):
        requested.append(str(request.url))
        return httpx.Response(200, text='Private fixture must not be collected')

    from httpx._client import AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: AsyncClient(
        **kwargs, transport=httpx.MockTransport(transport)))
    try:
        asyncio.run(BraveEngine().extract(ExtractOptions('http://127.0.0.1/fixture')))
    except Exception as error:
        assert 'denied' in str(error).lower()
    else:
        pytest.fail('ungranted loopback fetch succeeded')
    assert requested == []


def test_exact_private_grant_is_request_scoped(monkeypatch):
    requests = []

    async def transport(request):
        requests.append(str(request.url))
        return httpx.Response(200, text='<p>Authorized fixture body</p>')

    from httpx._client import AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: AsyncClient(
        **kwargs, transport=httpx.MockTransport(transport)))
    engine = BraveEngine()
    options = ExtractOptions('http://127.0.0.1:8080/fixture',
                             allow_private_origin='http://127.0.0.1:8080')
    body = asyncio.run(engine.extract(options))
    assert body.text == 'Authorized fixture body'
    from wrr.errors import FetchPolicyError
    with pytest.raises(FetchPolicyError):
        asyncio.run(engine.extract(ExtractOptions('http://127.0.0.1:8080/fixture')))
    assert len(requests) == 1


@pytest.mark.parametrize('target,grant', [
    ('http://127.0.0.1:8080/', 'http://127.0.0.1:8081'),
    ('http://127.0.0.1:8080/', 'https://127.0.0.1:8080'),
    ('http://127.0.0.1:8080/', 'http://localhost:8080'),
    ('http://127.0.0.1/', 'http://127.0.0.1/path'),
])
def test_mismatched_origin_grant_never_connects(monkeypatch, target, grant):
    from wrr.errors import FetchPolicyError
    requests = []

    async def transport(request):
        requests.append(str(request.url))
        return httpx.Response(200, text='Unauthorized')

    from httpx._client import AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: AsyncClient(
        **kwargs, transport=httpx.MockTransport(transport)))
    with pytest.raises(FetchPolicyError):
        asyncio.run(BraveEngine().extract(ExtractOptions(target, allow_private_origin=grant)))
    assert requests == []


@pytest.mark.parametrize('location,grant', [
    ('http://127.0.0.1/secret', None),
    ('http://127.0.0.1:8081/secret', 'http://127.0.0.1:8080'),
])
def test_redirect_cannot_expand_authorization(monkeypatch, location, grant):
    from wrr.errors import FetchPolicyError
    requests = []

    async def transport(request):
        requests.append(str(request.url))
        if len(requests) > 1:
            return httpx.Response(200, text='Unauthorized redirected content')
        return httpx.Response(302, headers={'location': location})

    from httpx._client import AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: AsyncClient(
        **kwargs, transport=httpx.MockTransport(transport)))
    target = (grant or 'http://8.8.8.8') + '/start'
    with pytest.raises(FetchPolicyError):
        asyncio.run(BraveEngine().extract(ExtractOptions(target, allow_private_origin=grant)))
    assert len(requests) == 1


@pytest.mark.parametrize('target', [
    'http://224.0.0.1/', 'http://[ff02::1]/', 'http://192.0.0.8/',
    'http://192.88.99.1/', 'http://8.8.8.8:0/', 'http://user:secret@8.8.8.8/',
    'http://[64:ff9b::7f00:1]/',
    'http://[::ffff:192.88.99.1]/',
])
def test_special_addresses_and_credentials_are_not_public_fetch_targets(monkeypatch, target):
    from wrr.errors import FetchPolicyError
    requests = []

    async def transport(request):
        requests.append(str(request.url))
        return httpx.Response(200, text='Unauthorized')

    from httpx._client import AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: AsyncClient(
        **kwargs, transport=httpx.MockTransport(transport)))
    with pytest.raises(FetchPolicyError):
        asyncio.run(BraveEngine().extract(ExtractOptions(target)))
    assert requests == []

def test_host_fetch_eligibility_rejects_private_target_without_loading_config(
        monkeypatch, capsys):
    from wrr import _cli

    monkeypatch.setattr(
        _cli, 'load_env',
        lambda *_args: pytest.fail('eligibility must not load provider configuration'),
    )
    target = 'http://user:secret@127.0.0.1/private'

    assert _cli.main(['host-fetch-eligibility', '--', target]) == 0
    payload = capsys.readouterr().out

    assert target not in payload
    assert json.loads(payload) == {
        'operation': 'host_fetch_eligibility',
        'ok': True,
        'eligible': False,
        'reason': 'policy_denied',
    }


def test_public_target_eligibility_accepts_only_all_public_dns_answers(monkeypatch):
    import socket
    from wrr.errors import FetchPolicyError
    from wrr.fetch_transport import validate_public_target

    async def scenario(addresses):
        async def resolve(*_args, **_kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))
                for address in addresses
            ]

        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        await validate_public_target('https://fixture.invalid/article')

    asyncio.run(scenario(['8.8.8.8', '1.1.1.1']))
    with pytest.raises(FetchPolicyError):
        asyncio.run(scenario(['8.8.8.8', '127.0.0.1']))


def test_policy_denial_is_not_a_fallback_invitation(monkeypatch):
    from conftest import FakeEngine
    from wrr import config
    from wrr.errors import AllEnginesFailedError, TerminalReason
    from wrr.registry import EngineRegistry
    from wrr.router import route

    class Backup(FakeEngine):
        calls = 0

        async def extract(self, options):
            self.calls += 1
            return await super().extract(options)

    backup = Backup('exa', extract_text='Must not bypass local denial')
    registry = EngineRegistry()
    registry.register(BraveEngine())
    registry.register(backup)
    monkeypatch.setattr(config, 'EXTRACT_FALLBACK_ORDER', ['brave', 'exa'])
    with pytest.raises(AllEnginesFailedError) as failure:
        asyncio.run(route('extract', ExtractOptions('http://127.0.0.1/fixture'), registry))
    assert backup.calls == 0
    assert 'denied' in str(failure.value)
    assert failure.value.terminal_reason is TerminalReason.FETCH_POLICY_DENIED
    assert failure.value.diagnostics is not None


def test_mixed_dns_answers_are_rejected_before_transport(monkeypatch):
    import socket
    from wrr.errors import FetchPolicyError
    requests = []

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80))
                for ip in ['8.8.8.8', '127.0.0.1']]

    async def transport(request):
        requests.append(str(request.url))
        return httpx.Response(200, text='DNS bypass')

    from httpx._client import AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: AsyncClient(
        **kwargs, transport=httpx.MockTransport(transport)))

    async def scenario():
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        with pytest.raises(FetchPolicyError):
            await BraveEngine().extract(ExtractOptions('http://fixture.invalid/'))

    asyncio.run(scenario())
    assert requests == []


def test_same_origin_redirect_cannot_rebind_after_initial_resolution(monkeypatch):
    import socket
    resolutions = []
    requests = []

    async def resolve(*args, **kwargs):
        resolutions.append(args)
        ip = '127.0.0.1' if len(resolutions) == 1 else '127.0.0.2'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 8080))]

    async def transport(request):
        requests.append(request)
        if request.url.host != '127.0.0.1':
            return httpx.Response(200, text='Rebound private service')
        if request.url.path == '/start':
            return httpx.Response(302, headers={'location': '/finish'})
        return httpx.Response(200, text='Authorized fixture')

    from httpx._client import AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: AsyncClient(
        **kwargs, transport=httpx.MockTransport(transport)))

    async def scenario():
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        return await BraveEngine().extract(ExtractOptions(
            'http://fixture.invalid:8080/start', allow_private_origin='http://fixture.invalid:8080'))

    body = asyncio.run(scenario())
    assert body.text == 'Authorized fixture'
    assert [request.headers['host'] for request in requests] == ['fixture.invalid:8080'] * 2


@pytest.mark.parametrize('provider', [None, 'exa'])
def test_private_grant_cannot_be_forwarded_to_implicit_or_remote_executor(provider):
    from conftest import FakeEngine
    from wrr.errors import AllEnginesFailedError, TerminalReason
    from wrr.registry import EngineRegistry
    from wrr.router import route
    calls = []

    class Extractor(FakeEngine):
        async def extract(self, options):
            calls.append(self.name)
            return await super().extract(options)

    registry = EngineRegistry()
    for name in ('brave', 'exa'):
        registry.register(Extractor(name, extract_text='Must not run'))
    options = ExtractOptions('http://127.0.0.1/', allow_private_origin='http://127.0.0.1')
    with pytest.raises(AllEnginesFailedError) as failure:
        asyncio.run(route('extract', options, registry, explicit_provider=provider))
    assert calls == []
    assert failure.value.terminal_reason is TerminalReason.FETCH_POLICY_DENIED


def test_terminal_failure_reason_is_closed():
    from wrr.errors import AllEnginesFailedError

    with pytest.raises((TypeError, ValueError)):
        AllEnginesFailedError('down', terminal_reason='unknown')


def test_cancellation_during_dns_does_not_connect_or_fallback(monkeypatch):
    from conftest import FakeEngine
    from wrr import config
    from wrr.registry import EngineRegistry
    from wrr.router import route
    calls = []

    class Backup(FakeEngine):
        async def extract(self, options):
            calls.append('backup')
            return await super().extract(options)

    async def scenario():
        started = asyncio.Event()

        async def resolve(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        async def transport(request):
            calls.append('http')
            return httpx.Response(200, text='Unexpected')

        from httpx._client import AsyncClient
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: AsyncClient(
            **kwargs, transport=httpx.MockTransport(transport)))
        monkeypatch.setattr(config, 'EXTRACT_FALLBACK_ORDER', ['brave', 'exa'])
        registry = EngineRegistry()
        registry.register(BraveEngine())
        registry.register(Backup('exa', extract_text='Unexpected'))
        task = asyncio.create_task(route('extract', ExtractOptions('http://fixture.invalid/'), registry))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert calls == []


def test_cancellation_closes_active_http_socket_without_fallback(monkeypatch):
    from conftest import FakeEngine
    from httpcore._backends.anyio import AnyIOBackend
    from httpx._client import AsyncClient
    from wrr import config
    from wrr.registry import EngineRegistry
    from wrr.router import route
    calls = []

    class Backup(FakeEngine):
        async def extract(self, options):
            calls.append('backup')
            return await super().extract(options)

    async def scenario():
        received = asyncio.Event()
        closed = asyncio.Event()

        async def serve(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                calls.append('http')
                received.set()
                assert await reader.read() == b''
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        original_connect = AnyIOBackend.connect_tcp

        async def fixture_connect(self, host, port, **kwargs):
            assert host == '8.8.8.8'
            return await original_connect(self, host='127.0.0.1', port=port, **kwargs)

        monkeypatch.setattr(AnyIOBackend, 'connect_tcp', fixture_connect)
        monkeypatch.setattr(httpx, 'AsyncClient', AsyncClient)
        monkeypatch.setattr(config, 'EXTRACT_FALLBACK_ORDER', ['brave', 'exa'])
        registry = EngineRegistry()
        registry.register(BraveEngine())
        registry.register(Backup('exa', extract_text='Unexpected'))
        async with server:
            task = asyncio.create_task(route('extract', ExtractOptions(f'http://8.8.8.8:{port}/'), registry))
            await asyncio.wait_for(received.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(closed.wait(), 2)

    asyncio.run(scenario())
    assert calls == ['http']


def test_idna_dns_name_matches_http_origin(monkeypatch):
    import socket
    requests = []

    async def resolve(host, port, **kwargs):
        # DNS is an ASCII protocol. A different IDNA encoding names a different service.
        address = '8.8.8.8' if host == 'xn--fa-hia.test' else '127.0.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port))]

    async def transport(request):
        requests.append(request)
        return httpx.Response(200, text='Correct IDNA origin')

    from httpx._client import AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: AsyncClient(
        **kwargs, transport=httpx.MockTransport(transport)))

    async def scenario():
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        return await BraveEngine().extract(ExtractOptions('https://faß.test/'))

    result = asyncio.run(scenario())
    assert result.text == 'Correct IDNA origin'
    assert requests[0].headers['host'] == 'xn--fa-hia.test'
