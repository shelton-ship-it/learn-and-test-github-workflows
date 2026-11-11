"""Classificação: o que NÃO é um item de catálogo (páginas institucionais, tutoriais, listas/artigos).

Duas fases, ambas conservadoras (na dúvida, trata como conteúdo — um falso "não-conteúdo" esconde um filme):

  1) ANTES de descarregar (url_verdict): só padrões inequívocos no slug/título da ligação (sobre, política de
     privacidade, DMCA, "como instalar…", VPN/Cloudflare…). Um slug que parece uma release (termina em ano, tem
     torrent/dublado/1080p…) nunca é cortado aqui — fica para a fase 2.
  2) DEPOIS de descarregar, só para páginas SEM magnet (page_verdict): pontua sinais de filme/série (ano, géneros,
     elenco, ficha técnica, JSON-LD Movie…) contra sinais de página institucional/artigo. Só classifica como
     não-conteúdo com evidência positiva; "sem magnet" por si só continua a ser uma falha (retentável).

Uma página com magnets válidos é sempre conteúdo (nem chega aqui).
"""
import json
import re
import unicodedata
import urllib.parse

from parsel import Selector


def slugify_text(text):
    t = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', t).strip('-')


def url_slug(url):
    segs = [s for s in urllib.parse.urlsplit(url).path.split('/') if s]
    return urllib.parse.unquote(segs[-1]).lower() if segs else ''


# ── padrões de NÃO-conteúdo (sobre slugs: minúsculas, sem acentos, separados por '-') ─────────────────────────────
_INSTITUTIONAL = [
    (r'^(sobre|about)(-|$)', 'página institucional (sobre)'),
    (r'(^|-)(politica|policy)-(de-)?(privacidade|privacy|cookies|dmca|uso|termos)', 'política/privacidade'),
    (r'(^|-)(privacidade|privacy)(-|$)', 'privacidade'),
    (r'(^|-)(termos|terms)-(de-|of-)?(uso|servico|use|service)', 'termos de uso'),
    (r'(^|-)dmca(-|$)', 'DMCA'),
    (r'(^|-)(direitos-autorais|copyright)(-|$)', 'direitos autorais'),
    (r'^(contato|contact|fale-conosco|anuncie|publicidade|advertise|cookies|lgpd|disclaimer|aviso-legal|faq|duvidas|'
     r'ajuda|trabalhe-conosco|mapa-do-site|sitemap|login|cadastro|minha-conta|my-account|carrinho|cart|checkout)(-|$)', 'página institucional'),
]
_ARTICLE = [
    (r'^como-(instalar|baixar|usar|configurar|acessar|assistir|funciona|desbloquear|ativar|atualizar|remover|'
     r'desinstalar|conectar|abrir)(-|$)', 'tutorial (como …)'),
    (r'(^|-)passo-a-passo(-|$)', 'tutorial (passo a passo)'),
    (r'(^|-)guia-(completo|definitivo|rapido|pratico|para|de-instalacao)(-|$)', 'guia'),
    (r'(^|-)tutorial(-|$)', 'tutorial'),
    (r'^(o-que-e|o-que-sao|o-que-significa|por-que|porque)(-|$)', 'artigo explicativo'),
    (r'^top-\d+(-|$)|^(os|as)-\d+-(melhores|maiores|piores)(-|$)|(^|-)melhores-(filmes|series|animes)(-|$)', 'lista/ranking'),
    (r'(^|-)(lancamentos|estreias)-(da-semana|do-mes)(-|$)', 'lista de lançamentos'),
]
_RULES = [(re.compile(p), why) for p, why in _INSTITUTIONAL + _ARTICLE]

_RELEASE_SLUG = re.compile(r'(^|-)(19[2-9]\d|20[0-4]\d)(-|$)|(^|-)(torrent|dublado|dual-audio|legendado|1080p|720p|2160p|bluray|web-?dl|hdtv|temporada)(-|$)')


_TECH = {'vpn', 'dns', 'cloudflare', 'proxy', 'adblock', 'antivirus', 'utorrent', 'qbittorrent', 'warp'}
_ARTICLE_HINT = {'como', 'guia', 'funciona', 'instalar', 'configurar', 'tutorial', 'melhor', 'melhores', 'gratis', 'seguro', 'bloquear'}


