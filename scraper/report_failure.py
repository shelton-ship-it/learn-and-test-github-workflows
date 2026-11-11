"""Último recurso: se o processo morreu sem reportar, marca o job como falhado (idempotente no worker)."""
import json
import os

from .clients import WorkerClient

try:
    params = json.loads(os.environ['JOB_PARAMS'])
    WorkerClient(params.get('worker_url'), os.environ.get('ADMIN_API_KEY', '')).finish(
        os.environ['JOB_ID'], 'failed', {}, error='workflow terminou sem reportar (falha/cancelamento do runner)')
    print('falha reportada ao worker')
except Exception as e:
    print(f'não foi possível reportar a falha: {e}')
