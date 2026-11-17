#!/usr/bin/env python3
"""Restore an actual Vaultwarden SQLite backup onto a separate disposable instance.

Run after owner_accounts.py. Uses a different master password and recovery key;
asserts existing encrypted items remain readable after rewrap and recovery reset.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time

from dotenv import dotenv_values
import requests

root = Path(os.environ['INSTALL_DIR'])
assert root.name == 'homebrain-owner-e2e'
source_env = dict(dotenv_values(root/'.env'))
restore = root/'restored'
restore.mkdir(mode=0o700, exist_ok=True)
state = restore/'state'
state.mkdir(mode=0o700, exist_ok=True)
source_state = root/'state'
assert not (source_state/'owner_vault.json').exists(), 'Complete pending rotation first'
snapshot = restore/'snapshot'
snapshot.mkdir(mode=0o700, exist_ok=True)
cli = [sys.executable,str(root/'src/owner_vault.py')]
process_env = dict(os.environ,HOMEBRAIN_STATE_DIR=str(source_state),HOMEBRAIN_HOME=str(root/'home'),
                   HOMEBRAIN_VAULT_ADMIN_CACHE=str(root/'run/vault-admin.json'))
subprocess.run(cli+['snapshot','--directory',str(snapshot)],env=process_env,check=True)
inspect=json.loads(subprocess.check_output(['docker','inspect','homebrain-owner-e2e-vaultwarden-1']))[0]
volume=Path(next(m['Source'] for m in inspect['Mounts'] if m['Destination']=='/data'))
data=restore/'vault-data'
if data.exists():raise RuntimeError('Use a new disposable stack for another restore test')
shutil.copytree(volume,data,ignore=shutil.ignore_patterns('db.sqlite3*'))
with sqlite3.connect(volume/'db.sqlite3') as src, sqlite3.connect(data/'db.sqlite3') as dest:
    src.backup(dest)
new_password=secrets.token_urlsafe(24);nonce=secrets.token_hex(16)
new_key=base64.b64encode(secrets.token_bytes(32)).decode()
env=dict(source_env,MASTER_PASSWORD=new_password,MANAGER_PASSWORD=new_password,VAULT_PORT='18083',
         RECOVERY_BACKUP_KEY=new_key,VAULT_ADMIN_NONCE=nonce)
(restore/'.env').write_text(''.join(f'{k}={v}\n' for k,v in env.items()))
(restore/'.env').chmod(0o600)
wrap=snapshot/'wrap';wrap.write_text(source_env['RECOVERY_BACKUP_KEY']);wrap.chmod(0o600)
dest_key=snapshot/'dest-key';dest_key.write_text(new_key);dest_key.chmod(0o600)
subprocess.run([sys.executable,str(root/'src/member_escrow.py'),'restore-rewrap',
                '--json',str(source_state/'member_escrow.json'),'--wrap-file',str(wrap),
                '--dest-key-file',str(dest_key),'--out',str(state/'member_escrow.json')],check=True)
wrap.unlink();dest_key.unlink()
token=hashlib.sha256(f'{new_password}:{nonce}'.encode()).hexdigest()
r=subprocess.run(['docker','run','-d','--name','homebrain-owner-e2e-restored',
    '--label','homebrain.e2e=owner','-p','127.0.0.1:18083:80','-v',str(data)+':/data',
    '-e','ADMIN_TOKEN='+token,'-e','SIGNUPS_ALLOWED=false','-e','ROCKET_PORT=80',
    'vaultwarden/server:1.37.2'],capture_output=True)
assert r.returncode==0,'Could not start restore fixture'
for _ in range(60):
    try:
        if requests.get('http://127.0.0.1:18083/alive',timeout=2).status_code==200:break
    except requests.RequestException:pass
    time.sleep(1)
process_env.update(INSTALL_DIR=str(restore),HOMEBRAIN_STATE_DIR=str(state),
                   HOMEBRAIN_VAULT_ADMIN_CACHE=str(restore/'runtime/vault-admin.json'))
for action in [['restore-binding','--directory',str(snapshot)],['restore'],['verify']]:
    result=subprocess.run(cli+action,env=process_env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
# Verify the ciphertext itself via the destination's real login/sync APIs.
os.environ.update(process_env)
sys.path.insert(0,str(root/'src'))
from owner_vault import OwnerVault  # noqa: E402
import vault_account  # noqa: E402
v=OwnerVault(dict(dotenv_values(restore/'.env')),lambda k,v:None)
auth=v.login(v.env['VAULT_OWNER_EMAIL'],new_password)
assert auth is not None
r=requests.get(v.base+'/api/sync',headers={'Authorization':'Bearer '+auth[0]['access_token']},timeout=20)
r.raise_for_status()
assert any(vault_account.decstring2(i['login']['password'],auth[2]).decode()=='owner-e2e-secret-value'
           for i in r.json()['ciphers'] if i.get('login'))
print('PASS cross-instance restore: owner identity, rewrapped recovery credential, new password, decrypted item')
