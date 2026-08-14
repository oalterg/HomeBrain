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
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    MCP_CONTENT, MCP_MEDIA_PATH, Consent, audit, consent_required,
    decrypt_secret, err, mcp_image, ok, serve, unavailable,
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
# For anything outside this set the agent must use `ha.call_service_raw`,
# which has no allowlist but the same consent envelope.
SERVICE_DOMAIN_ALLOWLIST = {
    "light", "switch", "fan", "cover", "climate", "media_player",
    "vacuum", "lock", "scene", "script", "automation", "input_boolean",
    "input_number", "input_select", "input_text", "input_button",
    "button", "notify", "humidifier", "water_heater", "lawn_mower",
    "remote", "siren", "valve",
    "number", "select", "text", "counter", "timer", "calendar",
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

# Camera stills. The agent must not HTTP-fetch these itself — ha.state used
# to leak `access_token` / `entity_picture?token=` and the model would then
# try `/api/camera_proxy` with that token (or the LLAT it does not have).
CAMERA_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_CAMERA_PROXY = {
    "camera": "/api/camera_proxy/",
    "image": "/api/image_proxy/",
}
_ATTR_ALWAYS_REDACT = {"access_token"}
_PICTURE_REDACT_MARKERS = ("token=", "camera_proxy", "image_proxy")
_CAMERA_IMAGE_HINT = (
    "Use ha.camera_image to fetch a still. Do not HTTP-fetch camera URLs "
    "or use tokens from entity attributes — the MCP server holds the HA token."
)


def _decrypt(blob: str) -> str:
    return decrypt_secret(blob, INTEGRATIONS_KEY)


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


def _http_bytes(account: dict, path: str,
                timeout: int = 20) -> tuple[int, bytes, str]:
    """GET a binary body. Returns (status, body, content_type_or_error)."""
    base = (account.get("base_url") or "").rstrip("/")
    tok = _decrypt(account.get("token") or "")
    if not (base and tok):
        return 0, b"", "account missing base_url or token"
    req = urllib.request.Request(f"{base}{path}", method="GET")
    req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            return r.status, r.read(), ctype
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300], ""
    except urllib.error.URLError as e:
        return 0, b"", str(e)


def _sanitize_attributes(attrs: Any) -> dict:
    """Drop camera (and similar) tokens the agent would otherwise fetch with."""
    if not isinstance(attrs, dict):
        return {}
    out = dict(attrs)
    for k in _ATTR_ALWAYS_REDACT:
        out.pop(k, None)
    pic = out.get("entity_picture")
    if isinstance(pic, str) and any(m in pic for m in _PICTURE_REDACT_MARKERS):
        out.pop("entity_picture", None)
    return out


def _media_dir() -> str:
    return os.environ.get(
        "HOMEBRAIN_HA_MEDIA_DIR",
        os.path.expanduser("~/.openclaw/workspace/media"),
    )


def _write_still(entity_id: str, body: bytes, mime: str) -> str | None:
    ext = ".png" if mime == "image/png" else ".jpg"
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in entity_id)
    try:
        dest_dir = _media_dir()
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, safe + ext)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        return path
    except OSError:
        return None


def _sniff_mime(body: bytes, declared: str) -> str | None:
    mime = (declared or "").lower()
    if mime.startswith("image/"):
        return mime
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG"):
        return "image/png"
    return None


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
    eid_out = body.get("entity_id") or eid
    payload = dict(
        account=account["name"],
        entity_id=eid_out,
        state=body.get("state"),
        attributes=_sanitize_attributes(body.get("attributes") or {}),
        last_changed=body.get("last_changed"),
    )
    domain = eid_out.split(".", 1)[0] if "." in eid_out else ""
    if domain in _CAMERA_PROXY:
        payload["hint"] = _CAMERA_IMAGE_HINT
    return ok(**payload)


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

    redeem_account = _pick_account(redeemed.get("account"))
    if redeem_account is None:
        return err(f"account '{redeemed.get('account')}' no longer configured")

    body = {**(redeemed.get("target") or {}),
            **redeemed.get("service_data", {})}
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


