"""Behavioral regressions for LAN migration; no appliance required."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
import selftest


def test_dashboard_password_uses_private_manager_port(monkeypatch):
    calls = []
    monkeypatch.setattr(selftest, 'http', lambda *a, **kw: (calls.append(a) or (200, '')))
    assert selftest.check_dashboard_password({'MANAGER_PASSWORD': 'test'})['status'] == 'ok'
    assert calls[0][1] == 'http://127.0.0.1:8000/login'


def test_mode_switch_removes_persisted_nextcloud_hostname():
    source = (ROOT / 'scripts/common.sh').read_text()
    start = source.index('configure_nc_ha_proxy_settings() {')
    function = source[start:source.index('\n}\n', start) + 3]
    script = function + r'''
get_nc_cid() { echo nc; }
get_ha_cid() { :; }
is_local_mode() { [ "$MODE" = local ]; }
hostname() { echo 192.168.1.10; }
log_info() { :; }
log_error() { echo "$*"; }
die() { echo "$*"; exit 1; }
TRUSTED_PROXIES_0=172.16.0.0/12
TRUSTED_PROXIES_1=127.0.0.1
docker() {
 if [ "$1" = inspect ]; then echo homebrain_default; return; fi
 if [ "$1" = network ]; then echo 172.18.0.0/16; return; fi
 if [ "$1" = compose ]; then echo nc; return; fi
 if [ "$1" = exec ]; then
   shift 6
   if [ "$1" = config:system:set ] && [ "$2" = overwritehost ]; then OVERWRITEHOST="${3#--value=}"; fi
   if [ "$1" = config:system:delete ] && [ "$2" = overwritehost ]; then OVERWRITEHOST=; fi
 fi
}
OVERWRITEHOST=nc.homebrain.local
for MODE in local remote local; do
 NEXTCLOUD_TRUSTED_DOMAINS=nc.example.com
 configure_nc_ha_proxy_settings
 [ -z "$OVERWRITEHOST" ] || exit 1
done
'''
    subprocess.run(['bash'], input=script, text=True, check=True)


def test_hosts_migrates_old_managed_block_and_is_idempotent(tmp_path):
    hosts = tmp_path / 'hosts'
    hosts.write_text('127.0.0.1 localhost\n# BEGIN homebrain-lan\n127.0.0.1 nc.homebrain.local vault.homebrain.local ha.homebrain.local\n# END homebrain-lan\n')
    env = dict(os.environ, LOG_DIR=str(tmp_path / 'log'), LAN_HOSTS_FILE=str(hosts))
    subprocess.run(['bash', '-c', 'source scripts/common.sh; ensure_lan_hosts; ensure_lan_hosts'], cwd=ROOT, env=env, check=True)
    assert hosts.read_text().count('nc-homebrain.local') == 1
    assert '127.0.0.1 localhost' in hosts.read_text()


def test_mdns_republishes_on_address_change(tmp_path):
    import time
    import signal
    import shutil
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    address = tmp_path / 'address'
    address.write_text('192.168.1.10')
    log = tmp_path / 'published'
    stubs = {
        'ip': f'echo "224.0.0.251 dev eth0 src $(cat {address})"\n',
        'avahi-publish': f'echo "$*" >> {log}\nexec /bin/sleep 100\n',
        'sleep': 'exec /bin/sleep 0.05\n',
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir) + ':' + os.environ['PATH'])
    # The target uses Bash 5; Bash 3 on macOS rejects empty arrays under -u.
    bash = shutil.which('bash')
    version = subprocess.check_output([bash, '-c', 'echo ${BASH_VERSINFO[0]}'], text=True)
    if int(version) < 4:
        import pytest
        pytest.skip('Publisher runs on Linux Bash 4+')
    p = subprocess.Popen([bash, str(ROOT / 'scripts/publish_mdns.sh')], env=env, start_new_session=True)
    try:
        for ip in ('192.168.1.10', '192.168.1.20'):
            address.write_text(ip)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                lines = log.read_text().splitlines() if log.exists() else []
                if sum(line.endswith(ip) for line in lines) == 3:
                    break
                time.sleep(0.05)
            assert sum(line.endswith(ip) for line in lines) == 3
    finally:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=5)
