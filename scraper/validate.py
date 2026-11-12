"""Validações por item, antes de qualquer escrita (tipo filme/série, categoria, ano, magnets, data de publicação).

O tipo e a categoria de cada item vêm da PRÓPRIA página (chips, título, rótulos dos downloads) — não de uma configuração
única da fonte, que fazia todos os itens ficarem iguais. A configuração da fonte só serve de último recurso, e isso fica
registado (type_source/avisos) para se poder auditar.

  infer_type   -> tipo + origem + evidências (pontuação; empate/contradição = conflito, nunca um palpite silencioso)
  category_of  -> categoria = primeiro género real do item (nunca ano/qualidade/tipo)
  validate_item-> (erros, avisos). Erros impedem o registo (item fica 'failed' com motivo); avisos ficam em metadata.validation.
"""
import re
import time
from collections import Counter

from .classify import _reason, slugify_text, url_slug
from .extract import is_bad_title

# Só Filmes/Séries/Animes são TIPOS no site; "Documentário", "Animação"… são géneros e não entram aqui.
CHIP_TYPE = {'filmes': 'movies', 'filme': 'movies', 'movies': 'movies', 'movie': 'movies',
             'series': 'series', 'serie': 'series', 'animes': 'anime', 'anime': 'anime'}
# Tipos que o administrador escolhe no worker e que a página não consegue distinguir (o site chama "Séries" a um anime):
# a escolha prevalece sempre; só 'movies'/'series'/'other' (genéricos) são afinados pelas evidências da página.
CHOSEN_TYPES = {'anime', 'documentaries', 'music', 'games', 'software', 'books', 'comics'}

_SEASON_SLUG = re.compile(r'(^|-)(\d{1,2}[aeo]?-temporada|temporada(-\d{1,2})?|season(-\d{1,2})?)(-|$)')
_SEASON_TITLE = re.compile(r'(\d{1,2})\s*[ªaºo°]?\s*temporada|temporada\s*(\d{1,2})|season\s*(\d{1,2})', re.I)
_LABEL_SERIES = re.compile(r'\bS\d{1,2}\b|\bS\d{1,2}[\s._-]*E\d{1,3}\b|\bEp\.?\s*\d{1,3}\b|\btemporada\b|\bepis[oó]dios?\b', re.I)
_YEAR_ONLY = re.compile(r'^(19|20)\d{2}$')
_QUALITY_ONLY = re.compile(r'^(\d{3,4}p|4k)$', re.I)

MIN_YEAR = 1920
MAX_MOVIE_MAGNETS = 15            # um filme com mais do que isto é quase sempre mistura com blocos "relacionados"


def season_from_title(title):
    m = _SEASON_TITLE.search(title or '')
    if not m:
        return None
    return int(next(g for g in m.groups() if g))


def infer_type(md, url, labels, src_type):
    """-> {'type', 'source': 'chips'|'conteudo'|'fonte', 'evidence': [..], 'conflict': bool, 'chosen': bool}"""
    if src_type in CHOSEN_TYPES:
        return {'type': src_type, 'source': 'fonte', 'evidence': [f'tipo "{src_type}" escolhido na fonte (a página só distingue filme/série)'], 'conflict': False, 'chosen': True}
    score, ev = Counter(), []
    for t in md.get('terms') or []:
        ty = CHIP_TYPE.get(slugify_text(str(t)))
        if ty:
            score[ty] += 3
            ev.append(f'chip:{t}')
    slug, title_slug = url_slug(url), slugify_text(md.get('title') or '')
    if _SEASON_SLUG.search(slug) or _SEASON_SLUG.search(title_slug):
        score['series'] += 2
        ev.append('título/URL com "temporada"')
    labs = [str(l) for l in (labels or []) if l]
    if labs:
        hits = sum(1 for l in labs if _LABEL_SERIES.search(l))
        if hits == len(labs):
            score['series'] += 2
            ev.append('todos os rótulos de download têm temporada/episódio')
        elif hits == 0:
            score['movies'] += 1
            ev.append('rótulos de download sem temporada/episódio')
    ranked = score.most_common()
    fallback = src_type if src_type else 'other'
    if not ranked:
        return {'type': fallback, 'source': 'fonte', 'evidence': ['sem evidência na página'], 'conflict': False, 'chosen': False}
    top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    if top[1] - second >= 2:
        return {'type': top[0], 'source': 'chips' if any(e.startswith('chip:') for e in ev) else 'conteudo', 'evidence': ev, 'conflict': False, 'chosen': False}
    # Contradição entre sinais (ex.: chip "Filmes" mas título "Temporada"): usa a configuração da fonte se for um tipo concreto.
    return {'type': src_type if src_type in ('movies', 'series') else top[0], 'source': 'fonte', 'evidence': ev + ['sinais contraditórios'], 'conflict': True, 'chosen': False}


def category_of(md, src_category):
    """Primeiro género real (não ano, qualidade nem tipo) como categoria; senão a categoria da fonte."""
    for t in md.get('terms') or []:
        t = str(t).strip()
        if not t or _YEAR_ONLY.match(t) or _QUALITY_ONLY.match(t) or slugify_text(t) in CHIP_TYPE:
            continue
        s = slugify_text(t)[:60]
        if s:
            return s
    return src_category


def validate_item(md, typ, labels, n_magnets, now_ms=None, host=None):
    """-> (erros, avisos). Pode corrigir md in place (descarta valores inválidos, nunca inventa)."""
    errors, warnings = [], []
    now_ms = now_ms or int(time.time() * 1000)
    title = md.get('title') or ''
    if is_bad_title(title, host):
        errors.append(f'título inválido (texto genérico/SEO ou nome do site): "{title[:80]}"')
    why = _reason(slugify_text(title)) if title else None
    if why:
        errors.append(f'título parece página institucional/artigo ({why})')
    if re.search(r'\bTorrent\b.*\bDownload\b', title, re.I):
        warnings.append('título ainda traz frases de SEO do site')
    if (md.get('site') or {}).get('title_note'):
        warnings.append(md['site']['title_note'])

    y = md.get('year')
    if y is not None and not (MIN_YEAR <= int(y) <= time.gmtime(now_ms / 1000).tm_year + 1):
        warnings.append(f'ano {y} fora do intervalo plausível: descartado')
        md['year'] = None
        md.get('provenance', {}).pop('year', None)
    for note in (md.get('site') or {}).get('year_notes') or []:
        warnings.append(note)
    if md.get('year') is None:
        warnings.append('sem ano (nem no título, nem nos chips, nem no selo do cartão)')

    pub = md.get('published_at')
    if pub is not None and not (946684800000 <= pub <= now_ms + 86_400_000):
        warnings.append('data de publicação inválida: descartada')
        md['published_at'] = None
    if md.get('published_at') is None:
        warnings.append('sem data de publicação do site: a ordem usa a posição na listagem')

    if typ not in ('series', 'anime') and n_magnets > MAX_MOVIE_MAGNETS:
        errors.append(f'{n_magnets} magnets num item que não é série: provável mistura com blocos relacionados')
    labs = [str(l) for l in (labels or []) if l]
    if typ in ('series', 'anime') and labs and not any(_LABEL_SERIES.search(l) for l in labs) and not season_from_title(title):
        warnings.append('série sem temporada/episódio nos rótulos nem no título')
    if typ == 'movies' and any(_LABEL_SERIES.search(l) for l in labs):
        warnings.append('filme com rótulos de temporada/episódio')
    if not md.get('description'):
        warnings.append('sem descrição')
    if not md.get('terms'):
        warnings.append('sem géneros')
    return errors, warnings
