"""Crawler Scrapy: descoberta de listagens + páginas de detalhe, com validação "já capturado?" no D1 (via worker),
registo no catálogo (API/Turso), thumbnails pelo mecanismo existente (shards GitHub + jsDelivr) e checkpoints retomáveis.

Fluxo por lote (~100 itens, sempre no fim de uma página de listagem):
  worker /seen/resolve  -> normaliza magnets (normalizeMagnet), decide catalog_id, hashes já conhecidos
  thumbnails            -> reutiliza a existente; senão descarrega, valida e publica (falha não invalida o item)
  API /api/pipeline/register -> upsert idempotente no Turso
  worker /seen/commit   -> marca como processado (+ hashes); só então o checkpoint avança

Sonda (options.dry_run): corre o mesmo crawl mas NÃO chama /seen/filter nem /seen/commit, não descarrega nem publica
thumbnails, não regista na API e não avança o checkpoint. Só usa /seen/resolve (leitura) para normalizar magnets e
ver o que já é conhecido, e devolve ao worker uma amostra (páginas, itens, diagnóstico, sugestões).
"""
import hashlib
import io
import json
import os
import re
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import scrapy
from parsel import Selector
from PIL import Image
from scrapy.exceptions import CloseSpider, IgnoreRequest

from . import netsafe
from .adapters import get_adapter
from .clients import ApiClient, WorkerClient
from .storage import StorageUnavailable, ThumbStore