def _tech_article(slugish):
    """Uma palavra técnica isolada (ex.: um filme chamado "Proxy") não chega: exige 2+ termos técnicos distintos,
    ou um no início do slug acompanhado de uma palavra típica de artigo."""
    toks = slugish.split('-')
    tech = {t.rstrip('s') if t.rstrip('s') in _TECH else t for t in toks} & _TECH      # vpns → vpn
    if len(tech) >= 2:
        return True
    # 1 termo técnico: só com palavra de artigo logo no início do slug ("melhores-vpns…") ou o termo no início + palavra de artigo
    return bool(tech) and ((toks[0] in _ARTICLE_HINT) or (toks[0].rstrip('s') in _TECH and bool(_ARTICLE_HINT & set(toks))))


def _reason(slugish):
    for rx, why in _RULES:
        if rx.search(slugish):
            return why
    if _tech_article(slugish):
        return 'artigo técnico (VPN/DNS/…)'
    return None


def url_verdict(url, title=None):
    """Fase 1. -> (True, motivo) se for claramente institucional/artigo; (False, None) caso contrário."""
    slug = url_slug(url)
    if not slug or _RELEASE_SLUG.search(slug):
        return False, None                      # parece uma release (ano/torrent/qualidade): decide a fase 2
    why = _reason(slug)
    if why:
        return True, why
    t = slugify_text(title)
    if t and not _RELEASE_SLUG.search(t):
        why = _reason(t)
        if why:
            return True, f'{why} (título da ligação)'
    return False, None


def split_entries(entries):
    """(mantidas, [(entrada, motivo)]) — filtro da listagem, antes de qualquer pedido de detalhe."""
    kept, dropped = [], []
    for e in entries:
        bad, why = url_verdict(e.url, e.title)
        (dropped.append((e, why)) if bad else kept.append(e))
    return kept, dropped


# ── fase 2 ────────────────────────────────────────────────────────────────────────────────────────────────────────
_CONTENT_LD = {'movie', 'tvseries', 'videoobject', 'tvepisode', 'tvseason'}
_NONCONTENT_LD = {'aboutpage', 'contactpage', 'privacypolicy', 'faqpage', 'howto', 'collectionpage', 'searchresultspage'}
_FICHA = re.compile(r'\b(sinopse|elenco|dire[cç][aã]o|diretor|g[eê]nero|qualidade|idioma|[aá]udio|formato|dura[cç][aã]o|ficha t[eé]cnica)\b', re.I)
_QUALITY = re.compile(r'\b(1080p|720p|2160p|4k|blu-?ray|web-?dl|hdtv|dual [aá]udio|dublado|legendado)\b', re.I)
_YEAR = re.compile(r'(?<!\d)(19[2-9]\d|20[0-4]\d)(?!\d)')


def _ld_types(sel):
    out = set()

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            t = n.get('@type')
            for v in (t if isinstance(t, list) else [t]):
                if isinstance(v, str):
                    out.add(v.lower())
            for v in n.values():
                if isinstance(v, (list, dict)):
                    walk(v)
    for s in sel.css('script[type="application/ld+json"]::text').getall():
        try:
            walk(json.loads(s))
        except (ValueError, TypeError):
            continue
    return out


def page_verdict(response, md):
    """Fase 2 (página sem magnets). -> (True, motivo) só com evidência positiva de página institucional/artigo."""
    sel = Selector(text=response.text)
    slug = url_slug(response.url)
    title = md.get('title') or ''
    h1 = ' '.join(sel.css('h1 ::text').getall()).strip()
    ld = _ld_types(sel)
    text = ' '.join(sel.css('body ::text').getall())[:20000]

    score, signals = 0, []
    if ld & _CONTENT_LD:
        score += 2; signals.append('json-ld filme/série')
    if _YEAR.search(slug) or _YEAR.search(h1) or _YEAR.search(title):
        score += 1; signals.append('ano')
    if md.get('terms') or md.get('directors') or md.get('cast'):
        score += 1; signals.append('géneros/elenco')
    if len(set(m.lower() for m in _FICHA.findall(text))) >= 2:
        score += 1; signals.append('ficha técnica')
    if _QUALITY.search(text) or _QUALITY.search(h1) or _QUALITY.search(title):
        score += 1; signals.append('qualidade/áudio')

    why = _reason(slug) or (_reason(slugify_text(h1)) if h1 else None) or (_reason(slugify_text(title)) if title else None)
    strong_ld = ld & _NONCONTENT_LD
    if strong_ld:
        why = why or f"schema.org {sorted(strong_ld)[0]}"
    if why and score <= 1:
        return True, f"{why}; sinais de conteúdo={score}"
    return False, None
