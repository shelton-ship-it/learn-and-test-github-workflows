"""Extracção de magnets e metadados (selectores configurados, JSON-LD, Open Graph e estrutura da página).

Princípios (cada um corrige um defeito observado em execuções reais):
  * O ANO do título nunca vem da data de publicação do artigo: vem do título "(AAAA)", do chip /genero/AAAA/, do selo do
    cartão da listagem ou de um objecto JSON-LD Movie/TVSeries. A data de publicação (Article/WebPage/article:published_time/
    <time>) é guardada à parte em published_at e é ela que define a ordem "do mais recente para o mais antigo".
  * Géneros, realizador e elenco só são lidos na zona do conteúdo (depois do título, antes dos blocos "relacionados"),
    nunca no menu/cabeçalho/rodapé — senão todos os itens herdam todos os géneros do menu (incl. Filmes E Séries).
  * Cada magnet fica com o grupo de download em que o site o mostra (principal / alternativo); magnets de blocos
    "relacionados" nunca entram.
"""
import html as htmllib
import json
import re
import time
import unicodedata
import urllib.parse
from collections import Counter
from datetime import datetime, timezone

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


_LABEL_JUNK = re.compile(r'[\u2190-\u21ff\u2600-\u27bf\U0001F300-\U0001FAFF\u2b00-\u2bff\uff0b+]+')

# Cabeçalhos de blocos (rótulos estruturais habituais em sites de catálogo em português).
_H_PRIMARY = re.compile(r'^\W*(dispon[ií]vel\s+para\s+download|op[cç][oõ]es\s+de\s+download|links?\s+de\s+download|baixar\s+(?:torrent|agora))\b', re.I)
_H_ALT = re.compile(r'^\W*(op[cç][oõ]es\s+alternativas?|outras\s+op[cç][oõ]es|downloads?\s+alternativos?)\b', re.I)
_H_RELATED = re.compile(r'^\W*(veja\s+tamb[eé]m|relacionad[oa]s?|voc[eê]\s+(?:tamb[eé]m\s+)?(?:pode|vai)\s+gostar|t[ií]tulos\s+similares|'
                        r'(?:filmes|s[eé]ries)\s+em\s+destaque|[uú]ltimos\s+filmes|[uú]ltimas\s+s[eé]ries|explore\s+o\s+site)\b', re.I)
_CHROME_TAGS = {'nav', 'footer', 'aside'}
_CHROME_CLS = re.compile(r'(^|[\s_-])(menu|navbar|sidebar|widget|footer|related|relacionad\w*|breadcrumbs?)([\s_-]|$)', re.I)
_SKIP_TAGS = {'script', 'style', 'html', 'body', 'head'}


def _el_text(el):
    return re.sub(r'\s+', ' ', ''.join(el.itertext())).strip()


def _heading_kind(el):
    """'primary' | 'alternative' | 'related' | None — só para elementos curtos (títulos de bloco), sem magnets dentro."""
    if not isinstance(el.tag, str) or el.tag in _SKIP_TAGS or len(el) > 3:
        return None
    text = _LABEL_JUNK.sub(' ', _el_text(el)).strip()
    if not text or len(text) > 70:
        return None
    for rx, kind in ((_H_ALT, 'alternative'), (_H_PRIMARY, 'primary'), (_H_RELATED, 'related')):
        if rx.search(text):
            return kind
    return None


def _in_chrome(el):
    for a in el.iterancestors():
        if not isinstance(a.tag, str):
            continue
        if a.tag in _CHROME_TAGS or _CHROME_CLS.search(f"{a.get('class') or ''} {a.get('id') or ''}"):
            return True
    return False


# Textos que NUNCA são o título de um item: chamadas à acção/SEO do site ("Baixar SITE via torrent", "Baixar torrent", "Download"),
# menus ("Início", "Buscar") e o próprio nome/domínio do site.
_GENERIC_TITLE = re.compile(r'^\W*(baixar|download|assistir|torrent|buscar|in[ií]cio|home|menu|filmes|s[eé]ries)\W*$|^\W*(baixar|download)\b|\bvia\s+torrent\b|'
                            r'\b(torrent|download)\s+(gr[aá]tis|dublado|dual)\b|[a-z0-9-]+\.(?:net|com|org|io|tv|me|info|xyz|site|cc|top|to|app)\b', re.I)


def is_bad_title(text, host=None):
    t = re.sub(r'\s+', ' ', str(text or '')).strip()
    if len(t) < 2 or _GENERIC_TITLE.search(t):
        return True
    if host:
        label = host.lower().removeprefix('www.').split('.')[0]
        if len(label) >= 5 and label in re.sub(r'[^a-z0-9]', '', t.lower()):
            return True
    return False


