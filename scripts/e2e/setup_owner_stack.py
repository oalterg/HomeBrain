#!/usr/bin/env python3
"""Prepare only the disposable owner-compose project; never reads production secrets."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time

import requests

root = Path(os.environ['INSTALL_DIR']).resolve()
assert root in (Path('/tmp/homebrain-owner-e2e'),Path('/var/lib/homebrain-owner-e2e')), 'Use a dedicated disposable install directory'
root.mkdir(mode=0o700, exist_ok=True)
if (root/'.env').exists():
    raise SystemExit('Fixture already exists; reuse it or explicitly remove its compose volumes first')
existing = subprocess.check_output(['docker','volume','ls','-q','--filter','label=com.docker.compose.project=homebrain-owner-e2e'],text=True)
if existing.strip():
    raise SystemExit('Disposable volumes already exist; do not pair a new .env with old service data')
password = secrets.token_urlsafe(24)
nonce = secrets.token_hex(16)
env = dict(MASTER_PASSWORD=password, MANAGER_PASSWORD=password, NEXTCLOUD_ADMIN_USER='admin',
           NEXTCLOUD_ADMIN_PASSWORD=password, HA_ADMIN_USER='legacyowner', HA_ADMIN_PASSWORD=password,
           HA_PASSWORD_MANAGED='true', VAULT_ENABLED='true', VAULT_PORT='18082', NEXTCLOUD_PORT='18080',
           VAULT_SIGNUPS_ALLOWED='false', VAULT_ADMIN_NONCE=nonce, HAS_GPU='false',
           RECOVERY_BACKUP_KEY=base64.b64encode(secrets.token_bytes(32)).decode(),
           HOMEBRAIN_SELF_NONCE=secrets.token_hex(16), E2E_PASSWORD=password, E2E_VAULT_TOKEN=hashlib.sha256(f'{password}:{nonce}'.encode()).hexdigest())
(root/'.env').write_text(''.join(f'{k}={v}\n' for k,v in env.items()))
(root/'.env').chmod(0o600)
shutil.copy2(root/'scripts/e2e/owner-compose.yml',root/'docker-compose.yml')
# Avoid unrelated startup registration against the host's OpenClaw service user.
config=root/'home/.openclaw/openclaw.json'
config.parent.mkdir(parents=True,exist_ok=True)
config.write_text(json.dumps({'mcp':{'servers':{'homebrain-self':{}}}}))
(root/'.setup_complete').touch()
subprocess.run(['docker','compose','--project-directory',str(root),'up','-d'],check=True)
for _ in range(180):
    try:
        data=requests.get('http://127.0.0.1:18080/status.php',timeout=2).json()
        if data.get('installed'):break
    except (requests.RequestException,ValueError):pass
    time.sleep(2)
else:raise SystemExit('Nextcloud fixture did not finish installation')
app=os.environ.get('PASSWORDS_APP_DIR')
if app:
    subprocess.run(['docker','cp',app,'homebrain-owner-e2e-nextcloud-1:/var/www/html/custom_apps/passwords'],check=True)
    subprocess.run(['docker','exec','homebrain-owner-e2e-nextcloud-1','chown','-R','www-data:www-data','/var/www/html/custom_apps/passwords'],check=True)
    subprocess.run(['docker','exec','-u','www-data','homebrain-owner-e2e-nextcloud-1','php','occ','app:enable','passwords'],check=True)
print('Disposable owner stack ready. Run owner_accounts.py before the migration fixtures.',flush=True)
