#!/usr/bin/env python3
"""Real-service E2E. Run only with owner-compose.yml in a disposable INSTALL_DIR.

Uses real Nextcloud, Home Assistant and Vaultwarden APIs and the Flask routes.
No service mocks. The .env must contain E2E_PASSWORD and E2E_VAULT_TOKEN.
"""
import concurrent.futures
import json
import hashlib
import base64
import multiprocessing
import os
from pathlib import Path
import sys
import time

import requests

ROOT = Path(os.environ['INSTALL_DIR'])
assert ROOT.name == 'homebrain-owner-e2e', 'Refusing to run outside the disposable stack'
os.environ['HOMEBRAIN_STATE_DIR'] = str(ROOT / 'state')
os.environ['HOMEBRAIN_HOME'] = str(ROOT / 'home')
os.environ['HOMEBRAIN_VAULT_ADMIN_CACHE'] = str(ROOT / 'run' / 'vault-admin.json')
# Prevent the dashboard's unrelated OpenClaw first-boot registration hook from running.
config = ROOT / 'home' / '.openclaw' / 'openclaw.json'
config.parent.mkdir(parents=True, exist_ok=True)
config.write_text(json.dumps({'mcp': {'servers': {'homebrain-self': {}}}}))
sys.path.insert(0, str(ROOT / 'src'))
from dotenv import dotenv_values  # noqa: E402
assert dotenv_values(ROOT/'.env').get('HOMEBRAIN_SELF_NONCE'), 'Fixture requires its own self-MCP nonce before importing the dashboard'
import app as hb  # noqa: E402
import household  # noqa: E402
import owner_vault  # noqa: E402
import vault_account  # noqa: E402
import member_escrow  # noqa: E402

household.HA_URL = 'http://127.0.0.1:18123'
household.HA_CLIENT_ID = household.HA_URL + '/'
household.HA_WS = 'ws://127.0.0.1:18123/api/websocket'
hb.app._vault_signups_checked = True  # This fixture has no MariaDB and starts signups closed.
hb.limiter.enabled = False
hb.app.config['TESTING'] = True
(ROOT / '.setup_complete').touch()
env = hb.get_env_config()
password = env['MASTER_PASSWORD']
# Keep real password-login rate limits enabled; space the crypto-heavy scenarios.


def check(condition, label):
    if not condition:
        raise AssertionError(label)
    print('PASS', label, flush=True)


