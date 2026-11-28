#!/usr/bin/env python3
"""Device-setup tools of the HA MCP against a live Home Assistant.

Run on the box as homebrain, with an entity that belongs to a device:
    sudo -u homebrain python3 scripts/e2e/ha_device_setup.py vacuum.x [--write] [--zigbee]
Without flags nothing changes: flows are always aborted. --write renames the
entity id and back; --zigbee opens joining for 10 s. HA_MCP=<path> tests a
server file other than the one wired in openclaw.json (with
PYTHONPATH=/opt/homebrain/scripts when it is not next to mcp_common.py).
"""
import json
import os
from pathlib import Path
import subprocess
import sys

entity = sys.argv[1]
flags = set(sys.argv[2:])
spec = json.loads((Path.home()/'.openclaw/openclaw.json').read_text())['mcp']['servers']['homebrain-homeassistant']
args = [os.environ.get('HA_MCP', spec['args'][0]), *spec['args'][1:]]
srv = subprocess.Popen([spec['command'], *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True, env=dict(os.environ, **spec.get('env', {})))
rid = 0


def rpc(method, params):
    global rid
    rid += 1
    srv.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})+'\n')
    srv.stdin.flush()
    while True:
        line = srv.stdout.readline()
        if not line:
            sys.exit('FAIL the MCP server exited; run it by hand to see why')
        msg = json.loads(line)
        if msg.get('id') == rid:
            return msg['result']


def call(tool, **a):
    return json.loads(rpc('tools/call', {'name': tool, 'arguments': a})['content'][0]['text'])


def abort_if_open(r, name):
    if r.get('type') in ('form', 'menu'):
        check(name, call('ha.integration_setup', flow_id=r['flow_id'], abort=True).get('aborted'))


def check(name, cond, detail=''):
    print(('ok   ' if cond else 'FAIL ')+name+('' if cond else f': {str(detail)[:300]}'))
    if not cond:
        sys.exit(1)


rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'e2e', 'version': '0'}})
names = {t['name'] for t in rpc('tools/list', {})['tools']}
check('tools listed', {'ha.integration_setup', 'ha.zigbee_join', 'ha.new_devices', 'ha.device_update',
                       'ha.device_remove', 'ha.diagnostics'} <= names, names)

r = call('ha.integration_setup')
check('lists discovered + configurable', r.get('ok') and 'configurable' in r, r)
configurable = r['configurable']
r = call('ha.integration_setup', domain='no_such_integration_e2e')
check('unknown domain is refused', not r.get('ok') and 'no integration' in r.get('error', ''), r)

# A new config flow shows HA's own field labels, then is aborted.
r = call('ha.integration_setup', domain='met')
check('config flow starts', r.get('ok') and r.get('type') in ('form', 'abort'), r)
if r.get('type') == 'form':
    check('fields carry labels', any(f.get('label') for f in r['fields']), r['fields'])
abort_if_open(r, 'config flow aborts')

# An options flow shows current values but never a stored secret, then is aborted.
for entry in configurable[:3]:
    r = call('ha.integration_setup', entry_id=entry['entry_id'])
    check(f"options flow opens ({entry['domain']})", r.get('ok') and r.get('type') in ('form', 'menu', 'abort'), r)
    leaked = [f['name'] for f in r.get('fields', []) if f.get('secret') and f.get('default') not in (None, '')]
    check(f"no stored secret shown ({entry['domain']})", not leaked, leaked)
    abort_if_open(r, f"options flow aborts ({entry['domain']})")

r = call('ha.new_devices', minutes=7*24*60)
check('new_devices answers', r.get('ok') and isinstance(r.get('devices'), list), r)
r = call('ha.diagnostics', device_id=entity)
check('device found by entity id', r.get('ok') or 'offers no device diagnostics' in r.get('error', ''), r)
r = call('ha.history', entity_id=entity, hours=1)
check('history answers', r.get('ok'), r)

domain = entity.split('.')[0]
other = 'sensor' if domain != 'sensor' else 'light'
r = call('ha.device_update', device_id=entity, entity_ids={entity: f'{other}.e2e'})
check('rename across domains is refused', not r.get('ok') and 'domain' in r.get('error', ''), r)
r = call('ha.device_update', device_id=entity, entity_ids={f'{domain}.not_this_device_e2e': f'{domain}.x'})
check("another device's entity is refused", not r.get('ok') and 'not an entity' in r.get('error', ''), r)

if '--write' in flags:
    tmp = entity+'_e2e'
    r = call('ha.device_update', device_id=entity, entity_ids={entity: tmp})
    check('entity id renamed', r.get('ok') and (r.get('renamed') or {}).get(entity) == tmp, r)
    check('state follows the new id', call('ha.state', entity_id=tmp).get('ok'))
    r = call('ha.device_update', device_id=tmp, entity_ids={tmp: entity})
    check('entity id restored', r.get('ok') and (r.get('renamed') or {}).get(tmp) == entity, r)

if '--zigbee' in flags:
    r = call('ha.zigbee_join', seconds=10)
    check('zigbee joining opens (or ZHA absent)', r.get('ok') or 'not set up' in r.get('error', ''), r)

srv.stdin.close()
srv.wait(timeout=10)
print('PASS')
