"""Extracção de magnets e metadados (JSON-LD, Open Graph, selectores configurados)."""
import html as htmllib
import json
import re
import urllib.parse

from parsel import Selector

MAGNET_RE = re.compile(r'magnet:\?[^\s"\'<>\\]+', re.I)
PCT_MAGNET_RE = re.compile(r'magnet%3A%3F[^\s"\'<>\\&]+', re.I)
YEAR_RE = re.compile(r'(?<!\d)(19[2-9]\d|20[0-4]\d)(?!\d)')


def _unescape(text):
    text = htmllib.unescape(text)
    return text.replace('\\u0026', '&').replace('\\u003d', '=').replace('\\/', '/')


def find_magnets(raw_html, limit=50):
    """Todos os magnets numa porção de HTML: atributos, texto, scripts, JSON-LD e links com magnet percent-encoded."""
    text = _unescape(raw_html or '')
    found = list(MAGNET_RE.findall(text))
    for m in PCT_MAGNET_RE.findall(text):
        found.append(urllib.parse.unquote(m))
    out, seen = [], set()
    for m in found:
        if m not in seen:
            seen.add(m)
            out.append(m)
        if len(out) >= limit:
            break
    return out


def magnets_from_selector(sel, css, limit=50):
    """Se há magnet_selector, só procura nos nós correspondentes (evita magnets de 'relacionados')."""
    if not css:
        return find_magnets(sel.get(), limit)
    chunks = [n.get() for n in sel.css(css)]
    return find_magnets('\n'.join(chunks), limit)


_LABEL_JUNK = re.compile(r'[\u2190-\u21ff\u2600-\u27bf\U0001F300-\U0001FAFF\u2b00-\u2bff]+')


def magnet_options(sel, css=None, limit=50):
    """[{magnet, label}] — cada elemento com um magnet leva o texto visível como rótulo (qualidade | contentor | áudio …).
    Magnets fora de elementos (scripts/JSON) entram sem rótulo."""
    scopes = sel.css(css) if css else [sel]
    out, seen = [], set()
    for scope in scopes:
        for el in scope.xpath('descendant-or-self::*[@*[contains(translate(., "MAGNET", "magnet"), "magnet")]]'):
            label = re.sub(r'\s+', ' ', _LABEL_JUNK.sub(' ', ' '.join(el.xpath('.//text()').getall()))).strip() or None
            for m in find_magnets(el.get(), limit):
                if m not in seen:
                    seen.add(m)
                    out.append({'magnet': m, 'label': label[:200] if label else None})
        for m in find_magnets(scope.get(), limit):
            if m not in seen:
                seen.add(m)
                out.append({'magnet': m, 'label': None})
    return out[:limit]


_TITLE_TAIL = re.compile(r'\s+(?:[-–|:]\s*)?Torrent\b.*$', re.I)
_DESC_HEAD = re.compile(r'^.{0,160}?\bDownload\s*[–—-]\s*', re.I)
GENRE_SEGS  = {'genero', 'generos', 'genre', 'genres', 'categoria', 'categorias', 'category', 'tag'}
DIRECTOR_SEGS = {'diretor', 'diretores', 'director', 'directors', 'direcao'}
CAST_SEGS   = {'atores', 'ator', 'atriz', 'elenco', 'actor', 'actors', 'cast', 'stars'}


def clean_title(t):
    """Tira frases de SEO coladas ao título ("... Torrent Dual Áudio Download"). Só corta se sobrar um título razoável."""
    if not t:
        return t
    c = _TITLE_TAIL.sub('', t).strip()
    c = re.sub(r'\s+Download$', '', c, flags=re.I).strip()
    return c if len(c) >= 2 else t


def clean_description(d):
    if not d:
        return d
    c = _DESC_HEAD.sub('', d, count=1).strip()
    return c if len(c) >= 40 else d


def auto_links(response):
    """Géneros/tags, realizadores e elenco reconhecidos pelo PADRÃO DO URL das ligações (/genero/x/, /diretor/y/, /atores/z/)."""
    terms, directors, cast = [], [], []
    base = urllib.parse.urlsplit(response.url).netloc.lower().removeprefix('www.')
    for a in Selector(text=response.text).css('a[href]'):
        u = urllib.parse.urlsplit(urllib.parse.urljoin(response.url, a.attrib['href']))
        if u.netloc.lower().removeprefix('www.') != base:
            continue
        segs = [x for x in u.path.split('/') if x]
        if len(segs) != 2:
            continue
        text = re.sub(r'\s+', ' ', ' '.join(a.css('::text').getall())).strip()
        if not text or len(text) > 60:
            continue
        if segs[0].lower() in GENRE_SEGS:
            terms.append(text)
        elif segs[0].lower() in DIRECTOR_SEGS:
            directors.append(text)
        elif segs[0].lower() in CAST_SEGS:
            cast.append(text)
    return terms, directors, cast


def _first_text(sel, css):
    if not css:
        return None
    nodes = sel.css(css)
    if not nodes:
        return None
    if '::' in css:
        v = nodes[0].get()
    else:
        v = ' '.join(t.strip() for t in nodes[0].css('::text').getall() if t.strip())
    v = (v or '').strip()
    return re.sub(r'\s+', ' ', v) or None


def _all_text(sel, css):
    if not css:
        return []
    if '::' in css:
        vals = sel.css(css).getall()
    else:
        vals = [' '.join(t.strip() for t in n.css('::text').getall() if t.strip()) for n in sel.css(css)]
    return [re.sub(r'\s+', ' ', v).strip() for v in vals if v and v.strip()]


