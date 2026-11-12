# Repositório do workflow (GitHub Actions)

`/.github/workflows/process.yml` + `/scraper` (Scrapy). Este é o repositório apontado por `GH_ACCOUNT_1_REPO`
no dispatcher; o nome do workflow tem de coincidir com `GH_WORKFLOW_FILE` (`process.yml`).

Sonda: `options.dry_run` (pedido por `POST /jobs/probe` no worker) corre o mesmo crawl sem escrever em D1/API/storage e devolve uma amostra ao worker (`scraper/crawler.py`: `_flush_probe`, `diagnose_page`, `finalize_sample`).

Adaptadores: apenas `generic` (guiado por configuração, ver `scraper/adapters/generic.py`). Não existe nenhum adaptador
para sites concretos — cada fonte é descrita pela sua configuração JSON.

## Fidelidade ao site de origem (validações por item)

- **Ano**: vem do título "(AAAA)", do chip `/genero/AAAA/` e do selo do cartão da listagem (votação; empate → título). Nunca da data de publicação do artigo.
- **Ordem**: a data de publicação do site (`published_at`: JSON-LD Article/WebPage, `article:published_time`, `<time>`) define a ordem "do mais recente para o mais antigo"; sem ela, cai para a posição na listagem.
- **Tipo e categoria por item** (`scraper/validate.py`): filme/série deduzidos dos chips, do título/URL ("temporada") e dos rótulos de download; categoria = primeiro género real. A configuração da fonte só é último recurso (fica registado em `metadata.validation`).
- **Géneros/realizador/elenco** só da zona do conteúdo (depois do título, antes dos blocos "relacionados"), nunca do menu.
- **Magnets** com o grupo do site (principal/alternativo) e sem os dos blocos "relacionados". Erros (título institucional, filme com mais de 15 magnets) impedem o registo; avisos vão para `metadata.validation`.
- A sonda (`dry_run`) mostra por item: tipo e origem, categoria, origem do ano e da data, grupo de cada magnet e avisos.
- **Carrossel de destaque do site de origem**: na 1.ª página da fonte, o scraper detecta o carrossel (contentor slider/carousel/swiper/slick com 3 a 15 ligações com miniatura, sem clones), segue cada ligação, valida o destino final e envia a ordem para `POST /api/pipeline/featured`. A home do destino mostra esses itens, na mesma ordem; sem carrossel detectado, mostra os mais recentes. A sonda indica `carousel_detected` e os destinos de cada slide.
