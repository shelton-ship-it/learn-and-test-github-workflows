"""Publicação de thumbnails — MESMO mecanismo do pipeline existente:
repositórios-shard `compiledNN` na conta de storage (GitHub App), uma branch `job-<id>` por job,
ficheiros em `<job_id>/thumbs/`, servidos por jsDelivr:
    https://cdn.jsdelivr.net/gh/<owner>/<shard>@job-<id>/<job_id>/thumbs/<ficheiro>
As thumbnails são publicadas em claro, como já eram (só os segmentos de vídeo eram cifrados).

Secrets (todos já existentes): STORAGE_OWNER, STORAGE_APP_ID, STORAGE_INSTALLATION_ID,
STORAGE_PRIVATE_KEY, STORAGE_CREATE_PAT.
"""
import base64
import json
import os
import random
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

THRESHOLD_KB = 3_500 * 1024
MAX_SHARDS_TO_SCAN = 20
HARD_CEILING = 200
MAX_SHARD_ATTEMPTS = 5
TOKEN_MAX_AGE = 45 * 60
QUOTA_MARKERS = ('size quota', 'pre-receive hook declined', 'repository is above')
WORK = '/tmp/shard'


class StorageUnavailable(Exception):
    pass


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


class ThumbStore:
    def __init__(self, job_id):
        self.base_job_id = job_id
        self.batch = 0
        self.job_id = job_id          # id efectivo do lote corrente: <job>-<nnnn> (ver publish)
        self.branch = ''
        self.owner = os.environ.get('STORAGE_OWNER', '')
        self.token = None
        self.token_at = 0
        self.shard = None
        self.exclude = set()

    def _begin_batch(self):
        # Cada lote de thumbnails é publicado como um "sub-job" com a convenção existente
        # (branch job-<id>, pasta <id>/thumbs/), empurrado UMA vez e nunca alterado — evita
        # reescrever branches (e o cache de branches do jsDelivr) e o crescimento do repo local.
        self.batch += 1
        self.job_id = f'{self.base_job_id}-{self.batch:04d}'
        safe = re.sub(r'[^A-Za-z0-9._-]', '-', self.job_id)
        safe = re.sub(r'\.{2,}', '.', safe).strip('.-') or 'job'
        self.branch = f'job-{safe}'

    def configured(self):
        return all(os.environ.get(k) for k in ('STORAGE_OWNER', 'STORAGE_APP_ID', 'STORAGE_INSTALLATION_ID', 'STORAGE_PRIVATE_KEY'))

    # ── token da App (JWT RS256 -> installation token) ────────────────────
    def _ensure_token(self):
        if self.token and time.time() - self.token_at < TOKEN_MAX_AGE:
            return
        if not self.configured():
            raise StorageUnavailable('credenciais da App de storage em falta')
        now = int(time.time())
        signing = f"{_b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())}.{_b64url(json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': os.environ['STORAGE_APP_ID']}).encode())}"
        key = serialization.load_pem_private_key(os.environ['STORAGE_PRIVATE_KEY'].replace('\\n', '\n').encode(), password=None)
        jwt = f"{signing}.{_b64url(key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256()))}"
        req = urllib.request.Request(
            f"https://api.github.com/app/installations/{os.environ['STORAGE_INSTALLATION_ID']}/access_tokens", method='POST',
            headers={'Authorization': f'Bearer {jwt}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                self.token = json.loads(r.read())['token']
        except urllib.error.HTTPError as e:
            raise StorageUnavailable(f'installation token: HTTP {e.code}')
        self.token_at = time.time()
        print(f'::add-mask::{self.token}')

    def _api(self, method, path, data=None, bearer=None):
        cmd = ['curl', '-s', '-L', '-m', '30', '-w', '\n%{http_code}', '-X', method,
               '-H', f'Authorization: Bearer {bearer or self.token}', '-H', 'Accept: application/vnd.github+json',
               f'https://api.github.com{path}']
        if data is not None:
            cmd += ['-d', json.dumps(data)]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout
        body, _, code = out.rpartition('\n')
        try:
            return int(code.strip()), body
        except ValueError:
            return 0, body

    def _repo_size_kb(self, repo, attempts=4):
        for i in range(1, attempts + 1):
            code, out = self._api('GET', f'/repos/{self.owner}/{repo}')
            if code == 200:
                return True, json.loads(out).get('size', 0)
            if code == 404:
                return False, 0
            time.sleep([3, 8, 15, 25][min(i - 1, 3)])
        raise StorageUnavailable(f'GET {repo} falhou persistentemente')

    def _create_repo(self, repo):
        pat = os.environ.get('STORAGE_CREATE_PAT', '')
        if not pat:
            raise StorageUnavailable(f'STORAGE_CREATE_PAT ausente — impossível criar {repo}')
        code, body = self._api('POST', '/user/repos', {'name': repo, 'private': False, 'auto_init': True}, bearer=pat)
        if code not in (201, 422):
            raise StorageUnavailable(f'criar {repo}: HTTP {code}')

    def _pick_shard(self):
        eligible, n = [], 1
        while n <= MAX_SHARDS_TO_SCAN:
            repo = f'compiled{n:02d}'
            n += 1
            if repo in self.exclude:
                continue
            exists, size = self._repo_size_kb(repo)
            if not exists:
                self._create_repo(repo)
                return repo
            if size < THRESHOLD_KB:
                eligible.append(repo)
        if eligible:
            return random.choice(eligible)
        while n <= HARD_CEILING:
            repo = f'compiled{n:02d}'
            n += 1
            if repo in self.exclude:
                continue
            exists, size = self._repo_size_kb(repo)
            if not exists:
                self._create_repo(repo)
                return repo
            if size < THRESHOLD_KB:
                return repo
        raise StorageUnavailable('nenhum shard livre')

    # ── envio ─────────────────────────────────────────────────────────────
    def _git(self, *args, check=True):
        return subprocess.run(['git', '-c', 'user.name=catalog-bot', '-c', 'user.email=catalog-bot@users.noreply.github.com', *args],
                              capture_output=True, text=True, check=check)

    def _push(self, shard, files):
        shutil.rmtree(WORK, ignore_errors=True)
        os.makedirs(WORK)
        cwd = os.getcwd()
        try:
            os.chdir(WORK)
            self._git('init', '-q')
            self._git('checkout', '-q', '-b', self.branch)
            self._git('remote', 'add', 'origin', f'https://x-access-token:{self.token}@github.com/{self.owner}/{shard}.git')
            os.makedirs(f'{self.job_id}/thumbs', exist_ok=True)
            for local, name in files:
                shutil.copy(local, f'{self.job_id}/thumbs/{name}')
            self._git('add', self.job_id)
            self._git('commit', '-q', '-m', f'job={self.job_id} thumbs={len(files)}')
            for attempt in range(1, 9):
                r = self._git('push', '-q', '--force', 'origin', self.branch, check=False)
                out = (r.stdout + r.stderr).lower()
                if r.returncode == 0:
                    return 'ok'
                if any(m in out for m in QUOTA_MARKERS):
                    return 'quota'
                time.sleep(attempt * 8 + random.randint(0, 10))
            return 'fail'
        finally:
            os.chdir(cwd)
            shutil.rmtree(WORK, ignore_errors=True)

    def publish(self, files):
        """files: [(caminho_local, nome)] -> {nome: {'public_url','storage_key'}}. Lança StorageUnavailable."""
        if not files:
            return {}
        self._ensure_token()
        self._begin_batch()
        for _ in range(MAX_SHARD_ATTEMPTS):
            if not self.shard:
                self.shard = self._pick_shard()
            res = self._push(self.shard, files)
            if res == 'ok':
                base = f'https://cdn.jsdelivr.net/gh/{self.owner}/{self.shard}@{self.branch}/{self.job_id}/thumbs'
                return {n: {'public_url': f'{base}/{n}', 'storage_key': f'{self.owner}/{self.shard}@{self.branch}/{self.job_id}/thumbs/{n}'}
                        for _, n in files}
            if res == 'quota':
                self.exclude.add(self.shard)
                self.shard = None
                continue
            raise StorageUnavailable(f'push falhou no shard {self.shard}')
        raise StorageUnavailable('todos os shards tentados estavam cheios')