def slug_tokens(url):
    seg = [x for x in urllib.parse.urlsplit(url or '').path.split('/') if x]
    return {t for t in _norm_tokens(seg[-1].replace('-', ' ')) if len(t) >= 2 and not t.isdigit()} if seg else set()


def title_from_slug(url):
    """Último recurso: o URL dos itens espelha o título. Nunca inventa — só humaniza o que o site pôs no URL."""
    seg = [x for x in urllib.parse.urlsplit(url or '').path.split('/') if x]
    if not seg:
        return None
    words = [w for w in seg[-1].split('-') if w]
    year = words[-1] if words and re.fullmatch(r'(19[2-9]\d|20[0-4]\d)', words[-1]) else None
    body = ' '.join(words[:-1] if year else words).strip()
    if len(body) < 2:
        return None
    t = body[:1].upper() + body[1:]
    return f'{t} ({year})' if year else t


def pick_title(cands, url, host=None):
    """cands = [(origem, texto)] por ordem de confiança. Rejeita textos genéricos/SEO/nome do site; entre os restantes prefere o
    que bate com o URL do item (o slug mirrors o título); sem nenhum válido, humaniza o slug. -> (título, origem, nota)"""
    ok = [(o, c) for o, c in cands if c and not is_bad_title(c, host)]
    sl = slug_tokens(url)
    if ok and len(sl) >= 2:
        for o, c in ok:
            tk = _norm_tokens(c)
            if tk and len(tk & sl) / len(sl) >= 0.6:
                return c, o, None
        o, c = ok[0]
        return c, o, 'título não bate com o URL do item'
    if ok:
        return ok[0][1], ok[0][0], None
    t = title_from_slug(url)
    return (t, 'url-slug', 'nenhum título válido na página: derivado do URL') if t else (None, None, 'sem título válido')


def _norm_tokens(s):
    t = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    return set(re.findall(r'[a-z0-9]+', t))


def find_title_el(sel, ref_title=None, host=None, url=None):
    """O <h1>/<h2> que contém o título do item (o que mais se parece com og:title/<title>). Evita apanhar o logótipo/hero
    de outros blocos. None se não houver candidato fiável."""
    root = sel.root
    cands = [el for el in root.iter('h1', 'h2') if isinstance(el.tag, str) and not _in_chrome(el)]
    cands = [(el, _el_text(el)) for el in cands]
    cands = [(el, t) for el, t in cands if len(t) >= 2 and not is_bad_title(clean_title(t), host)]
    if not cands:
        return None
    ref = _norm_tokens(ref_title) if ref_title and not is_bad_title(ref_title, host) else set()
    sl = slug_tokens(url)                      # o URL do item espelha o título: segunda referência quando og:title/<title> são genéricos
    best, best_score = None, 0.0
    for el, t in cands:
        tk = _norm_tokens(clean_title(t))
        if not tk:
            continue
        score = max(len(tk & ref) / max(1, len(tk)) if ref else 0.0, len(tk & sl) / len(sl) if len(sl) >= 2 else 0.0)
        if score > best_score:
            best, best_score = el, score
    if best is not None and best_score >= 0.6:
        return best
    return None            # sem correspondência fiável: melhor sem elemento-título do que um h1 genérico do cabeçalho


def locate_title(sel, url=None):
    """-> (elemento do título, nome do site, título de referência) — partilhado por magnet_options e extract_metadata."""
    og = lambda p: (sel.css(f'meta[property="{p}"]::attr(content)').get() or '').strip() or None
    site_name = og('og:site_name')
    ref = clean_title(og('og:title') or (sel.css('title::text').get() or '').strip(), site_name)
    host = urllib.parse.urlsplit(url).netloc.lower() if url else None
    return find_title_el(sel, ref, host, url), site_name, ref


