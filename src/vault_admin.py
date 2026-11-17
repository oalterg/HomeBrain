"""Server-side Vaultwarden admin session shared by manager workers."""
from __future__ import annotations

import email.utils
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import requests


class VaultAdminError(Exception):
    def __init__(self, message, status=503, retry_after=0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@contextmanager
def locked(path, timeout=10):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        end = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise VaultAdminError('Another account operation is running; retry shortly')
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


class VaultAdmin:
    def __init__(self, base, token, cache=None):
        self.base = base.rstrip('/')
        self.token = token
        self.cache = Path(cache or os.environ.get(
            'HOMEBRAIN_VAULT_ADMIN_CACHE', '/run/homebrain/vault-admin.json'))
        self.identity = hashlib.sha256(f'{self.base}\0{token}'.encode()).hexdigest()

    def _state(self):
        try:
            state = read_json(self.cache)
        except (ValueError, OSError):
            state = {}
        return state if state.get('identity') == self.identity else {'identity': self.identity}

    def _cooldown(self, response, state):
        value = response.headers.get('Retry-After', '')
        try:
            seconds = int(value)
        except ValueError:
            try:
                seconds = int(email.utils.parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                seconds = 300
        seconds = max(1, min(seconds, 3600))
        state['retry_at'] = time.time() + seconds
        write_json(self.cache, state)
        raise VaultAdminError('Vault admin is rate-limited; retry after the cooldown', 429, seconds)

    def jwt(self):
        if not self.token:
            raise VaultAdminError('Vault admin is not configured')
        with locked(str(self.cache) + '.lock'):
            state = self._state()
            now = time.time()
            if state.get('retry_at', 0) > now:
                raise VaultAdminError('Vault admin is rate-limited; retry after the cooldown',
                                      429, int(state['retry_at'] - now) + 1)
            if state.get('expires', 0) > now + 30 and state.get('jwt'):
                return state['jwt']
            try:
                r = requests.post(self.base + '/admin', data={'token': self.token},
                                  allow_redirects=False, timeout=10)
            except requests.RequestException:
                raise VaultAdminError('Vault admin is unreachable') from None
            if r.status_code == 429:
                self._cooldown(r, state)
            if r.status_code not in (200, 302):
                raise VaultAdminError(f'Vault admin rejected login (HTTP {r.status_code})', 502)
            jwt = r.cookies.get('VW_ADMIN')
            if not jwt:
                raise VaultAdminError('Vault admin login returned no session', 502)
            match = re.search(r'Max-Age=(\d+)', r.headers.get('Set-Cookie', ''), re.I)
            write_json(self.cache, {'identity': self.identity, 'jwt': jwt,
                                   'expires': now + (int(match[1]) if match else 1200)})
            return jwt

    def request(self, method, path, **kwargs):
        kwargs.setdefault('timeout', 30)
        kwargs.setdefault('allow_redirects', False)
        for attempt in range(2):
            jwt = self.jwt()
            try:
                r = requests.request(method, self.base + path, cookies={'VW_ADMIN': jwt}, **kwargs)
            except requests.RequestException:
                raise VaultAdminError('Vault admin is unreachable') from None
            if r.status_code == 429:
                with locked(str(self.cache) + '.lock'):
                    self._cooldown(r, self._state())
            if r.status_code not in (401, 403):
                return r
            r.close()
            with locked(str(self.cache) + '.lock'):
                state = self._state()
                if state.get('jwt') == jwt:
                    write_json(self.cache, {'identity': self.identity})
            if attempt:
                raise VaultAdminError('Vault admin rejected authorization', 502)
        raise VaultAdminError('Vault admin rejected authorization', 502)
