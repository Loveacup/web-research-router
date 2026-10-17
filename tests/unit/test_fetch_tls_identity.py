"""An IP-pinned connection must not carry a different HTTPS origin."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import ssl
import subprocess
import threading

import httpx
import pytest

from wrr.engines.brave import BraveEngine
from wrr.schemas import ExtractOptions


def test_same_ip_cross_origin_redirect_requires_new_tls_identity(monkeypatch, tmp_path):
    # Generate a fixture-only trust root for alpha.test, deliberately not beta.test.
    certificate = tmp_path / 'certificate.pem'
    key = tmp_path / 'key.pem'
    subprocess.run([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-subj', '/CN=alpha.test', '-addext', 'subjectAltName=DNS:alpha.test',
        '-keyout', str(key), '-out', str(certificate),
    ], check=True, capture_output=True, timeout=20)
    hosts = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            hosts.append(self.headers['Host'])
            if self.path == '/start':
                self.send_response(302)
                self.send_header('Location', f'https://beta.test:{server.server_port}/finish')
                self.send_header('Content-Length', '0')
                self.end_headers()
            else:
                body = b'Wrong TLS origin accepted'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, key)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_context = ssl.create_default_context(cafile=str(certificate))

    from httpx._client import AsyncClient
    from httpcore._backends.anyio import AnyIOBackend
    original_connect = AnyIOBackend.connect_tcp

    async def fixture_connect(self, host, port, **kwargs):
        assert host == '8.8.8.8' and port == server.server_port
        return await original_connect(self, host='127.0.0.1', port=port, **kwargs)

    monkeypatch.setattr(AnyIOBackend, 'connect_tcp', fixture_connect)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: AsyncClient(
        **kwargs, verify=client_context))

    async def scenario():
        loop = asyncio.get_running_loop()
        original_resolve = loop.getaddrinfo

        async def fixture_resolve(host, port, **kwargs):
            if host in {'alpha.test', 'beta.test'}:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]
            return await original_resolve(host, port, **kwargs)

        monkeypatch.setattr(loop, 'getaddrinfo', fixture_resolve)
        with pytest.raises(httpx.ConnectError, match='CERTIFICATE_VERIFY_FAILED'):
            await BraveEngine().extract(ExtractOptions(f'https://alpha.test:{server.server_port}/start'))

    try:
        asyncio.run(scenario())
        assert hosts == [f'alpha.test:{server.server_port}']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
