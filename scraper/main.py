"""Ponto de entrada do job de descoberta (GitHub Actions)."""
import json
import os
import sys

from scrapy.crawler import CrawlerProcess

from .adapters import get_adapter
from .clients import WorkerClient
from .crawler import CatalogSpider, Job, build_settings, finalize_sample


def outcome(job, spider):
    if job.fatal:
        return 'failed', job.fatal
    r = spider.stop_reason
    if job.cp_frozen and not job.dry:
        return 'failed', (f'{job.api_batches_failed} lote(s) não foram registados na API (instabilidade do EdgeOne/Turso); '
                          'o job será repetido a partir do último ponto seguro')
    if job.dry:    # sonda: terminar por limites (páginas/itens) é o normal; nunca deixa continuação
        if r == 'cancelled':
            return 'failed', 'job cancelado/expirado no worker'
        return ('done', None) if (r in ('max_pages', 'max_items', 'deadline') or spider.finished) else ('failed', 'a sonda terminou sem concluir (ver logs)')
    if r == 'deadline':
        return 'partial', None
    if r in ('max_pages', 'max_items'):
        return ('partial', None) if job.run_type == 'initial' else ('done', None)
    if r == 'cancelled':
        return 'failed', 'job cancelado/expirado no worker'
    if spider.finished:
        return 'done', None
    return 'failed', 'o crawl terminou sem concluir (ver logs)'


def main():
    job_id = os.environ['JOB_ID']
    params = json.loads(os.environ['JOB_PARAMS'])
    job = None
    spider = None
    try:
        job = Job(params, job_id)
        adapter = get_adapter(job.src['adapter'], job.cfg)
        print(f"[job] {job_id} {job.run_type} {job.src['host']} {job.src['type']}/{job.src['category']} retoma={bool(job.checkpoint)}")
        process = CrawlerProcess(build_settings(job))
        crawler = process.create_crawler(CatalogSpider)
        process.crawl(crawler, job=job, adapter=adapter)
        process.start()
        spider = crawler.spider
        status, error = outcome(job, spider)
    except Exception as e:
        status, error = 'failed', f'{type(e).__name__}: {str(e)[:300]}'
        print(f'[job] erro: {error}')

    counts = job.counts if job else {}
    cp = job.committed_cp if job else None
    sample = None
    if job and job.dry:
        try:
            sample = finalize_sample(job, spider) if spider else job.sample
        except Exception as e:
            print(f'[job] falha ao compor a amostra: {e}')
            sample = job.sample
    print(f'[job] resultado={status} contagens={counts}')
    try:
        base = (job.worker if job else WorkerClient(params.get('worker_url'), os.environ.get('ADMIN_API_KEY', '')))
        base.finish(job_id, status, counts, checkpoint=cp, error=error, sample=sample)
    except Exception as e:
        print(f'[job] não foi possível reportar a conclusão ao worker: {e}')
        sys.exit(2)
    sys.exit(0 if status != 'failed' else 1)


if __name__ == '__main__':
    main()
