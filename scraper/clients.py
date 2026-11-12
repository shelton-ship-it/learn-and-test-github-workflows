"""Clientes HTTP do worker (D1) e da API (Turso). Só variáveis/secrets já existentes."""
import json
import os
import time
import urllib.error
import urllib.request


# A Cloudflare bloqueia (403, código 1010) o User-Agent por omissão do urllib ("Python-urllib/x"); sem isto o worker recusa o scraper.
UA = 'Mozilla/5.0 (compatible; HDtorrentCatalog/1.0)'


def _call(method, url, key_header, key, payload=None, timeout=60, retries=4, retry_on=(429,)):
    data = json.dumps(payload).encode() if payload is not None else None
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={'Content-Type': 'application/json', 'User-Agent': UA, key_header: key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:300]
            last = f'HTTP {e.code} {body}'
            if e.code < 500 and e.code not in retry_on:
                raise RuntimeError(last)
        except Exception as e:  # rede/timeout
            last = str(e)
        if attempt < retries:
            time.sleep([3, 8, 20, 40][min(attempt - 1, 3)])
    raise RuntimeError(f'{method} {url} falhou: {last}')


class WorkerClient:
    """Worker Cloudflare (D1). A chave é o ADMIN_API_KEY (mesmo valor do WORKER_API_KEY)."""

    def __init__(self, base_url, key):
        if not base_url:
            raise RuntimeError('worker_url ausente nos parâmetros do job')
        self.base = base_url.rstrip('/')
        self.key = key

    def _p(self, path, payload):
        return _call('POST', self.base + path, 'x-api-key', self.key, payload)

    def filter(self, source_id, job_id, items):
        return self._p('/seen/filter', {'source_id': source_id, 'job_id': job_id, 'items': items})

    def resolve(self, source_id, items):
        return self._p('/seen/resolve', {'source_id': source_id, 'items': items})['items']

    def commit(self, source_id, job_id, items):
        return self._p('/seen/commit', {'source_id': source_id, 'job_id': job_id, 'items': items})

    def progress(self, job_id, checkpoint, counts):
        return self._p(f'/jobs/{job_id}/progress', {'checkpoint': checkpoint, 'counts': counts})

    def finish(self, job_id, status, counts, checkpoint=None, error=None, sample=None):
        body = {'status': status, 'counts': counts}
        if checkpoint is not None:
            body['checkpoint'] = checkpoint
        if error:
            body['error'] = error
        if sample is not None:
            body['sample'] = sample          # só nas sondas (dry_run)
        return self._p(f'/jobs/{job_id}/finish', body)


class ApiClient:
    """API do catálogo (EdgeOne -> Turso). Contrato existente: POST /api/pipeline/register com X-API-Key."""

    def __init__(self):
        self.base = (os.environ.get('PIPELINE_API') or '').rstrip('/')
        self.key = os.environ.get('ADMIN_API_KEY', '')
        if not self.base or not self.key:
            raise RuntimeError('PIPELINE_API / ADMIN_API_KEY não configurados')

    def register_featured(self, source_id, items):
        """Ordem do carrossel de destaque do site de origem (a API substitui a anterior desta fonte)."""
        return _call('POST', f'{self.base}/api/pipeline/featured', 'X-API-Key', self.key, {'source_id': source_id, 'items': items},
                     timeout=60, retries=3)

    def register(self, entries):
        return _call('POST', f'{self.base}/api/pipeline/register', 'X-API-Key', self.key, {'entries': entries},
                     timeout=90, retries=5)
