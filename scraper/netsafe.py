"""Protecção SSRF + descarregamento limitado (usado pelo Scrapy e pelas thumbnails)."""
import ipaddress
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = 'CatalogBot/1.0'
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
_DNS_CACHE = {}
_DNS_TTL = 300


class UnsafeUrl(Exception):
    pass


def _is_public(ip):
    a = ipaddress.ip_address(ip)
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified
                or (a.version == 4 and a in ipaddress.ip_network('100.64.0.0/10')))


def check_url(url):
    """Valida esquema, credenciais, porta, host e TODAS as resoluções DNS. Lança UnsafeUrl."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ('http', 'https'):
        raise UnsafeUrl(f'esquema não permitido: {u.scheme}')
    if u.username or u.password:
        raise UnsafeUrl('credenciais no URL')
    host = (u.hostname or '').lower()
    if not host or host == 'localhost' or host.endswith(('.local', '.internal', '.localhost', '.lan')):
        raise UnsafeUrl('host não permitido')
    try:
        port = u.port
    except ValueError:
        raise UnsafeUrl('porta inválida')
    if port not in ALLOWED_PORTS:
        raise UnsafeUrl(f'porta não permitida: {port}')
    try:
        ipaddress.ip_address(host)
        raise UnsafeUrl('IP literal não permitido')
    except ValueError:
        pass
    now = time.time()
    hit = _DNS_CACHE.get(host)
    if hit and now - hit[0] < _DNS_TTL:
        ok = hit[1]
    else:
        try:
            infos = socket.getaddrinfo(host, port or (443 if u.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise UnsafeUrl(f'DNS falhou: {e}')
        ips = {i[4][0] for i in infos}
        ok = bool(ips) and all(_is_public(ip) for ip in ips)
        _DNS_CACHE[host] = (now, ok)
    if not ok:
        raise UnsafeUrl('o host resolve para endereço privado/reservado')
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch_bytes(url, max_bytes=5 * 1024 * 1024, timeout=15, accept_prefix=('image/',), max_redirects=4, referer=None):
    """GET com validação SSRF em cada salto, limite de tamanho e de content-type. Devolve (bytes, content_type)."""
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(max_redirects + 1):
        check_url(current)
        headers = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
        if referer:
            headers['Referer'] = referer
        req = urllib.request.Request(current, headers=headers)
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get('Location'):
                current = urllib.parse.urljoin(current, e.headers['Location'])
                continue
            raise
        ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if accept_prefix and not any(ctype.startswith(p) for p in accept_prefix):
            raise UnsafeUrl(f'content-type não aceite: {ctype or "(vazio)"}')
        length = resp.headers.get('Content-Length')
        if length and length.isdigit() and int(length) > max_bytes:
            raise UnsafeUrl('resposta acima do limite de tamanho')
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise UnsafeUrl('resposta acima do limite de tamanho')
        return data, ctype
    raise UnsafeUrl('demasiados redirects')
