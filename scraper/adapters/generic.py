"""Adaptador genérico (JSON opcional em options.config).

Com seletores configurados usa-os. SEM seletores (modo automático) reconhece a estrutura típica de sites de
catálogo: cartões repetidos com ligações de um só segmento (/titulo-2026/), paginação por rel=next, /page/N/ ou
texto "próxima", e taxonomias/pessoas pelo padrão do URL (/genero/x/, /diretor/y/, /atores/z/).

  link_selector      CSS dos <a> para as páginas de detalhe (opcional: sem ele, modo automático)
  item_selector      CSS de cada cartão da listagem (opcional; link/thumb/título são relativos a ele)
  thumb_selector     CSS da imagem do cartão (usa ::attr(src) ou ::attr(data-src))
  key                {"attr": "data-id"} (no cartão) ou {"regex": "/(\\d+)-"} (no URL de detalhe)
                     sem 'key' => URL canónico do detalhe
  next_selector      CSS do link "página seguinte"
  discover_sections  {"selector": "a.cat", "include_regex": "...", "max": 30} — enumera categorias na página inicial
  detail             {"title","description","thumb","tags","year","subcategory","terms","directors","cast",
                      "magnet_selector","fields":{nome:css}}   (terms = géneros/chips; os magnets levam o texto do botão como rótulo)
  wait_selector      (Playwright) CSS a esperar antes de ler a página
  obey_robots        true (por omissão)
  max_magnets_per_item  50
"""
import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field

from parsel import Selector

from ..extract import _in_chrome, extract_metadata, find_magnets, is_bad_title, locate_title, magnet_options, resolve_year


@dataclass
class Entry:
    key: str
    url: str
    thumb: str = None
    title: str = None
    magnets: list = field(default_factory=list)
    rank: int = None          # posição na listagem do site (0 = o mais recente); só desempata quando o site não dá data de publicação
    year: int = None          # selo de ano do cartão da listagem (quando o site o mostra)


GENRE_LIKE = {'genero', 'generos', 'categoria', 'categorias', 'category', 'tag', 'atores', 'ator', 'diretor', 'diretores', 'page', 'busca', 'search'}


def canonical(url):
    u = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if not k.lower().startswith('utm_')]
    return urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path, urllib.parse.urlencode(q), ''))


