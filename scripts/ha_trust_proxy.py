#!/usr/bin/env python3
"""Make Home Assistant trust a reverse-proxy subnet: ha_trust_proxy.py <subnet>

Since 2026.8 Home Assistant keeps its HTTP settings in .storage and reads the
http: YAML block only once. A fresh install starts HA before the Docker subnet
is known, so the block written afterwards is ignored and HA answers every
request through Caddy with 400. This goes through HA's own http/config API,
waits for the restart that applies the change, then confirms it: HA reverts
an unconfirmed change after five minutes.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from dotenv import dotenv_values  # noqa: E402

import household  # noqa: E402

subnet = sys.argv[1]
env = dotenv_values(os.path.join(HERE, "..", ".env"))
if env.get("HA_PASSWORD_MANAGED") != "true":
    sys.exit(f"HomeBrain does not hold the Home Assistant admin password. Add {subnet} "
             "as a trusted proxy in Home Assistant's network settings.")

token = household.ha_mint_token(
    env.get("HA_ADMIN_USER") or "admin",
    env.get("HA_ADMIN_PASSWORD") or env.get("MASTER_PASSWORD") or "")
stable = household.ha_ws_call(token, [{"type": "http/config"}])[0]["stable"]
conf = {k: v for k, v in stable.items() if k not in ("created_at", "error", "error_message")}
conf["use_x_forwarded_for"] = True
conf["trusted_proxies"] = conf.get("trusted_proxies", []) + [subnet]
household.ha_ws_call(token, [{"type": "http/config/configure", "config": conf}])

# HA restarts onto the change as its "pending" config. Promote only once it is
# running on it, so a change that broke HA is the one left to revert.
deadline = time.time() + 300
while True:
    time.sleep(5)
    try:
        if household.ha_ws_call(token, [{"type": "http/config"}])[0]["active_config_type"] == "pending":
            break
    except Exception:
        pass  # restarting
    if time.time() > deadline:
        sys.exit("Home Assistant did not come back on the new HTTP settings.")
household.ha_ws_call(token, [{"type": "http/config/promote"}])
print(f"Home Assistant now trusts {subnet}.")