def magnet_options(sel, css=None, limit=50, title_el=None):
    """[{magnet, label, group}] em ordem de documento. label = texto visível do botão (qualidade | contentor | tamanho | áudio …);
    group = 'primary' | 'alternative' conforme o cabeçalho do bloco em que o site o mostra. Magnets depois de um cabeçalho de
    'relacionados' (veja também, em destaque…) são descartados. Magnets fora de elementos (scripts/JSON) entram sem rótulo."""
    scopes = sel.css(css) if css else [sel]
    out, index = [], {}

    def add(magnet, label, group):
        if magnet in index:
            if label and not out[index[magnet]]['label']:
                out[index[magnet]]['label'] = label
            return
        if len(out) >= limit:
            return
        index[magnet] = len(out)
        out.append({'magnet': magnet, 'label': label, 'group': group})

    for scope in scopes:
        root = scope.root
        group, after_title = 'primary', (title_el is None or bool(css))
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            if not after_title:
                if el is title_el:
                    after_title = True
                continue
            kind = _heading_kind(el)
            if kind:
                group = kind
                continue
            if group == 'related':
                continue
            if el.tag == 'script':
                for m in find_magnets(el.text or '', limit):
                    add(m, None, group)
                continue
            attrs = ' '.join(v for v in el.attrib.values() if 'magnet' in v.lower())
            if attrs:
                label = re.sub(r'\s+', ' ', _LABEL_JUNK.sub(' ', _el_text(el))).strip()
                par = el.getparent()
                if not label and par is not None and len(par) <= 3:
                    label = re.sub(r'\s+', ' ', _LABEL_JUNK.sub(' ', _el_text(par))).strip()
                for m in find_magnets(attrs, limit):
                    add(m, label[:200] if label else None, group)
            for txt in (el.text, el.tail):
                if txt and 'magnet' in txt.lower():
                    for m in find_magnets(txt, limit):
                        add(m, None, group)
    return out[:limit]


_TITLE_TAIL = re.compile(r'\s+(?:[-–|:]\s*)?Torrent\b.*$', re.I)
_TITLE_AUDIO = re.compile(r'\bTorrent\s+(.+?)\s+Download\s*$', re.I)
_DESC_HEAD = re.compile(r'^.{0,160}?\bDownload\s*[–—-]\s*', re.I)
GENRE_SEGS  = {'genero', 'generos', 'genre', 'genres', 'categoria', 'categorias', 'category', 'tag'}
DIRECTOR_SEGS = {'diretor', 'diretores', 'director', 'directors', 'direcao'}
CAST_SEGS   = {'atores', 'ator', 'atriz', 'elenco', 'actor', 'actors', 'cast', 'stars'}
TITLE_YEAR_RE = re.compile(r'[\(\[]\s*(19[2-9]\d|20[0-4]\d)\s*[\)\]]')
_YEAR_ONLY = re.compile(r'^(19[2-9]\d|20[0-4]\d)$')


def clean_title(t, site_name=None):
    """Tira frases de SEO coladas ao título ("... Torrent Dual Áudio Download") e o nome do site. Só corta se sobrar um título razoável."""
    if not t:
        return t
    c = _TITLE_TAIL.sub('', t).strip()
    c = re.sub(r'\s+Download$', '', c, flags=re.I).strip()
    if site_name:
        c = re.sub(r'\s*[-–|]\s*' + re.escape(site_name) + r'\s*$', '', c, flags=re.I).strip()
    return c if len(c) >= 2 else t


def title_audio(raw):
    """O texto entre "Torrent" e "Download" no título do site (ex.: Dual Áudio) — metadado do próprio site."""
    m = _TITLE_AUDIO.search(raw or '')
    return re.sub(r'\s+', ' ', m.group(1)).strip()[:60] if m else None


def title_year(title):
    """Ano entre parênteses no título ("Nome (2021)"): o último que aparecer. Anos soltos ("Blade Runner 2049") não contam."""
    found = TITLE_YEAR_RE.findall(title or '')
    return int(found[-1]) if found else None


def clean_description(d):
    if not d:
        return d
    c = _DESC_HEAD.sub('', d, count=1).strip()
    return c if len(c) >= 40 else d


def content_links(sel, base_url, title_el=None):
    """Géneros/tags, realizadores e elenco pelo PADRÃO DO URL (/genero/x/, /diretor/y/, /atores/z/), SÓ na zona do conteúdo:
    depois do título e antes do primeiro bloco "relacionados"; nunca dentro de menu/cabeçalho/rodapé/barra lateral."""
    terms, directors, cast = [], [], []
    base = urllib.parse.urlsplit(base_url).netloc.lower().removeprefix('www.')
    after = title_el is None
    for el in sel.root.iter():
        if not isinstance(el.tag, str):
            continue
        if not after:
            if el is title_el:
                after = True
            continue
        if el.tag != 'a':
            if _heading_kind(el) == 'related':
                break
            continue
        href = (el.get('href') or '').strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'magnet:')):
            continue
        u = urllib.parse.urlsplit(urllib.parse.urljoin(base_url, href))
        if u.netloc.lower().removeprefix('www.') != base:
            continue
        segs = [x for x in u.path.split('/') if x]
        if len(segs) != 2:
            continue
        kind = segs[0].lower()
        if kind not in GENRE_SEGS and kind not in DIRECTOR_SEGS and kind not in CAST_SEGS:
            continue
        text = re.sub(r'\s+', ' ', _el_text(el)).strip()
        if not text or len(text) > 60 or _in_chrome(el):
            continue
        (terms if kind in GENRE_SEGS else directors if kind in DIRECTOR_SEGS else cast).append(text)
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
        if node.get('name') or node.get('headline') or node.get('datePublished'):
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