BATCH_ITEMS = 100
DEADLINE_S = 5 * 3600 + 10 * 60          # o workflow tem timeout de 345 min: termina com folga e devolve checkpoint
HEARTBEAT_S = 60
MAX_API_FAILURES = 5                     # lotes consecutivos que falham o registo na API (cada um já tenta 5x com espera) antes de abortar
IMG_EXT = {'JPEG': ('jpg', 'image/jpeg'), 'PNG': ('png', 'image/png'), 'WEBP': ('webp', 'image/webp'), 'GIF': ('gif', 'image/gif')}
THUMB_DIR = '/tmp/thumbs'
SAMPLE_MAX_BYTES = 250_000               # o worker aceita até 300 000
SAMPLE_PAGE_EXAMPLES = 5
SAMPLE_MAX_FAILURES = 30


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class Job:
    def __init__(self, params, job_id):
        self.job_id = job_id
        self.run_type = params['run_type']
        self.reconcile = bool(params.get('reconcile'))
        self.opts = params['options']
        self.src = params['source']
        self.cfg = self.opts.get('config') or {}
        self.checkpoint = params.get('checkpoint') or None
        self.started = time.time()
        self.deadline = self.started + DEADLINE_S
        self.worker = WorkerClient(params.get('worker_url'), os.environ.get('ADMIN_API_KEY', ''))
        self.dry = bool(self.opts.get('dry_run'))
        self.api = None if self.dry else ApiClient()
        self.store = None if self.dry else ThumbStore(job_id)
        self.sample = ({'mode': 'configurado' if self.cfg.get('link_selector') else 'automatico', 'playwright': bool(self.opts.get('playwright')),
                        'pages': [], 'items': [], 'failures': [], 'diagnostics': {}, 'summary': {}, 'hints': []} if self.dry else None)
        self.counts = {'discovered': 0, 'new': 0, 'duplicates': 0, 'failures': 0, 'pages': 0, 'skipped': 0}
        self.buffer = []
        self.failed_direct = []
        self.candidate_cp = None
        self.committed_cp = self.checkpoint
        self.fatal = None
        self.job_keys = set()          # entradas já vistas NESTE job (menus/rodapé repetem-se em todas as páginas de listagem)
        self.cp_frozen = False         # lote falhou na API: o checkpoint deixa de avançar para o retomar daí
        self.api_batches_failed = 0
        self.api_failures = 0
        self.last_hb = 0
        self.cancelled = False

    # ── progresso ────────────────────────────────────────────────────────
    def heartbeat(self):
        self.last_hb = time.time()
        try:
            r = self.worker.progress(self.job_id, self.committed_cp, self.counts)
            if not r.get('continue', True):
                self.cancelled = True
        except Exception as e:
            print(f'[heartbeat] falhou (não fatal): {e}')

    # ── thumbnails ───────────────────────────────────────────────────────
    def _download_thumb(self, key, url, referer):
        try:
            data, _ = netsafe.fetch_bytes(url, referer=referer)
            if len(data) < 2000:
                return None, 'imagem demasiado pequena'
            im = Image.open(io.BytesIO(data))
            fmt, (w, h) = im.format, im.size
            im.verify()
            if fmt not in IMG_EXT:
                return None, f'formato não suportado: {fmt}'
            ext, mime = IMG_EXT[fmt]
            name = f"t_{hashlib.sha1(key.encode()).hexdigest()[:16]}.{ext}"
            os.makedirs(THUMB_DIR, exist_ok=True)
            path = os.path.join(THUMB_DIR, name)
            with open(path, 'wb') as f:
                f.write(data)
            return {'path': path, 'name': name, 'mime': mime, 'width': w, 'height': h}, None
        except Exception as e:
            return None, str(e)[:200]

    def _prepare_thumbs(self, ready):
        """Preenche rec['thumbnail'] (ou None). Nunca lança: falha de imagem não invalida o item."""
        todo = []
        for b, r in ready:
            b['thumbnail'] = None
            if r.get('existing_thumb_url'):
                b['thumbnail'] = {'source_url': b.get('thumb') or r['existing_thumb_url'], 'public_url': r['existing_thumb_url'],
                                  'status': 'stored', 'storage_key': None, 'mime': None, 'width': None, 'height': None, 'error': None}
            elif b.get('thumb'):
                todo.append(b)
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda b: self._download_thumb(b['key'], b['thumb'], b['detail_url']), todo))
        files, owners = [], {}
        for b, (info, err) in zip(todo, results):
            if info is None:
                b['thumbnail'] = {'source_url': b['thumb'], 'status': 'failed', 'error': err, 'public_url': None, 'storage_key': None,
                                  'mime': None, 'width': None, 'height': None}
            else:
                files.append((info['path'], info['name']))
                owners[info['name']] = (b, info)
        if not files:
            return
        try:
            if not self.store.configured():
                raise StorageUnavailable('credenciais de storage não configuradas')
            published = self.store.publish(files)
            for name, (b, info) in owners.items():
                p = published[name]
                b['thumbnail'] = {'source_url': b['thumb'], 'status': 'stored', 'error': None, 'public_url': p['public_url'],
                                  'storage_key': p['storage_key'], 'mime': info['mime'], 'width': info['width'], 'height': info['height']}
        except Exception as e:
            msg = f'armazenamento falhou: {str(e)[:150]}'
            print(f'[thumbs] {msg}')
            for name, (b, info) in owners.items():
                b['thumbnail'] = {'source_url': b['thumb'], 'status': 'failed', 'error': msg, 'public_url': None, 'storage_key': None,
                                  'mime': info['mime'], 'width': info['width'], 'height': info['height']}
        finally:
            for path, _ in files:
                try:
                    os.remove(path)
                except OSError:
                    pass

    # ── lote ─────────────────────────────────────────────────────────────
    def fail_direct(self, entry_key, detail_url, reason):
        print(f'[item] falhou {detail_url}: {reason}')
        self.counts['failures'] += 1
        if self.dry:
            if len(self.sample['failures']) < SAMPLE_MAX_FAILURES:
                self.sample['failures'].append({'detail_url': detail_url, 'reason': str(reason)[:200]})
            return
        self.failed_direct.append({'content_key': entry_key, 'detail_url': detail_url, 'state': 'failed'})

    def _flush_probe(self):
        """Sonda: normaliza (leitura) e guarda a amostra. Nada é escrito no D1, na API nem no storage."""
        sid = self.src['id']
        batch, self.buffer = self.buffer, []
        self.failed_direct = []
        res = {}
        for part in chunks(batch, 50):
            for r in self.worker.resolve(sid, [{'content_key': b['key'], 'magnets': b['magnets']} for b in part]):
                res[r['content_key']] = r
        for b in batch:
            r = res.get(b['key']) or {}
            mags = r.get('magnets') or []
            md = b['md']
            prov = md.get('provenance') or {}
            title = md.get('title') or b.get('title') or (mags[0].get('display_name') if mags else None)
            title_src = prov.get('title') or ('listing' if b.get('title') else ('magnet-dn' if title else None))
            problems = []
            if not mags:
                problems.append('nenhum magnet válido' + (f" ({len(r.get('invalid_magnets') or [])} inválido(s))" if r.get('invalid_magnets') else ''))
            if not title:
                problems.append('sem título')
            if not b.get('thumb'):
                problems.append('sem miniatura')
            if not md.get('description'):
                problems.append('sem descrição')
            if md.get('year') is None:
                problems.append('sem ano')
            if mags and not any(b['labels'].get(m['original']) for m in mags):
                problems.append('magnets sem rótulo (qualidade/áudio não serão inferidos)')
            if len(mags) > 10:
                problems.append(f'{len(mags)} magnets: possível mistura com blocos relacionados')
            if not mags or not title:
                self.counts['failures'] += 1
            elif r.get('is_new_item'):
                self.counts['new'] += 1
            else:
                self.counts['duplicates'] += 1
            self.sample['items'].append({
                'detail_url': b['detail_url'], 'title': (title or '')[:300] or None, 'title_source': title_src,
                'year': md.get('year'), 'year_source': prov.get('year'),
                'description': (md.get('description') or '')[:300] or None, 'description_source': prov.get('description'),
                'thumb': b.get('thumb'), 'subcategory': md.get('subcategory'), 'published_at': md.get('published_at'),
                'terms': (md.get('terms') or [])[:20], 'directors': (md.get('directors') or [])[:10], 'cast': (md.get('cast') or [])[:20],
                'tags': (md.get('tags') or [])[:15], 'fields': md.get('fields') or {},
                'magnets': [{'label': b['labels'].get(m['original']), 'info_hash': m.get('info_hash'), 'display_name': (m.get('display_name') or '')[:200] or None,
                             'trackers': len(m.get('trackers') or []), 'normalization': m.get('normalization'), 'changes': m.get('changes'),
                             'already_known': bool(m.get('already_known')), 'normalized': (m.get('normalized') or '')[:400]} for m in mags[:8]],
                'magnets_total': len(mags), 'invalid_magnets': (r.get('invalid_magnets') or [])[:5], 'problems': problems,
            })
        self.heartbeat()

    def flush(self):
        if self.dry:
            return self._flush_probe()
        sid = self.src['id']
        batch, self.buffer = self.buffer, []
        commits = list(self.failed_direct)
        self.failed_direct = []

        res = {}
        for part in chunks(batch, 50):
            for r in self.worker.resolve(sid, [{'content_key': b['key'], 'magnets': b['magnets']} for b in part]):
                res[r['content_key']] = r

        def fail(b, why):
            print(f"[item] falhou {b['detail_url']}: {why}")
            self.counts['failures'] += 1
            commits.append({'content_key': b['key'], 'detail_url': b['detail_url'], 'state': 'failed'})

        ready = []
        for b in batch:
            r = res.get(b['key'])
            if not r or not r['magnets']:
                fail(b, 'nenhum magnet válido' + (f" ({len(r['invalid_magnets'])} inválido(s))" if r and r['invalid_magnets'] else ''))
                continue
            title = b['md'].get('title') or b.get('title') or r['magnets'][0].get('display_name')
            if not title:
                fail(b, 'sem título')
                continue
            if not b['md'].get('title'):
                b['md']['provenance']['title'] = 'listing' if b.get('title') else 'magnet-dn'
            b['final_title'] = title[:300]
            ready.append((b, r))

        if ready:
            self._prepare_thumbs(ready)
            entries = []
            for b, r in ready:
                md = b['md']
                entries.append({
                    'item': {'id': r['catalog_id'], 'title': b['final_title'], 'type': self.src['type'], 'category': self.src['category'],
                             'subcategory': md.get('subcategory'), 'description': (md.get('description') or '')[:4000] or None,
                             'tags': md.get('tags') or [], 'year': md.get('year'), 'terms': md.get('terms') or [],
                             'people': {'directors': md.get('directors') or [], 'cast': md.get('cast') or []},
                             'published_at': md.get('published_at'),
                             'metadata': {'fields': md.get('fields') or {}, 'provenance': md.get('provenance') or {}}},
                    'magnets': [{'original': m['original'], 'normalized': m['normalized'], 'info_hash': m['info_hash'],
                                 'display_name': m.get('display_name'), 'trackers': m.get('trackers') or [],
                                 'label': b['labels'].get(m['original'])} for m in r['magnets']],
                    'thumbnail': b.get('thumbnail'),
                    'provenance': {'source_id': sid, 'source_host': self.src['host'], 'category_url': b['section_url'],
                                   'detail_url': b['detail_url'], 'job_id': self.job_id, 'extracted_at': int(time.time() * 1000)},
                })
            errors, infra = {}, set()
            try:
                out = self.api.register(entries)
                self.api_failures = 0
                errors = {e['id']: e.get('error', 'erro') for e in (out.get('errors') or [])}
            except Exception as e:
                # Falha de infraestrutura (API/Turso/EdgeOne): NÃO é culpa dos itens. Não os marcamos como falhados no D1
                # (o worker desiste de um item após 3 falhas) e congelamos o checkpoint para retomar deste ponto.
                self.api_failures += 1
                self.api_batches_failed += 1
                self.cp_frozen = True
                infra = {en['item']['id'] for en in entries}
                errors = {i: str(e)[:200] for i in infra}
                print(f'[api] registo falhou ({self.api_failures}/{MAX_API_FAILURES}): {str(e)[:200]}')
                code = next((c for c in (401, 403, 404) if f'HTTP {c}' in str(e)), None)
                if code:      # erro de configuração, não transitório: não adianta insistir
                    self.fatal = (f'API do catálogo respondeu HTTP {code} em {self.api.base}/api/pipeline/register — '
                                  'confira o secret PIPELINE_API (sem /api no fim), a chave ADMIN_API_KEY e se a pixel_service_v1 está publicada')
                elif self.api_failures >= MAX_API_FAILURES:
                    self.fatal = f'API do catálogo indisponível: {str(e)[:200]}'
            for b, r in ready:
                if r['catalog_id'] in errors:
                    self.counts['failures'] += 1
                    if r['catalog_id'] not in infra:      # erro do próprio item devolvido pela API: conta como falha dele
                        commits.append({'content_key': b['key'], 'detail_url': b['detail_url'], 'state': 'failed'})
                    continue
                th = b.get('thumbnail') or {}
                commits.append({'content_key': b['key'], 'detail_url': b['detail_url'], 'state': 'processed', 'catalog_id': r['catalog_id'],
                                'thumb_url': th.get('public_url') if th.get('status') == 'stored' else None,
                                'title': b['final_title'], 'info_hashes': [m['info_hash'] for m in r['magnets']]})
                if r['is_new_item']:
                    self.counts['new'] += 1
                else:
                    self.counts['duplicates'] += 1

        for part in chunks(commits, 50):
            self.worker.commit(sid, self.job_id, part)
        if not self.fatal and not self.cp_frozen:
            self.committed_cp = self.candidate_cp if self.candidate_cp is not None else self.committed_cp
        self.heartbeat()


