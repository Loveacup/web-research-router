"""Proxy mode keeps the public-address gate: DoH answer checked, CONNECT to that IP, TLS to the name."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import socketserver
import ssl
import subprocess
import threading
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from wrr import fetch_transport
from wrr.errors import EngineError, FetchPolicyError

PUBLIC_IP = '8.8.8.8'


@pytest.fixture
def world(monkeypatch, tmp_path):
    certificate, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=alpha.test',
        '-addext', 'subjectAltName=DNS:alpha.test,DNS:doh.test',
        '-keyout', str(key), '-out', str(certificate),
    ], check=True, capture_output=True, timeout=20)
    state = dict(answers={'alpha.test': PUBLIC_IP}, sni=[], hosts=[], connects=[], refuse=False)

    class Origin(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            if self.path.startswith('/dns-query'):
                query = parse_qs(urlparse(self.path).query)
                name, kind = query['name'][0], query['type'][0]
                ip = state['answers'].get(name)
                answer = [{'type': 1, 'data': ip}] if ip and kind == 'A' else []
                body = json.dumps({'Status': 0, 'Answer': answer}).encode()
            else:
                state['hosts'].append(self.headers['Host'])
                body = b'<html><body>Proxy article body</body></html>'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    context.sni_callback = lambda sock, name, ctx: state['sni'].append(name)
    origin.socket = context.wrap_socket(origin.socket, server_side=True)
    port = origin.server_port

    class Proxy(socketserver.StreamRequestHandler):
        def handle(self):
            line = self.rfile.readline().decode()
            while self.rfile.readline() not in (b'\r\n', b''):
                pass
            target = line.split()[1]
            state['connects'].append(target)
            refused = state['refuse'] and target.startswith(PUBLIC_IP)
            if refused or target not in (f'{PUBLIC_IP}:{port}', f'doh.test:{port}'):
                self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                return
            upstream = socket.create_connection(('127.0.0.1', port))
            self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            self.wfile.flush()

            def pump(source, sink):
                try:
                    while data := source.recv(65536):
                        sink.sendall(data)
                except OSError:
                    pass
                finally:
                    for end in (source, sink):
                        try:
                            end.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            other = threading.Thread(target=pump, args=(upstream, self.connection))
            other.start()
            pump(self.connection, upstream)
            other.join(5)
            upstream.close()

    proxy = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Proxy)
    proxy.daemon_threads = True
    for server in (origin, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv('WRR_FETCH_PROXY', f'http://127.0.0.1:{proxy.server_address[1]}')
    monkeypatch.setenv('WRR_FETCH_DOH_URL', f'https://doh.test:{port}/dns-query')
    monkeypatch.setattr(fetch_transport, '_ssl_context', lambda: ssl.create_default_context(cafile=str(certificate)))
    # Legacy suite tests replace httpx.AsyncClient globally without restoring it.
    from httpx._client import AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', AsyncClient)

    async def no_system_dns(*args, **kwargs):
        raise AssertionError('proxy mode must not use the system resolver')

    state['port'] = port
    state['no_system_dns'] = no_system_dns
    yield state
    for server in (origin, proxy):
        server.shutdown()
        server.server_close()


def fetch(state, url, **kwargs):
    async def scenario():
        asyncio.get_running_loop().getaddrinfo = state['no_system_dns']
        return await fetch_transport.fetch_html(url, timeout=10, **kwargs)
    return asyncio.run(scenario())


def test_proxy_connects_to_checked_ip_and_verifies_hostname(world):
    text = fetch(world, f'https://alpha.test:{world["port"]}/article')
    assert 'Proxy article body' in text
    assert f'{PUBLIC_IP}:{world["port"]}' in world['connects']
    assert f'alpha.test:{world["port"]}' not in world['connects']
    assert 'alpha.test' in world['sni']
    assert world['hosts'] == [f'alpha.test:{world["port"]}']


def test_private_doh_answer_is_denied_before_any_origin_connect(world):
    world['answers']['alpha.test'] = '10.0.0.5'
    with pytest.raises(FetchPolicyError, match='non-public address'):
        fetch(world, f'https://alpha.test:{world["port"]}/article')
    assert all(target.startswith('doh.test:') for target in world['connects'])
    assert world['hosts'] == []


def test_doh_answer_with_scope_id_cannot_inject_connect_line(world):
    world['answers']['alpha.test'] = '2606:4700::1111%\r\nX-Injected: 1'
    with pytest.raises(EngineError, match='DNS resolution failed'):
        fetch(world, f'https://alpha.test:{world["port"]}/article')
    assert all(target.startswith('doh.test:') for target in world['connects'])
    assert world['hosts'] == []


def test_refused_connect_fails_closed(world):
    world['refuse'] = True
    with pytest.raises(EngineError, match='CONNECT failed'):
        fetch(world, f'https://alpha.test:{world["port"]}/article')
    assert f'{PUBLIC_IP}:{world["port"]}' in world['connects']
    assert world['hosts'] == []


def test_private_origin_grant_never_uses_proxy(world):
    class Local(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'local granted body'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    local = ThreadingHTTPServer(('127.0.0.1', 0), Local)
    threading.Thread(target=local.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{local.server_port}'
    try:
        assert fetch(world, origin + '/x', allow_private_origin=origin) == 'local granted body'
    finally:
        local.shutdown()
        local.server_close()
    assert world['connects'] == []