def _ld_types(o):
    t = o.get('@type')
    return {str(v).lower() for v in (t if isinstance(t, list) else [t]) if isinstance(v, str)}


_LD_CONTENT = {'movie', 'tvseries', 'tvseason', 'tvepisode', 'videoobject'}
_LD_DOC = {'article', 'blogposting', 'newsarticle', 'webpage', 'itempage', 'creativework'}


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r'[,;|]', v) if x.strip()]
    if isinstance(v, list):
        return [str(x.get('name') if isinstance(x, dict) else x).strip() for x in v if x]
    return [str(v)]


def parse_datetime_ms(s):
    """Data ISO (ou AAAA-MM-DD) -> milissegundos UTC. None se inválida, anterior a 2000 ou no futuro (> 1 dia)."""
    s = (s or '').strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
        if not m:
            return None
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    ms = int(d.timestamp() * 1000)
    return ms if 946684800000 <= ms <= time.time() * 1000 + 86_400_000 else None


# Ordem de desempate quando as fontes do ano divergem (o ano entre parênteses no título é o mais fiável).
_YEAR_PRIORITY = ('title', 'listing', 'chip', 'json-ld')


def resolve_year(cands):
    """cands = {'config','title','chip','listing','json-ld'} -> (ano, origem, notas). Maioria entre as fontes; empate = prioridade.
    Nunca devolve o ano de publicação do artigo (esse é published_at)."""
    if cands.get('config'):
        return cands['config'], 'config', []
    vals = {k: cands[k] for k in _YEAR_PRIORITY if cands.get(k)}
    if not vals:
        return None, None, []
    cnt = Counter(vals.values())
    top = max(cnt.values())
    year = next(vals[k] for k in _YEAR_PRIORITY if k in vals and cnt[vals[k]] == top)
    notes = []
    if len(cnt) > 1:
        notes.append('anos divergentes entre fontes: ' + ', '.join(f'{k}={v}' for k, v in vals.items()))
    return year, '+'.join(k for k in _YEAR_PRIORITY if vals.get(k) == year), notes