_DL_RE = re.compile(r'download|baix|magnet|torrent', re.I)


def diagnose_page(response, kind):
    """Sonda: números que mostram o que o HTML recebido contém (sem guardar o HTML). kind = 'listing' | 'detail'."""
    sel = Selector(text=response.text)
    host = urllib.parse.urlsplit(response.url).netloc.lower().removeprefix('www.')
    depth, same, examples = Counter(), 0, []
    for a in sel.css('a[href]'):
        href = (a.attrib.get('href') or '').strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:')):
            continue
        text = re.sub(r'\s+', ' ', ' '.join(a.css('::text').getall())).strip()
        if href.lower().startswith('magnet:'):
            if kind == 'detail' and len(examples) < 5:
                examples.append({'text': text[:60], 'href': 'magnet:…'})
            continue
        u = urllib.parse.urlsplit(urllib.parse.urljoin(response.url, href))
        is_same = u.netloc.lower().removeprefix('www.') == host
        if is_same:
            same += 1
            depth[str(len([x for x in u.path.split('/') if x]))] += 1
        if kind == 'detail' and len(examples) < 5 and _DL_RE.search(text):
            examples.append({'text': text[:60], 'href': ((u.path + ('?…' if u.query else ''))[:100]) if is_same else u.netloc})
    low = response.text.lower()
    visible = re.sub(r'\s+', ' ', ' '.join(sel.xpath('//body//text()[not(ancestor::script) and not(ancestor::style)]').getall())).strip()
    d = {
        'url': response.url, 'status': response.status, 'bytes': len(response.body),
        'content_type': (response.headers.get('Content-Type') or b'').decode('latin-1')[:80],
        'title': (sel.css('title::text').get() or '').strip()[:120],
        'anchors': len(sel.css('a[href]')), 'anchors_same_host': same,
        'path_depth': dict(sorted(depth.items(), key=lambda kv: -kv[1])[:6]),
        'scripts': len(sel.css('script')), 'images': len(sel.css('img')), 'visible_text_chars': len(visible),
        'rel_next': bool(sel.css('a[rel~="next"], link[rel="next"]')),
        'magnet_occurrences': low.count('magnet:') + low.count('magnet%3a'), 'btih_occurrences': low.count('btih'),
    }
    if kind == 'detail':
        d.update({'h1': len(sel.css('h1')), 'json_ld_blocks': len(sel.css('script[type="application/ld+json"]')),
                  'og_title': bool(sel.css('meta[property="og:title"]')), 'download_like_links': examples})
    return d


