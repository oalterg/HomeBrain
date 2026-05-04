#!/usr/bin/env python3
"""HomeBrain Home Assistant MCP server.

Exposes a small, allowlisted slice of the HA REST API to OpenClaw. Talks to
HA over its native HTTP API using a long-lived access token (LLAT) that the
HomeBrain dashboard provisions automatically.

Why this shim instead of HA core's official `mcp_server` integration:
  * Predictable behaviour across HA versions (the official one moves fast).
  * Allowlist enforcement at the MCP layer — we deny destructive domains
    (`homeassistant.restart`, `recorder.*`, etc.) before they ever hit HA.
  * The dashboard already owns the LLAT lifecycle; reusing it keeps the
    "one root of identity" principle (see INTEGRATIONS_PLAN.md §1.2).

Environment:
  HA_BASE_URL    e.g. http://homeassistant:8123 or http://localhost:8123
  HA_TOKEN       long-lived access token (read from a file by the dashboard
                 and injected here via openclaw mcp set's `env` block)
  HA_TOKEN_FILE  optional alternative — file path containing the token,
                 mode 0600. Wins over HA_TOKEN if set.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
import json
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    Consent, audit, consent_required, err, ok, serve, unavailable,
)

HA_BASE_URL = os.environ.get("HA_BASE_URL", "http://localhost:8123").rstrip("/")
HA_TOKEN_FILE = os.environ.get("HA_TOKEN_FILE", "")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# Domains the agent is permitted to call services on. Critical/destructive
# domains intentionally absent. This list is the trust boundary; do not
# expand it without thinking about blast radius.
SERVICE_DOMAIN_ALLOWLIST = {
    "light", "switch", "fan", "cover", "climate", "media_player",
    "vacuum", "lock", "scene", "script", "automation", "input_boolean",
    "input_number", "input_select", "input_text", "input_button",
    "button", "notify", "humidifier", "water_heater", "lawn_mower",
    "remote", "siren", "valve",
}

# Services we deny even within an allowed domain (e.g. "automation.delete").
SERVICE_NAME_DENYLIST = {"delete", "remove", "clear_skipped_update", "purge"}


def _token() -> str:
    if HA_TOKEN_FILE and os.path.exists(HA_TOKEN_FILE):
        try:
            return open(HA_TOKEN_FILE).read().strip()
        except OSError:
            return ""
    return HA_TOKEN.strip()


def _http(method: str, path: str, body: Any = None, timeout: int = 8) -> tuple[int, dict | list | str]:
    tok = _token()
    if not tok:
        return 0, "no HA token configured"
    url = f"{HA_BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except urllib.error.URLError as e:
        return 0, str(e)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def t_health(_args: dict) -> dict:
    code, body = _http("GET", "/api/")
    if code == 200:
        return ok(message=body.get("message") if isinstance(body, dict) else "")
    return unavailable(f"HA at {HA_BASE_URL} unreachable: {body}")


def t_entity_search(args: dict) -> dict:
    """Search entity_ids and friendly names by free-text query."""
    q = (args.get("query") or "").lower().strip()
    if not q:
        return err("query is required")
    code, body = _http("GET", "/api/states")
    if code != 200 or not isinstance(body, list):
        return unavailable(f"HA states unreachable: {body}")
    matches = []
    for s in body:
        eid = s.get("entity_id", "")
        name = (s.get("attributes") or {}).get("friendly_name", "") or ""
        if q in eid.lower() or q in name.lower():
            matches.append({
                "entity_id": eid,
                "name": name,
                "state": s.get("state"),
                "domain": eid.split(".", 1)[0] if "." in eid else "",
            })
            if len(matches) >= 50:
                break
    return ok(results=matches, total=len(matches))


def t_state(args: dict) -> dict:
    eid = args.get("entity_id") or ""
    if not eid:
        return err("entity_id is required")
    code, body = _http("GET", f"/api/states/{eid}")
    if code == 404:
        return err("entity not found")
    if code != 200 or not isinstance(body, dict):
        return unavailable(f"HA unreachable: {body}")
    return ok(
        entity_id=body.get("entity_id"),
        state=body.get("state"),
        attributes=body.get("attributes") or {},
        last_changed=body.get("last_changed"),
    )


def t_area_list(_args: dict) -> dict:
    """Use the /api/template endpoint to enumerate areas, since HA has no
    REST endpoint for the area registry itself."""
    code, body = _http("POST", "/api/template",
                       {"template": "{{ areas() | list | tojson }}"})
    if code != 200 or not isinstance(body, str):
        return unavailable(f"HA template eval failed: {body}")
    try:
        ids = json.loads(body)
    except json.JSONDecodeError:
        return err("could not parse areas response")
    return ok(areas=ids, total=len(ids))


def t_call_service(args: dict) -> dict:
    domain = (args.get("domain") or "").strip()
    service = (args.get("service") or "").strip()
    target = args.get("target") or {}
    data = args.get("service_data") or {}
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")

    if not domain or not service:
        return err("domain and service are required")
    if domain not in SERVICE_DOMAIN_ALLOWLIST:
        return err(
            f"domain '{domain}' is not in the allowlist",
            hint="Allowed domains: " + ", ".join(sorted(SERVICE_DOMAIN_ALLOWLIST)),
        )
    if service in SERVICE_NAME_DENYLIST:
        return err(f"service '{service}' is denied for safety")

    summary = f"Home Assistant: call {domain}.{service} on {target or 'default target'}"
    payload = {"domain": domain, "service": service,
               "target": target, "service_data": data}

    if not confirm:
        action_id = Consent.issue("homeassistant", summary, payload, chat_id)
        return consent_required(action_id, summary)

    redeemed = Consent.verify(confirm, "homeassistant", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")

    body = {**redeemed.get("service_data", {}),
            **({"target": redeemed.get("target")} if redeemed.get("target") else {})}
    code, resp = _http("POST",
                       f"/api/services/{redeemed['domain']}/{redeemed['service']}",
                       body)
    if code not in (200, 201):
        audit("homeassistant", "call_service.fail",
              domain=redeemed["domain"], service=redeemed["service"], code=code)
        return err(f"HA service call failed: {resp}")
    audit("homeassistant", "call_service.ok",
          domain=redeemed["domain"], service=redeemed["service"],
          target=redeemed.get("target"))
    return ok(executed=True, response=resp if isinstance(resp, list) else None)


TOOLS = [
    {"name": "ha.health",
     "description": "Check that Home Assistant is reachable and return its API banner.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ha.entity_search",
     "description": "Search HA entities by free-text query (matches entity_id and friendly_name). Returns metadata only — entity_id, friendly name, current state.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},
    {"name": "ha.state",
     "description": "Fetch current state and attributes for one entity_id.",
     "inputSchema": {"type": "object",
                     "properties": {"entity_id": {"type": "string"}},
                     "required": ["entity_id"]}},
    {"name": "ha.area_list",
     "description": "List all configured Home Assistant areas (rooms).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ha.call_service",
     "description": (
         "Invoke a Home Assistant service. ACT-tier: first call returns a "
         "consent token; the agent must surface a confirmation prompt to "
         "the user and re-call with confirmation_token=action_id. "
         "Allowlisted domains only — destructive domains are denied "
         "server-side."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "domain": {"type": "string"},
             "service": {"type": "string"},
             "target": {"type": "object",
                        "description": "e.g. {entity_id: 'light.kitchen'}"},
             "service_data": {"type": "object"},
             "confirmation_token": {"type": "string"},
         },
         "required": ["domain", "service"],
     }},
]


DISPATCH = {
    "ha.health": t_health,
    "ha.entity_search": t_entity_search,
    "ha.state": t_state,
    "ha.area_list": t_area_list,
    "ha.call_service": t_call_service,
}


def dispatch(name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if not fn:
        return err(f"unknown tool: {name}")
    return fn(args)


if __name__ == "__main__":
    serve("homebrain-homeassistant", "0.1.0", TOOLS, dispatch)