class GenericAdapter:
    name = 'generic'

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.auto = not self.cfg.get('link_selector')    # sem seletores: reconhece a estrutura sozinho (ver _auto_*)
        self.key_cfg = self.cfg.get('key') or {}
        self.detail_cfg = self.cfg.get('detail') or {}
        self.max_magnets = int(self.cfg.get('max_magnets_per_item', 50))

    # ── modo automático ───────────────────────────────────────────────────
    RESERVED = {'genero', 'generos', 'genre', 'genres', 'categoria', 'categorias', 'category', 'tag', 'tags', 'page', 'diretor',
                'diretores', 'director', 'atores', 'ator', 'elenco', 'actor', 'author', 'autor', 'feed', 'search', 'busca', 'buscar',
                'sobre', 'about', 'contato', 'contact', 'login', 'register', 'wp-content', 'wp-admin', 'wp-json', 'tag', 'sitemap',
                'privacidade', 'dmca', 'termos', 'terms', 'privacy'}
    _NEXT_TEXT = re.compile(r'^\s*(pr[óo]xim[ao]|next|seguinte|mais antigos?|older)\b|[»›>]\s*$', re.I)

    @staticmethod
    def _host(u):
        return urllib.parse.urlsplit(u).netloc.lower().removeprefix('www.')

    @staticmethod
    def _sig(el, depth=3):
        parts, cur = [], el
        for _ in range(depth):
            if cur is None:
                break
            parts.append(cur.tag + '.' + '.'.join(sorted((cur.get('class') or '').split())))
            cur = cur.getparent()
        return '>'.join(parts)

    _YEAR_BADGE = re.compile(r'^(19[2-9]\d|20[0-4]\d)$')

    @staticmethod
    def _card_scope(a, url, base):
        """O cartão = o ancestral mais largo (até 3 níveis) cujas ligações apontam todas para o mesmo detalhe."""
        scope, cur = a, a
        for _ in range(3):
            cur = cur.getparent()
            if cur is None:
                break
            urls = {canonical(urllib.parse.urljoin(base, (x.get('href') or '').strip())) for x in cur.iter('a') if (x.get('href') or '').strip()}
            if urls - {url}:
                break
            scope = cur
        return scope

    def _badge_year(self, scope):
        for leaf in scope.iter():
            if isinstance(leaf.tag, str) and len(leaf) == 0:
                t = (leaf.text or '').strip()
                if self._YEAR_BADGE.match(t):
                    return int(t)
        return None

    def _auto_entries(self, response):
        sel = Selector(text=response.text)
        host = self._host(response.url)
        cands = []
        for a in sel.root.iter('a'):
            href = (a.get('href') or '').strip()
            if not href or href.startswith(('#', 'javascript:', 'mailto:', 'magnet:')):
                continue
            url = urllib.parse.urljoin(response.url, href)
            u = urllib.parse.urlsplit(url)
            if self._host(url) != host or u.query:
                continue
            segs = [x for x in u.path.split('/') if x]
            if len(segs) != 1 or segs[0].lower() in self.RESERVED or segs[0].lower().startswith('wp-') or len(segs[0]) < 4:
                continue
            cands.append((a, canonical(url)))
        if not cands:
            return []
        sigs = Counter(self._sig(a) for a, _ in cands)
        top = max(sigs.values())
        keep = {sg for sg, n in sigs.items() if n >= max(4, top // 2)}
        if not keep:
            return []
        by_url = {}
        for a, url in cands:
            if self._sig(a) not in keep:
                continue
            text = re.sub(r'\s+', ' ', ' '.join(a.itertext())).strip()
            scope = self._card_scope(a, url, response.url)
            badge = self._badge_year(scope)
            if badge and text.startswith(str(badge)) and len(text) > 4:
                text = text[4:].strip()                 # o selo de ano vem colado ao texto do cartão
            if is_bad_title(text, urllib.parse.urlsplit(response.url).netloc):
                text = ''                               # "Baixar torrent", nome do site… nunca são o título do item
            img = a.find('.//img')
            if img is None:
                img = scope.find('.//img')
            thumb = None
            if img is not None:
                thumb = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
                if thumb:
                    thumb = urllib.parse.urljoin(response.url, thumb)
            title = (img.get('alt') if img is not None and img.get('alt') else None)
            cur = by_url.get(url)
            best_text = text if len(text) >= 4 else None
            if cur is None:
                by_url[url] = Entry(key=url, url=url, thumb=thumb, title=(best_text or title or None), magnets=[], year=badge)
            else:
                cur.year = cur.year or badge
                if best_text and (not cur.title or len(best_text) > len(cur.title)):
                    cur.title = best_text
                cur.thumb = cur.thumb or thumb
        return [e for e in by_url.values()]

    def _auto_next(self, response):
        sel = Selector(text=response.text)
        n = sel.css('a[rel~="next"]::attr(href), link[rel="next"]::attr(href)').get()
        if n:
            return urllib.parse.urljoin(response.url, n)
        m = re.search(r'/page/(\d+)/?$', urllib.parse.urlsplit(response.url).path)
        cur = int(m.group(1)) if m else 1
        host = self._host(response.url)
        by_text = None
        for a in sel.css('a[href]'):
            url = urllib.parse.urljoin(response.url, a.attrib['href'])
            if self._host(url) != host:
                continue
            pm = re.search(r'/page/(\d+)/?$', urllib.parse.urlsplit(url).path)
            text = re.sub(r'\s+', ' ', ' '.join(a.css('::text').getall())).strip()
            if pm and int(pm.group(1)) == cur + 1 and self._NEXT_TEXT.search(text):
                return url
            if pm and int(pm.group(1)) == cur + 1 and not by_text:
                by_text = url
        return by_text

    # ── listagem ─────────────────────────────────────────────────────────
    def _key(self, url, node):
        if self.key_cfg.get('attr') and node is not None:
            v = node.attrib.get(self.key_cfg['attr'])
            if v:
                return str(v)[:200]
        if self.key_cfg.get('regex'):
            m = re.search(self.key_cfg['regex'], url)
            if m:
                return (m.group(1) if m.groups() else m.group(0))[:200]
        return canonical(url)

    _CAR_STRONG = re.compile(r'carousel|slider|slick|swiper|owl-|flickity|splide|glide', re.I)
    _CAR_WEAK = re.compile(r'hero|banner|destaque|featured', re.I)
    _CLONED = re.compile(r'clone|duplicate', re.I)

    def featured_entries(self, response):
        """Carrossel de destaque da página: [{position, url, thumb}] pela ordem em que o site o mostra (sem clones do slider).
        Heurística: ligações com miniatura dentro de um contentor de carrossel/slider (ou, sem esse, banner/destaque) com 3 a 15 itens."""
        sel = Selector(text=response.text)
        base = urllib.parse.urlsplit(response.url).netloc.lower().removeprefix('www.')
        cls = lambda x: f"{x.get('class') or ''} {x.get('id') or ''}"
        strong, weak = {}, {}
        for a in sel.root.iter('a'):
            href = (a.get('href') or '').strip()
            if not href or href.startswith(('#', 'javascript:', 'mailto:', 'magnet:')) or a.find('.//img') is None:
                continue
            u = urllib.parse.urlsplit(urllib.parse.urljoin(response.url, href))
            if u.netloc.lower().removeprefix('www.') != base or not u.path.strip('/') or u.path.strip('/').split('/')[0].lower() in GENRE_LIKE:
                continue
            anc = list(a.iterancestors())[:10]
            if any(self._CLONED.search(cls(x)) for x in [a] + anc[:6]) or _in_chrome(a):
                continue
            url = canonical(urllib.parse.urljoin(response.url, href))
            img = a.find('.//img')
            thumb = img.get('data-src') or img.get('data-lazy-src') or img.get('src')
            for rx, bucket in ((self._CAR_STRONG, strong), (self._CAR_WEAK, weak)):
                matched = [x for x in anc if rx.search(cls(x))]
                box = matched[-1] if matched else None        # o contentor mais alto: cada slide também tem classe "slide"
                if box is not None:
                    bucket.setdefault(id(box), {})[url] = urllib.parse.urljoin(response.url, thumb) if thumb else None
                    break
        for bucket in (strong, weak):
            groups = [g for g in bucket.values() if 3 <= len(g) <= 15]
            if groups:
                best = max(groups, key=len)
                return [{'position': i, 'url': u, 'thumb': t} for i, (u, t) in enumerate(best.items())]
        return []

    def list_entries(self, response):
        if self.auto:
            return self._auto_entries(response)
        sel = Selector(text=response.text)
        entries, seen = [], set()
        nodes = sel.css(self.cfg['item_selector']) if self.cfg.get('item_selector') else [sel]
        for node in nodes:
            link_nodes = node.css(self.cfg['link_selector'])
            if not link_nodes:
                continue
            for a in (link_nodes if not self.cfg.get('item_selector') else link_nodes[:1]):
                href = a.attrib.get('href')
                if not href or href.startswith(('javascript:', 'mailto:', '#', 'magnet:')):
                    continue
                url = canonical(urllib.parse.urljoin(response.url, href.strip()))
                key = self._key(url, node if self.cfg.get('item_selector') else a)
                if key in seen:
                    continue
                seen.add(key)
                thumb = None
                if self.cfg.get('thumb_selector') and self.cfg.get('item_selector'):
                    t = node.css(self.cfg['thumb_selector'])
                    if t:
                        thumb = t[0].attrib.get('src') or t[0].attrib.get('data-src') or t[0].css('::attr(src)').get()
                        if thumb:
                            thumb = urllib.parse.urljoin(response.url, thumb)
                title = (a.attrib.get('title') or ' '.join(a.css('::text').getall())).strip() or None
                mags = find_magnets(node.get(), self.max_magnets) if self.cfg.get('item_selector') else []
                entries.append(Entry(key=key, url=url, thumb=thumb, title=title[:300] if title else None, magnets=mags))
        return entries

    def next_page(self, response):
        css = self.cfg.get('next_selector')
        if not css:
            return self._auto_next(response) if self.auto else None
        n = Selector(text=response.text).css(css)
        href = n[0].attrib.get('href') if n else None
        return urllib.parse.urljoin(response.url, href) if href and not href.startswith(('javascript:', '#')) else None

    def discover_sections(self, response):
        d = self.cfg.get('discover_sections')
        if not d or not d.get('selector'):
            return []
        inc = re.compile(d['include_regex']) if d.get('include_regex') else None
        out = []
        for a in Selector(text=response.text).css(d['selector']):
            href = a.attrib.get('href')
            if not href:
                continue
            url = canonical(urllib.parse.urljoin(response.url, href))
            if inc and not inc.search(url):
                continue
            if url not in out:
                out.append(url)
            if len(out) >= int(d.get('max', 30)):
                break
        return out

    # ── detalhe ──────────────────────────────────────────────────────────
    def parse_detail(self, response, entry):
        sel = Selector(text=response.text)
        title_el, _site, _ref = locate_title(sel, response.url)
        opts = magnet_options(sel, self.detail_cfg.get('magnet_selector'), self.max_magnets, title_el)
        md = extract_metadata(response, self.detail_cfg)
        if entry is not None and getattr(entry, 'year', None):          # o selo do cartão entra na votação do ano
            cands = dict(md['year_candidates'], listing=entry.year)
            year, src, notes = resolve_year(cands)
            md['year_candidates'] = cands
            if year:
                md['year'], md['provenance']['year'] = year, src
            md['site']['year_notes'] = notes
        return {'magnets': [o['magnet'] for o in opts], 'labels': {o['magnet']: o['label'] for o in opts if o['label']},
                'groups': {o['magnet']: o['group'] for o in opts}, 'metadata': md}

    def parse_inline(self, response, entry):
        """Modo extract_details=false: o cartão da listagem é o item (sem visitar o detalhe)."""
        prov = {}
        if entry.title:
            prov['title'] = 'listing'
        if entry.year:
            prov['year'] = 'listing'
        return {'magnets': list(entry.magnets), 'labels': {}, 'groups': {}, 'metadata': {
            'title': entry.title, 'description': None, 'year': entry.year, 'tags': [], 'subcategory': None, 'thumb': entry.thumb,
            'terms': [], 'directors': [], 'cast': [], 'published_at': None, 'fields': {}, 'provenance': prov,
            'site': {}, 'year_candidates': {'listing': entry.year} if entry.year else {}}}
