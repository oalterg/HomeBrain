#!/usr/bin/env python3
"""Run the dashboard against owner-compose.yml for browser E2E via an SSH tunnel."""
import os
from pathlib import Path
import sys
root=Path(os.environ['INSTALL_DIR'])
assert root.name=='homebrain-owner-e2e'
os.environ.update(HOMEBRAIN_STATE_DIR=str(root/'state'),HOMEBRAIN_HOME=str(root/'home'),
                  HOMEBRAIN_VAULT_ADMIN_CACHE=str(root/'run/vault-admin.json'))
sys.path.insert(0,str(root/'src'))
from dotenv import dotenv_values  # noqa: E402
assert dotenv_values(root/'.env').get('HOMEBRAIN_SELF_NONCE')
assert (root/'home/.openclaw/openclaw.json').exists()
import app  # noqa: E402
import household  # noqa: E402
household.HA_URL='http://127.0.0.1:18123'
household.HA_CLIENT_ID=household.HA_URL+'/'
household.HA_WS='ws://127.0.0.1:18123/api/websocket'
app.app._vault_signups_checked=True
app.limiter.enabled=False
# Hide unrelated GPU-specific controls and use the fixture's services.
app._PLATFORM_RECORD={'arch':'x86_64','gpu_driver':'none','gpu_backend':'none','gpu_memory':'none','has_gpu':False}
app.app.run(host='127.0.0.1',port=18000,threaded=True)