def _walk_ld(node, out):
    if isinstance(node, list):
        for n in node:
            _walk_ld(n, out)
    elif isinstance(node, dict):
        if node.get('name') or node.get('headline'):
            out.append(node)
        for v in node.values():
            if isinstance(v, (list, dict)):
                _walk_ld(v, out)


def _ld_objects(sel):
    objs = []
    for s in sel.css('script[type="application/ld+json"]::text').getall():
        try:
            _walk_ld(json.loads(s), objs)
        except (ValueError, TypeError):
            continue
    return objs


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r'[,;|]', v) if x.strip()]
    if isinstance(v, list):
        return [str(x.get('name') if isinstance(x, dict) else x).strip() for x in v if x]
    return [str(v)]


def extract_metadata(response, detail_cfg=None):
    """Devolve {title, description, year, tags, subcategory, thumb, fields, provenance}. Nunca inventa valores."""
    cfg = detail_cfg or {}
    sel = Selector(text=response.text)
    md = {'title': None, 'description': None, 'year': None, 'tags': [], 'subcategory': None, 'thumb': None,
          'terms': [], 'directors': [], 'cast': [], 'published_at': None, 'fields': {}, 'provenance': {}}

    def put(field, value, origin):
        if value in (None, '', []) or md[field] not in (None, '', []):
            return
        md[field] = value
        md['provenance'][field] = origin

    # 1) selectores configurados (fonte explícita)
    put('title', _first_text(sel, cfg.get('title')), 'config')
    put('description', _first_text(sel, cfg.get('description')), 'config')
    put('subcategory', _first_text(sel, cfg.get('subcategory')), 'config')
    put('tags', _all_text(sel, cfg.get('tags')), 'config')
    put('terms', _all_text(sel, cfg.get('terms')), 'config')
    put('directors', _all_text(sel, cfg.get('directors')), 'config')
    put('cast', _all_text(sel, cfg.get('cast')), 'config')
    thumb = _first_text(sel, cfg.get('thumb'))
    put('thumb', thumb, 'config')
    ytxt = _first_text(sel, cfg.get('year'))
    if ytxt:
        m = YEAR_RE.search(ytxt)
        put('year', int(m.group(1)) if m else None, 'config')
    for name, css in (cfg.get('fields') or {}).items():
        v = _first_text(sel, css)
        if v:
            md['fields'][str(name)[:40]] = v[:500]

    # 2) JSON-LD
    for o in _ld_objects(sel):
        put('title', str(o.get('name') or o.get('headline') or '').strip() or None, 'json-ld')
        put('description', str(o.get('description') or '').strip() or None, 'json-ld')
        date = str(o.get('datePublished') or o.get('dateCreated') or '')
        m = YEAR_RE.search(date)
        put('year', int(m.group(1)) if m else None, 'json-ld')
        put('tags', _as_list(o.get('keywords')), 'json-ld')
        put('terms', _as_list(o.get('genre')), 'json-ld')
        put('directors', _as_list(o.get('director')), 'json-ld')
        put('cast', _as_list(o.get('actor')), 'json-ld')
        img = o.get('image')
        if isinstance(img, dict):
            img = img.get('url')
        if isinstance(img, list) and img:
            img = img[0].get('url') if isinstance(img[0], dict) else img[0]
        put('thumb', str(img).strip() if img else None, 'json-ld')
        break

    # 3) Open Graph
    og = lambda p: (sel.css(f'meta[property="{p}"]::attr(content)').get() or '').strip() or None
    put('title', og('og:title'), 'open-graph')
    put('description', og('og:description') or (sel.css('meta[name="description"]::attr(content)').get() or '').strip() or None, 'open-graph')
    put('thumb', og('og:image'), 'open-graph')
    put('tags', [t.strip() for t in sel.css('meta[property="article:tag"]::attr(content)').getall() if t.strip()], 'open-graph')
    pub = og('article:published_time')
    if pub and md['published_at'] is None:
        try:
            from datetime import datetime
            md['published_at'] = int(datetime.fromisoformat(pub.replace('Z', '+00:00')).timestamp() * 1000)
            md['provenance']['published_at'] = 'open-graph'
        except ValueError:
            pass

    # 4) <title>
    put('title', (sel.css('title::text').get() or '').strip() or None, 'html-title')

    # 4b) padrões de URL (só se nada foi configurado nem veio em JSON-LD)
    a_terms, a_dirs, a_cast = auto_links(response)
    put('terms', a_terms, 'url-pattern')
    put('directors', a_dirs, 'url-pattern')
    put('cast', a_cast, 'url-pattern')
    h1 = _first_text(sel, 'h1')
    if h1 and md['provenance'].get('title') in (None, 'open-graph', 'html-title'):
        md['title'], md['provenance']['title'] = h1, 'h1'
    md['title'] = clean_title(md['title'])
    md['description'] = clean_description(md['description'])
    if not md['description']:
        for p_ in sel.css('p'):
            txt = re.sub(r'\s+', ' ', ' '.join(p_.css('::text').getall())).strip()
            if len(txt) >= 80:
                md['description'], md['provenance']['description'] = clean_description(txt), 'paragraph'
                break

    # 5) ano inferido do título (marcado como inferido)
    if md['year'] is None and md['title']:
        m = YEAR_RE.search(md['title'])
        if m:
            md['year'] = int(m.group(1))
            md['provenance']['year'] = 'inferred:title'

    if md['thumb']:
        md['thumb'] = urllib.parse.urljoin(response.url, md['thumb'])
    for k, n in (('tags', 30), ('terms', 20), ('directors', 10), ('cast', 40)):
        md[k] = list(dict.fromkeys(md[k]))[:n]
    return md