def wait(url):
    for _ in range(90):
        try:
            if requests.get(url, timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError('Test service not ready: ' + url)


def vault():
    return owner_vault.OwnerVault(hb.get_env_config(), hb.update_env_var)


def client():
    c = hb.app.test_client()
    with c.session_transaction() as s:
        s['authenticated'] = True
    return c


def eventually(operation):
    for attempt in range(4):
        try:
            return operation()
        except owner_vault.OwnerVaultError as e:
            if '429' not in str(e) or attempt == 3:
                raise
            print('Waiting for the real vault login cooldown', flush=True)
            time.sleep(180)


def cipher_items(v, pw):
    auth = eventually(lambda: v.login(v.env['VAULT_OWNER_EMAIL'], pw))
    check(auth is not None, 'vault login and key unwrap')
    r = requests.get(v.base + '/api/sync', headers={'Authorization': 'Bearer ' + auth[0]['access_token']}, timeout=20)
    r.raise_for_status()
    return auth, r.json().get('ciphers', [])


wait('http://127.0.0.1:18082/alive')
wait('http://127.0.0.1:18080/status.php')
wait(household.HA_URL + '/api/onboarding')
onboarding = requests.post(household.HA_URL + '/api/onboarding/users', json={
    'name': 'Owner', 'username': 'legacyowner', 'password': password,
    'client_id': household.HA_CLIENT_ID, 'language': 'en'}, timeout=30)
if onboarding.status_code == 200:
    hb.update_env_var('HA_ADMIN_USER', 'legacyowner')

if hb.get_env_config().get('VAULT_OWNER_ID'):
    state = owner_vault.read_json(owner_vault.STATE_FILE)
    if state.get('phase') == 'rotate':
        pending = member_escrow.open_password(owner_vault.PENDING, env['RECOVERY_BACKUP_KEY'])
        eventually(lambda: vault().rotate(pending))
    eventually(lambda: vault().rotate(password))

c = client()
r = c.get('/api/household/members')
check(r.status_code == 200, 'Household API before owner vault exists')
d = r.get_json()
check(d['owner']['user'] == 'admin' and d['total'] == 1 and not d['members'], 'single owner visible and counted')
check(d['owner']['home_user'] == 'legacyowner' and not d['unmatched'], 'HA owner mapped despite different login')
r = c.post('/api/household/owner/vault', json={'action': 'create'})
check(r.status_code == 200, 'owner vault provision: ' + str(r.get_json().get('error', 'ok')))
uid = hb.get_env_config()['VAULT_OWNER_ID']
check(c.post('/api/household/owner/vault', json={'action': 'create'}).status_code == 200,
      'provisioning is idempotent')
check(len(vault().users()) == 1 and hb.get_env_config()['VAULT_OWNER_ID'] == uid, 'no duplicate vault UUID')
check(c.get('/api/household/members').get_json()['owner']['vault'] is True, 'owner vault displayed as linked')
check(hb.app.test_client().post('/api/household/owner/vault', json={'action': 'create'}).status_code in (401, 302),
      'unauthenticated owner mutation rejected')
for suffix, method in [('', 'delete'), ('/password', 'post'), ('/services', 'post')]:
    check(getattr(c, method)('/api/household/members/Admin' + suffix, json={}).status_code == 400,
          'case-insensitive owner protection ' + suffix)
try:
    hb.pairing_payload('Admin', password, hb.get_env_config())
except hb.NextcloudError:
    check(True, 'admin phone QR remains prohibited')
else:
    raise AssertionError('Admin pairing was allowed')

check(c.post('/api/household/members/legacyowner/password', json={}).status_code == 400,
      'differently named HA owner cannot be reset through member endpoint')
v = vault()
time.sleep(30)
# Resume a previous test run which stopped after remote rotation.
if v.env.get('VAULT_OWNER_ID'):
    eventually(lambda: v.rotate(password))
time.sleep(15)
auth = v.login(v.env['VAULT_OWNER_EMAIL'], password)
fixture = 'owner-e2e-secret-value'
# Vaultwarden 1.37.5 rejects a cipher without encryptedFor (HTTP 422).
item = {'type': 1, 'encryptedFor': v.env['VAULT_OWNER_ID'],
        'name': vault_account.encstring2(b'Owner E2E', auth[2]),
        'notes': None, 'favorite': False, 'reprompt': 0,
        'login': {'username': vault_account.encstring2(b'owner', auth[2]),
                  'password': vault_account.encstring2(fixture.encode(), auth[2]), 'uris': []}}
r = requests.post(v.base + '/api/ciphers', json=item,
                  headers={'Authorization': 'Bearer ' + auth[0]['access_token']}, timeout=20)
check(r.status_code == 200, 'encrypted fixture saved')
cipher_id = r.json()['id']
attachment_key = os.urandom(64)
encrypted = vault_account.encstring2(b'owner-attachment-fixture', attachment_key)
iv, ciphertext, mac = (base64.b64decode(p) for p in encrypted[2:].split('|'))
blob = bytes([2]) + iv + mac + ciphertext
headers = {'Authorization': 'Bearer ' + auth[0]['access_token']}
attachment = requests.post(v.base + '/api/ciphers/' + cipher_id + '/attachment/v2', headers=headers,
    json={'key': vault_account.encstring2(attachment_key, auth[2]),
          'fileName': vault_account.encstring2(b'fixture.txt', auth[2]), 'fileSize': len(blob)}, timeout=30)
attachment.raise_for_status()
upload = requests.post(v.base + '/api' + attachment.json()['url'], headers=headers,
                       files={'data': ('fixture', blob)}, timeout=30)
upload.raise_for_status()
check(True, 'encrypted attachment uploaded through the vault API')
new = 'e2e-rotated-owner-password-2026'
eventually(lambda: v.rotate(new))
time.sleep(30)
auth2, items = cipher_items(vault(), new)
check(auth2[2] == auth[2] and any(vault_account.decstring2(i['login']['password'], auth2[2]).decode() == fixture
                               for i in items if i.get('login')), 'rotation preserves readable encrypted items')
check(v.login(v.env['VAULT_OWNER_EMAIL'], password) is None, 'old vault password rejected')
# Resume the exact state after remote rotation succeeded but its journal has not been cleared.
member_escrow.seal(owner_vault.PENDING, new, env['RECOVERY_BACKUP_KEY'])
owner_vault.write_json(owner_vault.STATE_FILE, {'phase': 'rotate', 'vault_id': uid,
                                            'uid': 'admin', 'email': v.env['VAULT_OWNER_EMAIL'],
                                            'key_hash': hashlib.sha256(auth2[2]).hexdigest()})
eventually(lambda: vault().rotate(new))
check(not owner_vault.STATE_FILE.exists(), 'interrupted rotation resumes without replacing encryption key')
eventually(lambda: vault().rotate(password))
def discover_users(_):
    return vault().users()

Path(os.environ['HOMEBRAIN_VAULT_ADMIN_CACHE']).unlink(missing_ok=True)
with concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context('fork')) as pool:
    results = list(pool.map(discover_users, range(16)))
check(all(len(x) == 1 for x in results), 'concurrent vault discovery works without rate limiting')
with c.session_transaction() as session:
    check('vault_admin_jwt' not in session, 'admin token is server-side')
(ROOT / 'e2e-owner-result.json').write_text(json.dumps({'status': 'passed', 'owner_id': uid}))
print('OWNER_E2E_COMPLETE', flush=True)
