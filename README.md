# Repositório do workflow (GitHub Actions)

`/.github/workflows/process.yml` + `/scraper` (Scrapy). Este é o repositório apontado por `GH_ACCOUNT_1_REPO`
no dispatcher; o nome do workflow tem de coincidir com `GH_WORKFLOW_FILE` (`process.yml`).

Sonda: `options.dry_run` (pedido por `POST /jobs/probe` no worker) corre o mesmo crawl sem escrever em D1/API/storage e devolve uma amostra ao worker (`scraper/crawler.py`: `_flush_probe`, `diagnose_page`, `finalize_sample`).

Adaptadores: apenas `generic` (guiado por configuração, ver `scraper/adapters/generic.py`). Não existe nenhum adaptador
para sites concretos — cada fonte é descrita pela sua configuração JSON.