def _js_like(d):
    return bool(d) and d.get('scripts', 0) >= 3 and (d.get('anchors', 0) < 15 or d.get('visible_text_chars', 0) < 400)


_PROBLEM_TIPS = {
    'sem miniatura': 'defina detail.thumb (ou thumb_selector com item_selector) se o site não usa og:image/JSON-LD',
    'sem descrição': 'defina detail.description',
    'sem ano': 'defina detail.year (o ano só é inferido do título quando lá aparece)',
    'magnets sem rótulo (qualidade/áudio não serão inferidos)': 'aponte detail.magnet_selector ao botão (ou ao contentor dos botões) de download: o texto do botão serve de rótulo',
}


def finalize_sample(job, spider):
    """Sonda: resumo + sugestões + corte de tamanho. Só devolve dados observados; as sugestões são heurísticas."""
    sm = job.sample
    items, pages, d = sm['items'], sm['pages'], sm['diagnostics']
    sm['stop_reason'], sm['finished'] = spider.stop_reason, spider.finished
    for p in pages:
        p.pop('_keys', None)

    n = len(items)
    hashes = Counter(m['info_hash'] for it in items for m in it['magnets'] if m.get('info_hash'))
    sm['summary'] = {
        'itens_extraidos': n, 'com_titulo': sum(1 for i in items if i['title']), 'com_miniatura': sum(1 for i in items if i['thumb']),
        'com_descricao': sum(1 for i in items if i['description']), 'com_ano': sum(1 for i in items if i['year'] is not None),
        'com_generos': sum(1 for i in items if i['terms']), 'com_realizador': sum(1 for i in items if i['directors']),
        'com_elenco': sum(1 for i in items if i['cast']), 'com_magnet': sum(1 for i in items if i['magnets_total']),
        'magnets_total': sum(i['magnets_total'] for i in items),
        'magnets_com_rotulo': sum(1 for i in items for m in i['magnets'] if m.get('label')),
        'magnets_ja_conhecidos': sum(1 for i in items for m in i['magnets'] if m.get('already_known')),
        'fontes_do_titulo': dict(Counter(i['title_source'] or 'nenhuma' for i in items)),
        'paginas_lidas': len(pages), 'motivo_de_paragem': spider.stop_reason or ('fim' if spider.finished else '—'),
    }

    hints, L, D = [], d.get('listing'), d.get('detail_sem_magnet') or d.get('detail')
    pw = sm['playwright']
    if pages and pages[0]['entries'] == 0 and L:
        if L['status'] >= 400:
            hints.append(f"A listagem respondeu HTTP {L['status']}: o site pode bloquear o acesso automático ou o URL não existe.")
        elif _js_like(L) and not pw:
            hints.append('Poucas ligações/texto no HTML recebido: a página parece desenhada por JavaScript. Experimente options.playwright=true (e config.wait_selector).')
        elif sm['mode'] == 'automatico':
            hints.append(f"O modo automático não reconheceu cartões (precisa de várias ligações repetidas de um só segmento). Profundidade dos caminhos na página: {L['path_depth']}. Defina config.link_selector (e item_selector).")
        else:
            hints.append('O link_selector configurado não apanhou nenhuma ligação nesta página.')
    if pages and pages[0]['entries'] and len(pages) == 1 and pages[0]['next'] is None and pages[0].get('stop') is None:
        hints.append('Não foi detetada página seguinte. Defina config.next_selector ou use {n} no URL da secção (ex.: https://…/page/{n}/).')
    if len(pages) >= 2:
        a, b = set(job._page_keys[0]), set(job._page_keys[1])
        if b and len(a & b) / len(b) > 0.5:
            hints.append('A página 2 repete mais de metade das entradas da página 1: a paginação detetada não avança. Verifique next_selector.')
    if n and sm['summary']['com_magnet'] < n:
        miss = n - sm['summary']['com_magnet']
        if D and D['magnet_occurrences'] == 0 and D['btih_occurrences'] == 0 and _js_like(D) and not pw:
            hints.append('Os detalhes não têm nenhum magnet no HTML e parecem desenhados por JavaScript. Experimente options.playwright=true.')
        else:
            extra = f" Ligações tipo download vistas no detalhe: {D['download_like_links']}." if D and D.get('download_like_links') else ''
            hints.append(f'{miss} de {n} itens sem magnet válido. Confirme detail.magnet_selector.{extra}')
    if any(c > 1 for c in hashes.values()):
        hints.append('Há magnets repetidos em itens diferentes: provável bloco de "relacionados". Defina detail.magnet_selector (numa execução real o dedup juntaria estes itens).')
    if n >= 3:
        counted = Counter(pr for it in items for pr in it['problems'] if pr in _PROBLEM_TIPS)
        for pr, c in counted.most_common():
            if c * 2 >= n:
                hints.append(f'{c} de {n} itens: {pr} — {_PROBLEM_TIPS[pr]}.')
        if not any(i['terms'] or i['directors'] or i['cast'] for i in items):
            hints.append('Nenhum item com géneros/realizador/elenco. O modo automático só reconhece /genero/x/, /diretor/y/ e /atores/z/: se o site usa outro padrão, defina detail.terms/directors/cast.')
    sm['hints'] = hints

    while items and len(json.dumps(sm, ensure_ascii=False)) > SAMPLE_MAX_BYTES:
        items.pop()
        sm['truncated_items'] = True
    if sm.get('truncated_items'):
        hints.append('A amostra foi reduzida para caber no limite; reduza max_items da sonda para ver todos os itens.')
    return sm


