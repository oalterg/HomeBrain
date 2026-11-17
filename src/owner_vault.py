"""Managed owner vault: identity, provisioning, and recoverable password changes."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import requests

import member_escrow
import vault_account
from vault_admin import VaultAdmin, VaultAdminError, locked, read_json, write_json

STATE_DIR = Path(os.environ.get('HOMEBRAIN_STATE_DIR', '/var/lib/homebrain'))
STATE_FILE = STATE_DIR / 'owner_vault.json'
LOCK_FILE = STATE_DIR / 'accounts.lock'
PENDING = 'owner-vault:pending'


class OwnerVaultError(Exception):
    pass


def account_lock():
    return locked(LOCK_FILE, timeout=1)


class OwnerVault:
    def __init__(self, env, save):
        self.env, self.save = env, save
        self.uid = env.get('NEXTCLOUD_ADMIN_USER', 'admin')
        self.base = 'http://127.0.0.1:' + env.get('VAULT_PORT', '8082')
        token = hashlib.sha256(f"{env.get('MASTER_PASSWORD', '')}:{env.get('VAULT_ADMIN_NONCE', '')}".encode()).hexdigest()
        self.admin = VaultAdmin(self.base, token if env.get('VAULT_ADMIN_NONCE') else '')

    def users(self):
        r = self.admin.request('GET', '/admin/users')
        if r.status_code != 200:
            raise OwnerVaultError(f'Could not read vault accounts (HTTP {r.status_code})')
        try:
            data = r.json()
        except ValueError:
            raise OwnerVaultError('Vault returned an invalid account list') from None
        if isinstance(data, dict):
            data = data.get('users', data.get('data'))
        if not isinstance(data, list):
            raise OwnerVaultError('Vault returned an invalid account list')
        return data

    def bound_user(self):
        uid = self.env.get('VAULT_OWNER_ID')
        user = next((u for u in self.users() if u.get('id') == uid), None)
        if not uid or not user or user.get('email', '').lower() != self.env.get('VAULT_OWNER_EMAIL', '').lower():
            raise OwnerVaultError('Owner vault is missing or not linked; link the existing account first')
        return user

    def login(self, email, password):
        r = requests.post(self.base + '/identity/accounts/prelogin', json={'email': email}, timeout=15)
        if r.status_code != 200:
            raise OwnerVaultError('Could not read vault key settings')
        kdf = r.json()
        if kdf.get('kdf', 0) != vault_account.KDF_PBKDF2:
            raise OwnerVaultError('This vault uses unsupported key settings; link it as independently managed')
        iterations = kdf.get('kdfIterations', vault_account.KDF_ITERATIONS)
        digest, mk = vault_account.master_password_hash(password, email, iterations)
        r = requests.post(self.base + '/identity/connect/token', data={
            'grant_type': 'password', 'username': email, 'password': digest,
            'scope': 'api offline_access', 'client_id': 'web', 'deviceType': '9',
            'deviceIdentifier': 'homebrain-owner', 'deviceName': 'HomeBrain owner'},
            headers={'Bitwarden-Client-Version': '2024.6.0'}, timeout=30)
        if r.status_code == 400:
            if r.json().get('TwoFactorProviders') or r.json().get('twoFactorProviders'):
                raise OwnerVaultError('Vault requires two-factor authentication; use independently managed linkage')
            return None
        if r.status_code != 200:
            raise OwnerVaultError(f'Vault login could not be verified (HTTP {r.status_code})')
        data = r.json()
        key = data.get('Key') or data.get('key')
        if not data.get('access_token') or not key:
            raise OwnerVaultError('Vault did not return a usable account key')
        raw = vault_account.decstring2(key, vault_account.stretch(mk))
        if len(raw) != 64:
            raise OwnerVaultError('Vault account key failed verification')
        return data, iterations, raw

    def _key(self):
        key = self.env.get('RECOVERY_BACKUP_KEY')
        if not key:
            raise OwnerVaultError('Enable recovery before creating or managing the owner vault')
        return key

    def _bind(self, user, managed):
        for key, value in [('VAULT_OWNER_ID', user['id']), ('VAULT_OWNER_EMAIL', user['email']),
                           ('VAULT_OWNER_PASSWORD_MANAGED', 'true' if managed else 'false')]:
            self.save(key, value)
            self.env[key] = value

    def provision(self, already_locked=False):
        if self.env.get('VAULT_ENABLED', 'true') == 'false':
            return {'status': 'skipped'}
        if not already_locked:
            with account_lock():
                return self.provision(already_locked=True)
        state = read_json(STATE_FILE)
        if self.env.get('VAULT_OWNER_ID') and state.get('phase') != 'provision':
            self.bound_user()
            return self.verify()
        key = self._key()
        email = vault_account.vault_email(self.uid)
        if state and (state.get('uid') != self.uid or state.get('email') != email):
            raise OwnerVaultError('An unfinished owner vault operation needs recovery first')
        existing = next((u for u in self.users() if u.get('email', '').lower() == email), None)
        if existing and not state:
            raise OwnerVaultError('That vault email already exists; explicitly link it instead')
        if not state:
            member_escrow.seal(self.uid, self.env['MASTER_PASSWORD'], key)
            state = {'phase': 'provision', 'uid': self.uid, 'email': email}
            write_json(STATE_FILE, state)
        password = member_escrow.open_password(self.uid, key)
        if not existing:
            r = self.admin.request('POST', '/admin/invite', json={'email': email})
            if r.status_code not in (200, 201):
                raise OwnerVaultError(f'Vault invite failed (HTTP {r.status_code})')
        # An invite creates an unregistered row. Registration is needed until a key exists.
        verified = self.login(email, password) if existing else None
        if not verified:
            payload, _ = vault_account.register_payload(self.uid, self.uid, password)
            r = requests.post(self.base + '/identity/accounts/register', json=payload, timeout=60)
            if r.status_code not in (200, 201):
                raise OwnerVaultError('Vault registration is incomplete; existing account was not reset')
            verified = self.login(email, password)
        if not verified:
            raise OwnerVaultError('Created vault could not be verified')
        user = next(u for u in self.users() if u.get('email', '').lower() == email)
        self._bind(user, True)
        self.save('VAULT_SIGNUPS_ALLOWED', 'false')
        STATE_FILE.unlink(missing_ok=True)
        return {'status': 'ready', 'email': email, 'managed': True}

    def link(self, uid, password='', managed=False):
        with account_lock():
            if read_json(STATE_FILE):
                raise OwnerVaultError('Finish the pending owner vault operation before linking')
            user = next((u for u in self.users() if u.get('id') == uid), None)
            if not user:
                raise OwnerVaultError('Vault account no longer exists')
            if managed:
                key = self._key()
                if not password or not self.login(user['email'], password):
                    raise OwnerVaultError('A working vault password is required to manage this account')
                member_escrow.seal(self.uid, password, key)
            self._bind(user, managed)
            return {'status': 'linked', 'email': user['email'], 'managed': managed}

    def verify(self):
        user = self.bound_user()
        managed = self.env.get('VAULT_OWNER_PASSWORD_MANAGED') == 'true'
        if managed:
            password = member_escrow.open_password(self.uid, self._key())
            if not self.login(user['email'], password):
                raise OwnerVaultError('Owner vault password changed independently; relink with its current password')
        return {'status': 'ready' if managed else 'independently_managed',
                'email': user['email'], 'managed': managed}

    def rotate(self, new_password, already_locked=False):
        if not already_locked:
            with account_lock():
                return self.rotate(new_password, already_locked=True)
        if self.env.get('VAULT_OWNER_PASSWORD_MANAGED') != 'true':
            return {'status': 'skipped', 'reason': 'Owner vault manages its own password or is not linked'}
        user = self.bound_user()
        key = self._key()
        state = read_json(STATE_FILE)
        if state and (state.get('phase') != 'rotate' or state.get('vault_id') != user['id']):
            raise OwnerVaultError('Another owner vault operation needs recovery first')
        old = member_escrow.open_password(self.uid, key)
        if not state and old == new_password:
            return self.verify()
        resuming = bool(state)
        if not state:
            # Keep old/new sealed until the remote response and persisted state agree.
            member_escrow.seal(PENDING, new_password, key)
            state = {'phase': 'rotate', 'vault_id': user['id'], 'uid': self.uid,
                     'email': user['email']}
            write_json(STATE_FILE, state)
        pending = member_escrow.open_password(PENDING, key)
        if pending != new_password:
            raise OwnerVaultError('Resume the pending password change before choosing another password')
        verified = self.login(user['email'], pending) if resuming else None
        if verified and state.get('key_hash') and hashlib.sha256(verified[2]).hexdigest() != state['key_hash']:
            raise OwnerVaultError('Resumed vault key verification failed; recovery checkpoint retained')
        if not verified:
            current = self.login(user['email'], old)
            if not current:
                raise OwnerVaultError('Owner vault password changed independently; no reset was attempted')
            data, iterations, raw_key = current
            state['key_hash'] = hashlib.sha256(raw_key).hexdigest()
            write_json(STATE_FILE, state)
            payload = vault_account.password_change_payload(
                old, pending, user['email'], data.get('Key') or data.get('key'), kdf_iterations=iterations)
            r = requests.post(self.base + '/api/accounts/password', json=payload,
                              headers={'Authorization': 'Bearer ' + data['access_token'],
                                       'Bitwarden-Client-Version': '2024.6.0'}, timeout=30)
            if r.status_code != 200:
                raise OwnerVaultError(f'Owner vault password change failed (HTTP {r.status_code}); retry to resume')
            verified = self.login(user['email'], pending)
            if not verified or verified[2] != raw_key:
                raise OwnerVaultError('Owner vault key verification failed; recovery checkpoint retained')
        member_escrow.seal(self.uid, pending, key)
        STATE_FILE.unlink(missing_ok=True)
        member_escrow.drop(PENDING)
        return {'status': 'ready', 'email': user['email'], 'managed': True}


def main():
    import argparse
    import json
    import sys
    from dotenv import dotenv_values, set_key
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['provision', 'verify', 'rotate', 'restore', 'snapshot', 'restore-binding'])
    parser.add_argument('--password-file')
    parser.add_argument('--directory')
    args = parser.parse_args()
    env_file = Path(os.environ.get('INSTALL_DIR', '/opt/homebrain')) / '.env'
    def save(key, value):
        with locked(str(env_file) + '.lock'):
            set_key(str(env_file), key, value)
            env_file.chmod(0o600)
    vault = OwnerVault(dict(dotenv_values(env_file)), save)
    try:
        if args.action == 'snapshot':
            directory = Path(args.directory)
            binding = {k: vault.env.get(k, '') for k in
                       ('VAULT_OWNER_ID', 'VAULT_OWNER_EMAIL', 'VAULT_OWNER_PASSWORD_MANAGED')}
            write_json(directory / 'owner_vault_binding.json', binding)
            if STATE_FILE.exists():
                write_json(directory / 'owner_vault_pending.json', read_json(STATE_FILE))
            return 0
        if args.action == 'restore-binding':
            directory = Path(args.directory)
            binding = read_json(directory / 'owner_vault_binding.json')
            for k in ('VAULT_OWNER_ID', 'VAULT_OWNER_EMAIL', 'VAULT_OWNER_PASSWORD_MANAGED'):
                save(k, str(binding.get(k, '')))
            pending = read_json(directory / 'owner_vault_pending.json')
            if pending:
                write_json(STATE_FILE, pending)
            else:
                STATE_FILE.unlink(missing_ok=True)
            return 0
        if args.action in ('rotate', 'restore'):
            password = Path(args.password_file).read_text().strip() if args.password_file else vault.env['MASTER_PASSWORD']
            already_locked = os.environ.get('HOMEBRAIN_ACCOUNTS_LOCKED') == '1'
            if args.action == 'restore' and read_json(STATE_FILE).get('phase') == 'provision':
                vault.provision(already_locked=already_locked)
            if args.action == 'restore' and read_json(STATE_FILE).get('phase') == 'rotate':
                pending = member_escrow.open_password(PENDING, vault._key())
                vault.rotate(pending, already_locked=already_locked)
            result = vault.rotate(password, already_locked=already_locked)
        else:
            result = getattr(vault, args.action)()
        print(json.dumps(result))
    except (OwnerVaultError, VaultAdminError, member_escrow.EscrowError, vault_account.VaultAccountError,
            requests.RequestException) as exc:
        print(f'Owner vault: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
