#!/usr/bin/env python3
"""HomeBrain Home Assistant MCP server (multi-account).

Exposes a small, allowlisted slice of the HA REST API to OpenClaw. Users
may register multiple HA instances (different houses, different domains)
and the agent picks one with the `account` parameter on every tool call.

Why this shim instead of HA core's official `mcp_server` integration:
  * Predictable behaviour across HA versions (the official one moves fast).
  * Allowlist enforcement at the MCP layer — we deny destructive domains
    (`homeassistant.restart`, `recorder.*`, etc.) before they ever hit HA.
  * The dashboard owns LLAT lifecycle, so reusing it keeps the
    "one root of identity" principle (see INTEGRATIONS_PLAN.md §1.2).

Environment:
  HA_ACCOUNTS_FILE             path to ~/.openclaw/ha_accounts.json
                               (list of {name, base_url, token}; token
                               is Fernet-encrypted using
                               HOMEBRAIN_INTEGRATIONS_KEY).
  HOMEBRAIN_INTEGRATIONS_KEY   Fernet key for at-rest decryption.

Legacy fallback (single-account installs pre-multi-account):
  HA_BASE_URL, HA_TOKEN, HA_TOKEN_FILE — used only if HA_ACCOUNTS_FILE
  is absent. The dashboard migrates these on first read.
"""
from __future__ import annotations

import os
import sys
import json
import urllib.error
import urllib.request
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    Consent, audit, consent_required, err, ok, serve, unavailable,
)

HA_ACCOUNTS_FILE = os.environ.get("HA_ACCOUNTS_FILE", "")
INTEGRATIONS_KEY = os.environ.get("HOMEBRAIN_INTEGRATIONS_KEY", "")

# Legacy single-account fallback — kept so the MCP keeps working if it's
# ever spawned before the dashboard migrates a legacy install.
LEGACY_BASE_URL = os.environ.get("HA_BASE_URL", "").rstrip("/")
LEGACY_TOKEN_FILE = os.environ.get("HA_TOKEN_FILE", "")
LEGACY_TOKEN = os.environ.get("HA_TOKEN", "")

# Domains the agent is permitted to call services on through the curated
# `ha.call_service` tool. Critical/destructive domains intentionally absent.
# This list is the trust boundary for the curated path; do not expand it
# without thinking about blast radius. For anything outside this set the
# agent must use `ha.call_service_raw`, which has no allowlist and routes
# every call through the same user consent flow.
SERVICE_DOMAIN_ALLOWLIST = {
    "light", "switch", "fan", "cover", "climate", "media_player",
    "vacuum", "lock", "scene", "script", "automation", "input_boolean",
    "input_number", "input_select", "input_text", "input_button",
    "button", "notify", "humidifier", "water_heater", "lawn_mower",
    "remote", "siren", "valve",
}
SERVICE_NAME_DENYLIST = {"delete", "remove", "clear_skipped_update", "purge"}

# Permanently blocked from `ha.call_service_raw` even with user consent —
# these either brick the running HA instance for ~30s (invisible to a user
# clicking "approve") or are irreversibly destructive.
RAW_NUCLEAR_DENYLIST = {
    ("homeassistant", "restart"),
    ("homeassistant", "stop"),
    ("homeassistant", "reload_core_config"),
}


def _decrypt(blob: str) -> str:
    if not INTEGRATIONS_KEY:
        return blob
    try:
        from cryptography.fernet import Fernet  # type: ignore
        return Fernet(INTEGRATIONS_KEY.encode()).decrypt(blob.encode()).decode()
    except Exception:
        return blob


def _accounts() -> list[dict]:
    if HA_ACCOUNTS_FILE and os.path.exists(HA_ACCOUNTS_FILE):
        try:
            with open(HA_ACCOUNTS_FILE) as f:
                data = json.load(f)
            return data.get("accounts", []) if isinstance(data, dict) else []
        except (OSError, json.JSONDecodeError):
            return []
    # Legacy single-account fallback.
    tok = ""
    if LEGACY_TOKEN_FILE and os.path.exists(LEGACY_TOKEN_FILE):
        try:
            tok = open(LEGACY_TOKEN_FILE).read().strip()
        except OSError:
            pass
    if not tok:
        tok = LEGACY_TOKEN.strip()
    if tok and LEGACY_BASE_URL:
        return [{"name": "home", "base_url": LEGACY_BASE_URL, "token": tok}]
    return []