class SafeMiddleware:
    """SSRF: valida cada pedido (incluindo redirects) e só aceita respostas de texto."""

    def process_request(self, request, spider=None):
        try:
            netsafe.check_url(request.url)
        except netsafe.UnsafeUrl as e:
            raise IgnoreRequest(f'SSRF: {e}')
        return None

    def process_response(self, request, response, spider=None):
        ct = (response.headers.get('Content-Type') or b'').decode('latin-1').lower()
        if ct and not any(t in ct for t in ('html', 'xml', 'json', 'text')):
            raise IgnoreRequest(f'content-type não aceite: {ct}')
        return response


class CatalogSpider(scrapy.Spider):
    name = 'catalog'

    def __init__(self, job, adapter, **kw):
        super().__init__(**kw)
        self.job = job
        self.ad = adapter
        self.sections = []
        self.visited = set()
        self.stop_reason = None
        self.finished = False
        self.items_taken = 0
        self.first_page_seen = False
        self.job._page_keys = []

    # ── pedidos ──────────────────────────────────────────────────────────
    def _req(self, url, cb, **kw):
        meta = kw.pop('meta', {})
        if self.job.opts.get('playwright'):
            meta['playwright'] = True
            if self.job.cfg.get('wait_selector'):
                from scrapy_playwright.page import PageMethod
                meta['playwright_page_methods'] = [PageMethod('wait_for_selector', self.job.cfg['wait_selector'], timeout=20000)]
        return scrapy.Request(url, callback=cb, meta=meta, errback=kw.pop('errback', None), **kw)

    def _listing_request(self, idx, url, n, streak):
        if url is None:
            section = self.sections[idx]
            url = section.replace('{n}', str(n)) if '{n}' in section else section
        return self._req(url, self.parse_listing, errback=self.listing_error, cb_kwargs={'idx': idx, 'n': n, 'streak': streak})

    # Scrapy >= 2.13 usa start() assíncrono; versões anteriores usam start_requests().
    async def start(self):
        for r in self._initial():
            yield r

    def start_requests(self):
        yield from self._initial()

    def _initial(self):
        if self.job.cfg.get('discover_sections'):
            yield self._req(self.job.src['url'], self.on_sections, errback=self.listing_error)
        else:
            self.sections = [self.job.src['url']] + list(self.job.opts.get('sections') or [])
            yield from self._begin()

    def on_sections(self, response):
        found = self.ad.discover_sections(response)
        self.sections = list(dict.fromkeys([self.job.src['url']] + found + list(self.job.opts.get('sections') or [])))[:50]
        print(f'[sections] {len(self.sections)} secção(ões)')
        yield from self._begin()

    def _begin(self):
        cp = self.job.checkpoint or {}
        idx = int(cp.get('section', 0))
        if idx >= len(self.sections):
            self.finished = True
            return
        yield self._listing_request(idx, cp.get('url'), int(cp.get('n', 1)), 0)

    def listing_error(self, failure):
        self.job.fatal = f'falha ao obter listagem {failure.request.url}: {failure.getErrorMessage()[:200]}'
        raise CloseSpider('listing_error')

    # ── listagem ─────────────────────────────────────────────────────────
    def parse_listing(self, response, idx, n, streak):
        j = self.job
        self.visited.add(response.url)
        entries = self.ad.list_entries(response)
        page = None
        if j.dry:
            try:
                if 'listing' not in j.sample['diagnostics']:
                    j.sample['diagnostics']['listing'] = diagnose_page(response, 'listing')
            except Exception as e:
                print(f'[probe] diagnóstico da listagem falhou: {e}')
            page = {'url': response.url, 'entries': len(entries), 'next': None, 'stop': None,
                    'sample': [{'title': e.title, 'url': e.url, 'thumb': e.thumb} for e in entries[:SAMPLE_PAGE_EXAMPLES]]}
            j.sample['pages'].append(page)
            j._page_keys.append([e.key for e in entries])
        if not entries:
            if not self.first_page_seen and j.counts['discovered'] == 0:
                j.fatal = f'nenhuma entrada encontrada em {response.url} — verifique options.config.link_selector'
                raise CloseSpider('no_entries')
            yield from self._finish_page({'remaining': 0, 'resume': {'section': idx + 1, 'url': None, 'n': 1}, 'stop': None, 'streak': 0})
            return
        self.first_page_seen = True

        fresh = [e for e in entries if e.key not in j.job_keys]
        j.job_keys.update(e.key for e in fresh)
        entries = fresh                       # o que já apareceu numa página anterior deste job não é descarregado de novo

        if j.dry or not entries:
            unseen = list(entries)        # sonda: não consulta nem grava o "já capturado?" (/seen/filter escreve no D1)
        else:
            res = j.worker.filter(j.src['id'], j.job_id, [{'key': e.key, 'url': e.url} for e in entries])
            unseen_keys = set(res['unseen'])
            unseen = [e for e in entries if e.key in unseen_keys]
        j.counts['discovered'] += len(entries)
        j.counts['skipped'] += len(entries) - len(unseen)

        room = j.opts['max_items'] - self.items_taken
        todo = unseen[:max(room, 0)]
        self.items_taken += len(todo)
        stop = 'max_items' if len(unseen) > len(todo) or self.items_taken >= j.opts['max_items'] else None

        new_streak = 0 if unseen else streak + 1
        section_done = False
        resume = None
        early = j.run_type != 'initial' and not j.reconcile and new_streak >= j.opts.get('early_stop_pages', 2)
        if not early:
            section = self.sections[idx]
            nxt = section.replace('{n}', str(n + 1)) if '{n}' in section else self.ad.next_page(response)
            if nxt and nxt not in self.visited:
                resume = {'section': idx, 'url': nxt, 'n': n + 1}
        if resume is None:
            section_done = True
            resume = {'section': idx + 1, 'url': None, 'n': 1}
        if not stop and j.counts['pages'] + 1 >= j.opts['max_pages']:
            stop = 'max_pages'

        if page is not None:
            page['next'] = resume['url'] if resume['section'] == idx else None
            page['stop'] = stop
        st = {'remaining': 0, 'resume': resume, 'stop': stop, 'streak': 0 if section_done else new_streak, 'idx': idx}
        if not j.opts.get('extract_details', True):
            for e in todo:
                self._buffer(e, e.url, self.ad.parse_inline(response, e), idx)
            yield from self._finish_page(st)
            return
        if not todo:
            yield from self._finish_page(st)
            return
        st['remaining'] = len(todo)
        for e in todo:
            yield self._req(e.url, self.parse_detail, errback=self.detail_error, dont_filter=True, cb_kwargs={'entry': e, 'st': st})

    # ── detalhe ──────────────────────────────────────────────────────────
    def _buffer(self, entry, detail_url, rec, idx):
        self.job.buffer.append({'key': entry.key, 'detail_url': detail_url, 'section_url': self.sections[idx], 'magnets': rec['magnets'],
                                'md': rec['metadata'], 'labels': rec.get('labels') or {}, 'thumb': rec['metadata'].get('thumb') or entry.thumb, 'title': entry.title})

    def parse_detail(self, response, entry, st):
        if self.job.dry and 'detail' not in self.job.sample['diagnostics']:
            try:
                self.job.sample['diagnostics']['detail'] = diagnose_page(response, 'detail')
            except Exception as e:
                print(f'[probe] diagnóstico do detalhe falhou: {e}')
        try:
            rec = self.ad.parse_detail(response, entry)
            if self.job.dry and not rec['magnets'] and 'detail_sem_magnet' not in self.job.sample['diagnostics']:
                try:
                    self.job.sample['diagnostics']['detail_sem_magnet'] = diagnose_page(response, 'detail')
                except Exception as e:
                    print(f'[probe] diagnóstico do detalhe sem magnet falhou: {e}')
            self._buffer(entry, response.url, rec, st['idx'])
        except Exception as e:
            self.job.fail_direct(entry.key, entry.url, f'parse: {e}')
        st['remaining'] -= 1
        if st['remaining'] == 0:
            yield from self._finish_page(st)

    def detail_error(self, failure):
        kw = failure.request.cb_kwargs
        self.job.fail_direct(kw['entry'].key, kw['entry'].url, failure.getErrorMessage()[:120])
        kw['st']['remaining'] -= 1
        if kw['st']['remaining'] == 0:
            yield from self._finish_page(kw['st'])

    # ── fim de página: flush, checkpoint, limites ───────────────────────
    def _finish_page(self, st):
        j = self.job
        j.counts['pages'] += 1
        reason = st.get('stop')
        if not reason and time.time() > j.deadline:
            reason = 'deadline'
        if not reason and j.cancelled:
            reason = 'cancelled'
        resume = st['resume']
        j.candidate_cp = resume
        all_done = resume['section'] >= len(self.sections)
        if len(j.buffer) >= BATCH_ITEMS or j.failed_direct and len(j.failed_direct) >= BATCH_ITEMS or reason or all_done:
            j.flush()
        elif time.time() - j.last_hb > HEARTBEAT_S:
            j.heartbeat()
        if j.fatal:
            raise CloseSpider('fatal')
        if j.cancelled and not reason:
            reason = 'cancelled'
        if reason:
            self.stop_reason = reason
            return
        if all_done:
            self.finished = True
            return
        yield self._listing_request(resume['section'], resume['url'], resume['n'], st.get('streak', 0))

    def closed(self, reason):
        try:
            if self.job.buffer or self.job.failed_direct:
                self.job.flush()
        except Exception as e:
            print(f'[closed] flush final falhou: {e}')
            self.job.fatal = self.job.fatal or f'flush final falhou: {str(e)[:200]}'


