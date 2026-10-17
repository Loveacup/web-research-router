"""Direct HTTP fetch: public addresses only, validated again at every redirect.

Resolve once per origin and dial that literal address; retain the original HTTP
Host and TLS identity. Environment proxies must not bypass this boundary.

Optional proxy mode (WRR_FETCH_PROXY=http://host:port in WRR's own env) serves
fake-IP DNS setups: names are resolved via DNS-over-HTTPS through that proxy
(WRR_FETCH_DOH_URL, default Cloudflare), every answer must pass the same public
check, and the proxy is asked to CONNECT to the checked IP literal while TLS
still verifies the original hostname. Private-origin grants never use the proxy.
"""
import asyncio
import ipaddress
import socket
from contextlib import AsyncExitStack

import httpcore
import httpx

from . import config
from .errors import EngineError, FetchPolicyError

_REDIRECTS = {301, 302, 303, 307, 308}
_TRANSITION_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '64:ff9b::/96', '64:ff9b:1::/48', '2002::/16', '2001::/32', '192.88.99.0/24',
))


def _url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
        if (url.scheme not in {'http', 'https'} or not url.host
                or url.userinfo or '%' in url.host
                or (url.port is not None and not 0 < url.port < 65536)):
            raise ValueError
    except (TypeError, ValueError, httpx.InvalidURL):
        raise FetchPolicyError('fetch denied: invalid HTTP(S) target') from None
    return url


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    return url.scheme, url.raw_host.decode('ascii'), url.port or (443 if url.scheme == 'https' else 80)


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_global and not ip.is_multicast and not ip.is_reserved
            and not any(ip in network for network in _TRANSITION_NETWORKS))


async def _address(url: httpx.URL, *, allow_private: bool) -> str:
    host = url.raw_host.decode('ascii')
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(
                host, url.port or (443 if url.scheme == 'https' else 80),
                family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
            )
        except OSError as error:
            raise EngineError('fetch DNS resolution failed') from error
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or (not allow_private and not all(_public(address) for address in addresses)):
        raise FetchPolicyError('fetch denied: non-public address')
    return addresses[0]


_DEFAULT_DOH_URL = 'https://1.1.1.1/dns-query'


def _ssl_context():
    return httpx.create_ssl_context()


def _proxy_origin(value: str) -> tuple[str, int]:
    proxy = httpx.URL(value)
    if proxy.scheme != 'http' or not proxy.host or proxy.userinfo or proxy.path not in ('', '/'):
        raise EngineError('WRR_FETCH_PROXY must be http://host:port')
    return proxy.host, proxy.port or 80


async def _doh_address(url: httpx.URL, proxy: str) -> str:
    host = url.raw_host.decode('ascii')
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        doh = config.get_env('WRR_FETCH_DOH_URL') or _DEFAULT_DOH_URL
        addresses = []
        async with httpx.AsyncClient(proxy=proxy, trust_env=False, verify=_ssl_context(),
                                     follow_redirects=False) as client:
            for record_type, code in (('A', 1), ('AAAA', 28)):
                try:
                    response = await client.get(doh, params={'name': host, 'type': record_type},
                                                headers={'Accept': 'application/dns-json'})
                    response.raise_for_status()
                    answer = response.json()
                except (httpx.HTTPError, ValueError) as error:
                    raise EngineError('fetch DNS resolution failed') from error
                if answer.get('Status') != 0:
                    continue
                addresses += [item['data'] for item in answer.get('Answer') or []
                              if item.get('type') == code and isinstance(item.get('data'), str)]
        addresses = list(dict.fromkeys(addresses))
    if not addresses:
        raise EngineError('fetch DNS resolution failed')
    try:
        # Canonical form only: the address is written into the proxy CONNECT line.
        parsed = [ipaddress.ip_address(address) for address in addresses]
    except ValueError:
        raise EngineError('fetch DNS resolution failed') from None
    if any(getattr(ip, 'scope_id', None) for ip in parsed):
        raise EngineError('fetch DNS resolution failed')
    addresses = [str(ip) for ip in parsed]
    if not all(_public(address) for address in addresses):
        raise FetchPolicyError('fetch denied: non-public address')
    return addresses[0]