def _pick_account(name: str | None) -> dict | None:
    accounts = _accounts()
    if not accounts:
        return None
    if not name:
        # Single-account installs default to the only entry. Multi-account
        # installs require an explicit account.
        return accounts[0] if len(accounts) == 1 else None
    for a in accounts:
        if a.get("name") == name:
            return a
    return None


def _account_or_err(args: dict) -> tuple[dict | None, dict | None]:
    """Returns (account, None) on success or (None, err_response) on failure.
    Centralises the "which account?" lookup so every tool gets the same
    error messaging for missing/ambiguous selection."""
    name = (args.get("account") or "").strip() or None
    a = _pick_account(name)
    if a is not None:
        return a, None
    accounts = _accounts()
    if not accounts:
        return None, unavailable("no Home Assistant accounts configured")
    if not name and len(accounts) > 1:
        names = ", ".join(repr(x.get("name")) for x in accounts)
        return None, err(
            f"multiple HA accounts configured; pass `account` (one of: {names})",
            hint="Use ha.list_accounts to see the configured set.",
        )
    return None, err(f"account '{name}' not found",
                     hint="Use ha.list_accounts to see the configured set.")


def _http(account: dict, method: str, path: str, body: Any = None,
          timeout: int = 8) -> tuple[int, dict | list | str]:
    base = (account.get("base_url") or "").rstrip("/")
    tok = _decrypt(account.get("token") or "")
    if not (base and tok):
        return 0, "account missing base_url or token"
    url = f"{base}{path}"
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

def t_list_accounts(_args: dict) -> dict:
    accounts = [{"name": a.get("name"), "base_url": a.get("base_url")}
                for a in _accounts()]
    return ok(accounts=accounts, total=len(accounts),
              hint=("Pass `account: <name>` on other tools to pick one. "
                    "Single-account installs default to the only entry."))