def t_history(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    eid = (args.get("entity_id") or "").strip()
    if not eid:
        return err("entity_id is required")
    hours = min(int(args.get("hours", 24) or 24), 168)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    path = (f"/api/history/period/{start.isoformat()}"
            f"?filter_entity_id={eid}"
            f"&end_time={end.isoformat()}"
            f"&minimal_response&significant_changes_only")
    code, body = _http(account, "GET", path, timeout=15)
    if code != 200:
        return unavailable(f"HA history unreachable: {body}")
    if not isinstance(body, list) or not body:
        return ok(account=account["name"], entity_id=eid, changes=[], total=0)
    changes = body[0] if body else []
    trimmed = len(changes) > 200
    entries = [{"state": s.get("state"), "last_changed": s.get("last_changed")}
               for s in (changes[-200:] if trimmed else changes)]
    return ok(account=account["name"], entity_id=eid,
              changes=entries, total=len(changes), trimmed=trimmed)


def t_call_service_raw(args: dict) -> dict:
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

    body = {**(redeemed.get("target") or {}),
            **redeemed.get("service_data", {})}
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


def t_camera_image(args: dict) -> dict:
    """Fetch a still from a camera (or image) entity via HA's proxy.

    Uses the stored LLAT internally. The agent never sees the token and
    must not reconstruct /api/camera_proxy requests itself.
    """
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    eid = (args.get("entity_id") or "").strip()
    if not eid or "." not in eid:
        return err("entity_id is required (e.g. camera.front_door)")
    domain = eid.split(".", 1)[0]
    prefix = _CAMERA_PROXY.get(domain)
    if not prefix:
        return err(
            f"'{eid}' is not a camera or image entity",
            hint="Pass a camera.* (or image.*) entity_id. "
                 "Use ha.entity_search to find cameras.",
        )
    code, body, ctype = _http_bytes(account, f"{prefix}{eid}", timeout=20)
    if code == 404:
        return err("entity not found")
    if code == 401:
        return unavailable("Home Assistant rejected the stored token")
    if code != 200:
        err_txt = body.decode("utf-8", "replace")[:200] if body else ctype
        return unavailable(f"camera proxy failed ({code}): {err_txt}")
    if len(body) > CAMERA_IMAGE_MAX_BYTES:
        audit("homeassistant", "camera_image.too_large",
              account=account["name"], entity_id=eid, bytes=len(body))
        return err(
            f"still is {len(body)} bytes (cap {CAMERA_IMAGE_MAX_BYTES})",
        )
    mime = _sniff_mime(body, ctype)
    if not mime:
        return err(f"HA did not return an image (content-type {ctype!r})")
    path = _write_still(eid, body, mime)
    audit("homeassistant", "camera_image.ok",
          account=account["name"], entity_id=eid, bytes=len(body),
          path=path)
    envelope = ok(
        account=account["name"],
        entity_id=eid,
        mime_type=mime,
        size=len(body),
        path=path,
        hint=("Still is attached. Deliver the image to the user. "
              "Do not fetch /api/camera_proxy or use HA tokens."),
    )
    envelope[MCP_CONTENT] = [mcp_image(body, mime)]
    if path:
        envelope[MCP_MEDIA_PATH] = path
    return envelope


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
     "description": ("Fetch current state and attributes for one entity_id. "
                     "Camera access tokens and proxy URLs are stripped — use "
                     "ha.camera_image for a still, never HTTP-fetch HA."),
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
    {"name": "ha.history",
     "description": (
         "Fetch state-change history for one entity. Returns timestamped "
         "state transitions (e.g. on→off, 21.3→22.1°C). Use this to answer "
         "questions like 'was the door open last night?' or 'what was the "
         "temperature at 3 am?'. Read-only."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "entity_id": {"type": "string"},
             "hours": {"type": "integer",
                       "description": "Lookback window in hours (default 24, max 168)."},
             "account": _ACCOUNT_PROP,
         },
         "required": ["entity_id"],
     }},
    {"name": "ha.call_service",
     "description": (
         "Invoke a Home Assistant service. Allowlisted domains only — "
         "for custom integrations or domains not in the allowlist, fall "
         "back to ha.call_service_raw. Use ha.list_services first if "
         "you're unsure which fields a service accepts."
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
         "Allowlist-free version of ha.call_service. Use when "
         "ha.call_service refuses your domain or for custom integrations. "
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
    {"name": "ha.camera_image",
     "description": (
         "Fetch a still image from a Home Assistant camera (camera.*) or "
         "image (image.*) entity. The MCP server authenticates to HA — do "
         "NOT curl /api/camera_proxy, do NOT use entity_picture URLs or "
         "access_token attributes, do NOT pass an API token. The still is "
         "attached to the tool result for delivery to the user."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "entity_id": {"type": "string",
                           "description": "e.g. camera.front_door"},
             "account": _ACCOUNT_PROP,
         },
         "required": ["entity_id"],
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
    "ha.history": t_history,
    "ha.call_service": t_call_service,
    "ha.call_service_raw": t_call_service_raw,
    "ha.camera_image": t_camera_image,
}


def dispatch(name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if not fn:
        return err(f"unknown tool: {name}")
    return fn(args)


if __name__ == "__main__":
    serve("homebrain-homeassistant", "0.5.0", TOOLS, dispatch)