def _unsafe_literal(url):
    from urllib.parse import urlsplit
    try:
        netsafe.check_url(url)
        return False
    except Exception:
        return True


def build_settings(job):
    delay = job.opts.get('delay_ms', 1000) / 1000.0
    s = {
        'USER_AGENT': netsafe.USER_AGENT,
        'ROBOTSTXT_OBEY': bool(job.cfg.get('obey_robots', True)),
        'DOWNLOAD_DELAY': delay,
        'RANDOMIZE_DOWNLOAD_DELAY': True,
        'CONCURRENT_REQUESTS': 4,
        'CONCURRENT_REQUESTS_PER_DOMAIN': 2,
        'AUTOTHROTTLE_ENABLED': True,
        'AUTOTHROTTLE_START_DELAY': delay,
        'AUTOTHROTTLE_MAX_DELAY': 30,
        'DOWNLOAD_TIMEOUT': 30,
        'DOWNLOAD_MAXSIZE': 5 * 1024 * 1024,
        'RETRY_TIMES': 2,
        'REDIRECT_MAX_TIMES': 5,
        'COOKIES_ENABLED': False,
        'TELNETCONSOLE_ENABLED': False,
        'LOG_LEVEL': 'INFO',
        'DOWNLOADER_MIDDLEWARES': {'scraper.crawler.SafeMiddleware': 50},
        'REQUEST_FINGERPRINTER_IMPLEMENTATION': '2.7',
    }
    if job.opts.get('playwright'):
        h = 'scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler'
        s.update({'DOWNLOAD_HANDLERS': {'http': h, 'https': h},
                  'TWISTED_REACTOR': 'twisted.internet.asyncioreactor.AsyncioSelectorReactor',
                  'PLAYWRIGHT_BROWSER_TYPE': 'chromium', 'PLAYWRIGHT_LAUNCH_OPTIONS': {'headless': True},
                  'PLAYWRIGHT_ABORT_REQUEST': lambda req: _unsafe_literal(req.url)})
    return s