class _ProxyConnect(httpcore.AsyncNetworkBackend):
    """Dial one checked IP literal through an HTTP CONNECT proxy."""

    def __init__(self, proxy: str, origin: tuple[str, str, int], address: str):
        self._proxy = _proxy_origin(proxy)
        self._origin = origin
        self._address = address
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if (host, port) != self._origin[1:]:
            raise EngineError('fetch proxy refused an unpinned destination')
        stream = await self._inner.connect_tcp(*self._proxy, timeout=timeout)
        target = f'[{self._address}]:{port}' if ':' in self._address else f'{self._address}:{port}'
        try:
            await stream.write(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode('ascii'),
                               timeout=timeout)
            head = b''
            while b'\r\n\r\n' not in head:
                chunk = await stream.read(4096, timeout=timeout)
                if not chunk or len(head) + len(chunk) > 65536:
                    raise EngineError('fetch proxy CONNECT failed')
                head += chunk
            status, _, rest = head.partition(b'\r\n\r\n')
            parts = status.split(b'\r\n', 1)[0].split()
            if rest or len(parts) < 2 or not parts[0].startswith(b'HTTP/1.') or not parts[1].startswith(b'2'):
                raise EngineError('fetch proxy CONNECT failed')
        except BaseException:
            await stream.aclose()
            raise
        return stream

    async def sleep(self, seconds):
        await self._inner.sleep(seconds)


async def _pooled_get(pool: httpcore.AsyncConnectionPool, url: httpx.URL) -> httpx.Response:
    headers = [(b'Host', url.netloc), (b'User-Agent', config.USER_AGENT.encode())]
    response = await pool.request('GET', str(url), headers=headers)
    return httpx.Response(response.status, headers=response.headers, content=response.content,
                          request=httpx.Request('GET', url))



async def validate_public_target(value: str) -> None:
    """Apply the shared public-target gate without opening a connection."""
    await _address(_url(value), allow_private=False)


async def fetch_html(value: str, *, timeout: float, allow_private_origin: str | None = None) -> str:
    url = _url(value)
    grant = None
    if allow_private_origin is not None:
        granted_url = _url(allow_private_origin)
        if granted_url.path != '/' or granted_url.query or granted_url.fragment:
            raise FetchPolicyError('fetch denied: grant must be an exact origin')
        grant = _origin(granted_url)
        if grant != _origin(url):
            raise FetchPolicyError('fetch denied: origin does not match grant')
    proxy = config.get_env('WRR_FETCH_PROXY') if grant is None else None
    connections: dict[tuple[str, str, int], tuple[str, object]] = {}
    async with asyncio.timeout(timeout), AsyncExitStack() as stack:
        for hop in range(6):
            origin = _origin(url)
            if grant is not None and origin != grant:
                raise FetchPolicyError('fetch denied: redirect outside granted origin')
            connection = connections.get(origin)
            if connection is None:
                if proxy:
                    address = await _doh_address(url, proxy)
                    client = await stack.enter_async_context(httpcore.AsyncConnectionPool(
                        ssl_context=_ssl_context(), network_backend=_ProxyConnect(proxy, origin, address),
                    ))
                else:
                    address = await _address(url, allow_private=grant is not None)
                    # An IP-keyed HTTP pool cannot safely share TLS across hostnames.
                    # Each original origin owns its pool, even when IP/port match.
                    client = await stack.enter_async_context(httpx.AsyncClient(
                        follow_redirects=False, trust_env=False, timeout=timeout,
                    ))
                connection = connections[origin] = (address, client)
            address, client = connection
            if proxy:
                response = await _pooled_get(client, url)
            else:
                client.cookies.clear()
                response = await client.get(
                    url.copy_with(host=address),
                    headers={'Host': url.netloc.decode('ascii'), 'User-Agent': config.USER_AGENT},
                    extensions={'sni_hostname': url.raw_host.decode('ascii')},
                )
            if response.status_code not in _REDIRECTS:
                response.raise_for_status()
                return response.text
            if hop == 5 or not response.headers.get('location'):
                raise FetchPolicyError('fetch denied: invalid or excessive redirects')
            url = _url(str(url.join(response.headers['location'])))