def t_health(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    code, body = _http(account, "GET", "/api/")
    if code == 200:
        return ok(account=account["name"],
                  message=body.get("message") if isinstance(body, dict) else "")
    return unavailable(f"HA '{account['name']}' at {account['base_url']} unreachable: {body}")


def t_entity_search(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    q = (args.get("query") or "").lower().strip()
    if not q:
        return err("query is required")
    code, body = _http(account, "GET", "/api/states")
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
    return ok(account=account["name"], results=matches, total=len(matches))


def t_state(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    eid = args.get("entity_id") or ""
    if not eid:
        return err("entity_id is required")
    code, body = _http(account, "GET", f"/api/states/{eid}")
    if code == 404:
        return err("entity not found")
    if code != 200 or not isinstance(body, dict):
        return unavailable(f"HA unreachable: {body}")
    return ok(
        account=account["name"],
        entity_id=body.get("entity_id"),
        state=body.get("state"),
        attributes=body.get("attributes") or {},
        last_changed=body.get("last_changed"),
    )


def t_area_list(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    code, body = _http(account, "POST", "/api/template",
                       {"template": "{{ areas() | list | tojson }}"})
    if code != 200 or not isinstance(body, str):
        return unavailable(f"HA template eval failed: {body}")
    try:
        ids = json.loads(body)
    except json.JSONDecodeError:
        return err("could not parse areas response")
    return ok(account=account["name"], areas=ids, total=len(ids))


def t_call_service(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
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

    summary = (f"Home Assistant ({account['name']}): call {domain}.{service} "
               f"on {target or 'default target'}")
    payload = {"account": account["name"], "domain": domain, "service": service,
               "target": target, "service_data": data}

    if not confirm:
        action_id = Consent.issue("homeassistant", summary, payload, chat_id)
        return consent_required(action_id, summary)

    redeemed = Consent.verify(confirm, "homeassistant", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")

    # Defensive: confirm the redeemed payload still points at a configured
    # account (could have been removed between issue and redeem).
    redeem_account = _pick_account(redeemed.get("account"))
    if redeem_account is None:
        return err(f"account '{redeemed.get('account')}' no longer configured")

    body = {**redeemed.get("service_data", {}),
            **({"target": redeemed.get("target")} if redeemed.get("target") else {})}
    code, resp = _http(redeem_account, "POST",
                       f"/api/services/{redeemed['domain']}/{redeemed['service']}",
                       body)
    if code not in (200, 201):
        audit("homeassistant", "call_service.fail",
              account=redeem_account["name"],
              domain=redeemed["domain"], service=redeemed["service"], code=code)
        return err(f"HA service call failed: {resp}")
    audit("homeassistant", "call_service.ok",
          account=redeem_account["name"],
          domain=redeemed["domain"], service=redeemed["service"],
          target=redeemed.get("target"))
    return ok(account=redeem_account["name"], executed=True,
              response=resp if isinstance(resp, list) else None)


def t_list_services(args: dict) -> dict:
    """Introspect the HA service registry so the agent can discover what
    parameters a service like `light.turn_on` actually accepts before
    calling it (HA returns a 400 for unknown/typed-wrong fields). Optionally
    filter by domain to keep the payload compact."""
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    only_domain = (args.get("domain") or "").strip().lower()
    code, body = _http(account, "GET", "/api/services")
    if code != 200 or not isinstance(body, list):
        return unavailable(f"HA services unreachable: {body}")
    if only_domain:
        body = [d for d in body if d.get("domain") == only_domain]
    return ok(account=account["name"], domains=body, total=len(body))


def t_template(args: dict) -> dict:
    """Render a Jinja2 template against HA state — read-only. Useful for
    composite queries the curated tools don't cover (e.g. `{{ states.light
    | selectattr('state','eq','on') | list | count }}`)."""
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    tpl = args.get("template") or ""
    if not tpl:
        return err("template is required")
    code, body = _http(account, "POST", "/api/template", {"template": tpl})
    if code != 200:
        return err(f"template render failed (code {code}): {body}")
    return ok(account=account["name"], rendered=body)


def t_call_service_raw(args: dict) -> dict:
    """Allowlist-free escape hatch for `ha.call_service`. Use when the
    curated tool rejects a domain you need (e.g. `number.set_value` for a
    bulb's startup brightness) or when HA returns a 400 because the field
    name doesn't match what your specific entity expects. Same consent
    flow as the curated tool — the user still approves every call."""
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    domain = (args.get("domain") or "").strip()
    service = (args.get("service") or "").strip()
    target = args.get("target") or {}
    data = args.get("service_data") or {}
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")

    if not domain or not service:
        return err("domain and service are required")
    if (domain, service) in RAW_NUCLEAR_DENYLIST:
        return err(f"{domain}.{service} is permanently denied",
                   hint="restart/stop/reload_core_config cannot be invoked "
                        "via the agent; do it from the HA UI.")

    summary = (f"Home Assistant ({account['name']}): RAW {domain}.{service} "
               f"on {target or 'default target'}")
    payload = {"account": account["name"], "domain": domain, "service": service,
               "target": target, "service_data": data}

    if not confirm:
        action_id = Consent.issue("homeassistant", summary, payload, chat_id)
        return consent_required(action_id, summary)

    redeemed = Consent.verify(confirm, "homeassistant", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")

    redeem_account = _pick_account(redeemed.get("account"))
    if redeem_account is None:
        return err(f"account '{redeemed.get('account')}' no longer configured")

    body = {**redeemed.get("service_data", {}),
            **({"target": redeemed.get("target")} if redeemed.get("target") else {})}
    code, resp = _http(redeem_account, "POST",
                       f"/api/services/{redeemed['domain']}/{redeemed['service']}",
                       body)
    if code not in (200, 201):
        audit("homeassistant", "call_service_raw.fail",
              account=redeem_account["name"],
              domain=redeemed["domain"], service=redeemed["service"],
              code=code, resp=str(resp)[:200])
        return err(f"HA service call failed (code {code}): {resp}")
    audit("homeassistant", "call_service_raw.ok",
          account=redeem_account["name"],
          domain=redeemed["domain"], service=redeemed["service"],
          target=redeemed.get("target"))
    return ok(account=redeem_account["name"], executed=True,
              response=resp if isinstance(resp, list) else None)


_ACCOUNT_PROP = {
    "type": "string",
    "description": ("Configured account name to act on. Omit when only one "
                    "account is configured; required when multiple are. Use "
                    "ha.list_accounts to enumerate."),
}

TOOLS = [
    {"name": "ha.list_accounts",
     "description": "List configured Home Assistant accounts (name + base_url).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ha.health",
     "description": "Check that a Home Assistant instance is reachable.",
     "inputSchema": {"type": "object",
                     "properties": {"account": _ACCOUNT_PROP}}},
    {"name": "ha.entity_search",
     "description": ("Search HA entities by free-text query (matches entity_id "
                     "and friendly_name). Returns metadata only — entity_id, "
                     "friendly name, current state."),
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "account": _ACCOUNT_PROP},
                     "required": ["query"]}},
    {"name": "ha.state",
     "description": "Fetch current state and attributes for one entity_id.",
     "inputSchema": {"type": "object",
                     "properties": {"entity_id": {"type": "string"},
                                    "account": _ACCOUNT_PROP},
                     "required": ["entity_id"]}},
    {"name": "ha.area_list",
     "description": "List all configured Home Assistant areas (rooms).",
     "inputSchema": {"type": "object",
                     "properties": {"account": _ACCOUNT_PROP}}},
    {"name": "ha.list_services",
     "description": (
         "List Home Assistant service definitions, including each field's "
         "expected type, selector, and example value. Call this before "
         "ha.call_service / ha.call_service_raw when you're not sure what "
         "parameters a service accepts (HA returns 400 for unknown fields, "
         "and brightness in particular has multiple variants: `brightness` "
         "0-255, `brightness_pct` 0-100, `brightness_step_pct`)."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "domain": {"type": "string",
                        "description": "Filter to one domain (e.g. 'light')."},
         },
     }},
    {"name": "ha.template",
     "description": ("Render a Jinja2 template against HA state — read-only. "
                     "Useful for composite queries (counts, filters, conditions)."),
     "inputSchema": {"type": "object",
                     "properties": {"template": {"type": "string"},
                                    "account": _ACCOUNT_PROP},
                     "required": ["template"]}},
    {"name": "ha.call_service",
     "description": (
         "Invoke a Home Assistant service. Just call this directly with the "
         "domain, service, target, and optional account; the runtime prompts "
         "the user for approval automatically — you do not need to ask the "
         "user for a token first. Allowlisted domains only — destructive "
         "domains are denied server-side. For non-allowlisted domains "
         "(`number`, `text`, `select`, custom integrations) or when this "
         "tool returns a 400 because a field name doesn't match what your "
         "specific entity expects, fall back to ha.call_service_raw."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "domain": {"type": "string"},
             "service": {"type": "string"},
             "target": {"type": "object",
                        "description": "e.g. {entity_id: 'light.kitchen'}"},
             "service_data": {"type": "object"},
             "confirmation_token": {"type": "string"},
         },
         "required": ["domain", "service"],
     }},
    {"name": "ha.call_service_raw",
     "description": (
         "Allowlist-free version of ha.call_service. Same consent flow — "
         "the user still approves every call — but with no domain or "
         "service-name filtering. Use this when ha.call_service refuses "
         "your domain (e.g. you need `number.set_value` for a bulb's "
         "startup brightness) or when you've confirmed via ha.list_services "
         "that the curated call shape is correct but HA still returns 400. "
         "A small nuclear list (homeassistant.restart/stop, "
         "reload_core_config) is permanently blocked."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
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
    "ha.list_accounts": t_list_accounts,
    "ha.health": t_health,
    "ha.entity_search": t_entity_search,
    "ha.state": t_state,
    "ha.area_list": t_area_list,
    "ha.list_services": t_list_services,
    "ha.template": t_template,
    "ha.call_service": t_call_service,
    "ha.call_service_raw": t_call_service_raw,
}


def dispatch(name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if not fn:
        return err(f"unknown tool: {name}")
    return fn(args)


if __name__ == "__main__":
    serve("homebrain-homeassistant", "0.3.0", TOOLS, dispatch)