def extract_metadata(response, detail_cfg=None):
    """Devolve {title, description, year, tags, subcategory, thumb, terms, directors, cast, published_at, fields, provenance,
    site, year_candidates}. Nunca inventa valores. `site` guarda o que o site mostra tal como está (título bruto, áudio do
    título, chips, origem da data de publicação)."""
    cfg = detail_cfg or {}
    sel = Selector(text=response.text)
    md = {'title': None, 'description': None, 'year': None, 'tags': [], 'subcategory': None, 'thumb': None,
          'terms': [], 'directors': [], 'cast': [], 'published_at': None, 'fields': {}, 'provenance': {},
          'site': {}, 'year_candidates': {}}

    def put(field, value, origin):
        if value in (None, '', []) or md[field] not in (None, '', []):
            return
        md[field] = value
        md['provenance'][field] = origin

    og = lambda p: (sel.css(f'meta[property="{p}"]::attr(content)').get() or '').strip() or None
    host = urllib.parse.urlsplit(response.url).netloc.lower()
    title_el, site_name, _ref = locate_title(sel, response.url)
    raw_h1 = _el_text(title_el) if title_el is not None else None
    tcands = []                                   # (origem, texto) por ordem de confiança

    # 1) selectores configurados (fonte explícita)
    tcands.append(('config', _first_text(sel, cfg.get('title'))))
    put('description', _first_text(sel, cfg.get('description')), 'config')
    put('subcategory', _first_text(sel, cfg.get('subcategory')), 'config')
    put('tags', _all_text(sel, cfg.get('tags')), 'config')
    put('terms', _all_text(sel, cfg.get('terms')), 'config')
    put('directors', _all_text(sel, cfg.get('directors')), 'config')
    put('cast', _all_text(sel, cfg.get('cast')), 'config')
    put('thumb', _first_text(sel, cfg.get('thumb')), 'config')
    ytxt = _first_text(sel, cfg.get('year'))
    if ytxt:
        m = YEAR_RE.search(ytxt)
        if m:
            md['year_candidates']['config'] = int(m.group(1))
    for name, css in (cfg.get('fields') or {}).items():
        v = _first_text(sel, css)
        if v:
            md['fields'][str(name)[:40]] = v[:500]

    # 2) título: o <h1>/<h2> do item vem antes de JSON-LD/og/<title> (que costumam trazer sufixos do site)
    if raw_h1:
        tcands.append(('h1', clean_title(raw_h1, site_name)))

    # 3) JSON-LD — o ano só vem de objectos Movie/TVSeries; a data de Article/WebPage é a data de PUBLICAÇÃO
    objs = _ld_objects(sel)
    content_objs = [o for o in objs if _ld_types(o) & _LD_CONTENT]
    doc_objs = [o for o in objs if not (_ld_types(o) & _LD_CONTENT) and (_ld_types(o) & _LD_DOC)]
    primary = (content_objs or [o for o in objs if o.get('name') or o.get('headline')] or [None])[0]
    if primary is not None:
        o = primary
        tcands.append(('json-ld', clean_title(str(o.get('name') or o.get('headline') or '').strip() or None, site_name)))
        put('description', str(o.get('description') or '').strip() or None, 'json-ld')
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
    if content_objs:
        o = content_objs[0]
        m = YEAR_RE.search(str(o.get('datePublished') or o.get('dateCreated') or o.get('copyrightYear') or ''))
        if m:
            md['year_candidates']['json-ld'] = int(m.group(1))
    for o in doc_objs:
        ms = parse_datetime_ms(str(o.get('datePublished') or ''))
        if ms:
            md['published_at'], md['provenance']['published_at'] = ms, 'json-ld'
            break

    # 4) Open Graph e data de publicação alternativa
    tcands.append(('open-graph', clean_title(og('og:title'), site_name)))
    put('description', og('og:description') or (sel.css('meta[name="description"]::attr(content)').get() or '').strip() or None, 'open-graph')
    put('thumb', og('og:image'), 'open-graph')
    put('tags', [t.strip() for t in sel.css('meta[property="article:tag"]::attr(content)').getall() if t.strip()], 'open-graph')
    for origin, raw in (('open-graph', og('article:published_time')),
                        ('meta-itemprop', sel.css('meta[itemprop="datePublished"]::attr(content)').get()),
                        ('time-tag', sel.css('article time[datetime]::attr(datetime), time[datetime]::attr(datetime)').get())):
        if md['published_at'] is None:
            ms = parse_datetime_ms(raw)
            if ms:
                md['published_at'], md['provenance']['published_at'] = ms, origin

    # 5) <title>
    tcands.append(('html-title', clean_title((sel.css('title::text').get() or '').strip(), site_name)))
    md['title'], tsrc, tnote = pick_title(tcands, response.url, host)
    if tsrc:
        md['provenance']['title'] = tsrc

    # 6) géneros/realizador/elenco pela estrutura do conteúdo (nunca pelo menu)
    a_terms, a_dirs, a_cast = content_links(sel, response.url, title_el)
    put('terms', a_terms, 'url-pattern')
    put('directors', a_dirs, 'url-pattern')
    put('cast', a_cast, 'url-pattern')

    md['description'] = clean_description(md['description'])
    if not md['description']:
        for p_ in sel.css('p'):
            txt = re.sub(r'\s+', ' ', ' '.join(p_.css('::text').getall())).strip()
            if len(txt) >= 80:
                md['description'], md['provenance']['description'] = clean_description(txt), 'paragraph'
                break

    # 7) candidatos a ano (a decisão final junta também o selo do cartão da listagem — ver adaptador)
    ty = title_year(md['title']) or title_year(raw_h1)
    if ty:
        md['year_candidates']['title'] = ty
    chip_years = [int(t) for t in md['terms'] if _YEAR_ONLY.match(str(t).strip())]
    if chip_years:
        md['year_candidates']['chip'] = chip_years[0]
    year, ysrc, ynotes = resolve_year(md['year_candidates'])
    if year:
        md['year'], md['provenance']['year'] = year, ysrc
    md['site'] = {'raw_title': (raw_h1 or '')[:300] or None, 'audio': title_audio(raw_h1), 'chips': list(dict.fromkeys(md['terms']))[:30],
                  'year_notes': ynotes, 'title_located': title_el is not None, 'title_note': tnote}

    if md['thumb']:
        md['thumb'] = urllib.parse.urljoin(response.url, md['thumb'])
    for k, n in (('tags', 30), ('terms', 20), ('directors', 10), ('cast', 40)):
        md[k] = list(dict.fromkeys(md[k]))[:n]
    return md
