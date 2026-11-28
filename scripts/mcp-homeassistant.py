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
import re
import sys
import json
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_common import (  # noqa: E402
    Consent, audit, consent_required, decrypt_secret, err, ok, serve,
    unavailable,
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
    "todo", "tts",
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
#
# OpenClaw ≥2026.4 treats MCP tools as untrusted for local MEDIA: delivery
# (GHSA-jjgj-cpp9-cvpv) and truncates live tool results at ~32–64k chars.
# Embedding the JPEG as MCP image content therefore (a) looks like an error
# after truncation, (b) is never sendPhoto'd, and (c) burns a long qwen turn
# sanitising base64. Return a workspace path; the agent sends it with the
# message tool. camera.snapshot is the other trap: its filename is on the
# HA instance, so a remote house's camera dumps the JPEG on that other box.
CAMERA_IMAGE_MAX_BYTES = 5 * 1024 * 1024
CAMERA_IMAGE_TIMEOUT = 45
_CAMERA_PROXY = {
    "camera": "/api/camera_proxy/",
    "image": "/api/image_proxy/",
}
_CAMERA_FILE_SERVICES = {"snapshot", "record"}
_ATTR_ALWAYS_REDACT = {"access_token"}
_PICTURE_REDACT_MARKERS = ("token=", "camera_proxy", "image_proxy")
_CAMERA_IMAGE_HINT = (
    "Use ha.camera_image to fetch a still onto this HomeBrain, then send "
    "it with the message tool (media=<path>). camera.snapshot writes a file "
    "on the Home Assistant instance — including a different house — and "
    "will not reach the chat."
)
_CAMERA_SNAPSHOT_HINT = (
    "camera.snapshot writes a JPEG on that Home Assistant instance's disk, "
    "not this box. Call ha.camera_image with the same account and entity_id, "
    "then send the returned path with the message tool's media parameter."
)
_SEND_STILL_HINT = (
    "Still saved on THIS HomeBrain (not the remote HA) at `media`. "
    "Send it with the message tool (media=<that path>). "
    "Do not call camera.snapshot. Do not HTTP-fetch Home Assistant."
)
_CAMERA_HTTP_HINT = (
    "Retry ha.camera_image once if this looks transient. "
    "Do not call camera.snapshot, do not HTTP-fetch Home Assistant, "
    "and do not try to decrypt credentials."
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


def _ws(account: dict, commands: list[dict], timeout: int = 20) -> list[dict]:
    """Run WS commands in one authenticated session; returns each command's
    result frame ({success, result | error}). Device/area/entity registries,
    flow progress and translations exist only on the websocket API."""
    import websocket  # python3-websocket; lazy so the REST tools load without it
    base = (account.get("base_url") or "").rstrip("/")
    scheme, _, rest = base.partition("://")
    # websocket-client's errors are not OSErrors; make them ConnectionErrors
    # so callers see "Home Assistant did not answer", not a traceback.
    try:
        ws = websocket.create_connection(
            ("wss://" if scheme == "https" else "ws://") + rest + "/api/websocket",
            timeout=timeout)
    except websocket.WebSocketException as e:
        raise ConnectionError(f"websocket: {e}") from e
    try:
        ws.recv()
        ws.send(json.dumps({"type": "auth",
                            "access_token": _decrypt(account.get("token") or "")}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            raise ConnectionError("Home Assistant rejected the stored credential")
        out = []
        for i, cmd in enumerate(commands, start=1):
            ws.send(json.dumps({**cmd, "id": i}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == i and msg.get("type") == "result":
                    out.append(msg)
                    break
        return out
    except websocket.WebSocketException as e:
        raise ConnectionError(f"websocket: {e}") from e
    finally:
        ws.close()


def _ws_results(account: dict, *commands: dict) -> list:
    """Results of several WS commands; raises RuntimeError on the first
    refusal (non-admin token, unknown device, ...)."""
    out = []
    for frame in _ws(account, list(commands)):
        if not frame.get("success"):
            raise RuntimeError((frame.get("error") or {}).get("message")
                               or "Home Assistant refused the command")
        out.append(frame.get("result"))
    return out


def _http_bytes(account: dict, path: str,
                timeout: int = CAMERA_IMAGE_TIMEOUT) -> tuple[int, bytes, str]:
    """GET a binary body. Returns (status, body, content_type_or_error)."""
    base = (account.get("base_url") or "").rstrip("/")
    tok = _decrypt(account.get("token") or "")
    if not (base and tok):
        return 0, b"", "account missing base_url or token"
    req = urllib.request.Request(f"{base}{path}", method="GET")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "image/jpeg, image/png, */*")
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


def _agent_media_path(path: str) -> str:
    """Workspace-relative path when the still landed under the OpenClaw
    workspace, otherwise the absolute path. The message tool resolves
    either; relative is what qwen should pass as `media`."""
    ws = os.environ.get(
        "HOMEBRAIN_OPENCLAW_WORKSPACE",
        os.path.expanduser("~/.openclaw/workspace"),
    )
    try:
        rel = os.path.relpath(path, ws)
    except ValueError:
        return path
    if rel.startswith(".."):
        return path
    return rel


def _sniff_mime(body: bytes, declared: str) -> str | None:
    mime = (declared or "").lower()
    if mime.startswith("image/"):
        return mime
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG"):
        return "image/png"
    return None


def _extract_still(body: bytes, declared: str) -> tuple[bytes, str] | tuple[None, None]:
    """Accept a direct image or the first JPEG frame of an MJPEG stream."""
    mime = _sniff_mime(body, declared)
    if mime:
        return body, mime
    soi = body.find(b"\xff\xd8\xff")
    if soi < 0:
        return None, None
    eoi = body.find(b"\xff\xd9", soi + 3)
    frame = body[soi:eoi + 2] if eoi > soi else body[soi:]
    if _sniff_mime(frame, ""):
        return frame, "image/jpeg"
    return None, None


def _refuse_camera_file_service(domain: str, service: str) -> dict | None:
    if domain == "camera" and service in _CAMERA_FILE_SERVICES:
        return err("use ha.camera_image instead of camera.snapshot",
                   hint=_CAMERA_SNAPSHOT_HINT)
    return None


def _ha_err_snippet(body: bytes) -> str:
    """Short HA error for the model. Drop HTML and anything that looks like
    a credential — those sent qwen hunting for the LLAT last time."""
    text = (body or b"").decode("utf-8", "replace").strip()
    if not text or text.startswith("<"):
        return ""
    try:
        obj = json.loads(text)
        msg = obj.get("message") if isinstance(obj, dict) else None
        text = msg if isinstance(msg, str) else text
    except json.JSONDecodeError:
        pass
    low = text.lower()
    if any(s in low for s in ("token", "bearer", "authorization", "gaaaaa")):
        return ""
    return " ".join(text.split())[:120]


def _camera_http_err(code: int, body: bytes) -> dict:
    """Map HA/proxy 400/500 so the agent retries this tool, not snapshot."""
    snippet = _ha_err_snippet(body)
    detail = f": {snippet}" if snippet else ""
    if code == 400:
        return err(
            f"this camera refused a still (HTTP 400){detail}",
            hint=_CAMERA_HTTP_HINT,
        )
    if code >= 500:
        return unavailable(
            f"Home Assistant camera backend failed (HTTP {code}){detail}. "
            + _CAMERA_HTTP_HINT
        )
    return unavailable(
        f"camera proxy failed (HTTP {code}){detail}. " + _CAMERA_HTTP_HINT
    )


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


_AREA_TEMPLATE = (
    "{% set ns = namespace(items=[]) %}"
    "{% for a in areas() %}"
    "{% set ns.items = ns.items + [{'id': a, 'name': area_name(a) or a}] %}"
    "{% endfor %}"
    "{{ ns.items | tojson }}"
)
CALENDAR_MAX_DAYS = 14
CALENDAR_MAX_CALENDARS = 15
CALENDAR_MAX_EVENTS = 40


def _as_list(body: Any) -> list | None:
    """`_http` json-decodes when the body happens to be JSON, so a template
    that ends in `| tojson` arrives as a list, not a string."""
    if isinstance(body, list):
        return body
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def t_area_list(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    code, body = _http(account, "POST", "/api/template",
                       {"template": _AREA_TEMPLATE})
    if code != 200:
        return unavailable(f"HA template eval failed: {body}")
    raw = _as_list(body)
    if raw is None:
        return err("could not parse areas response")
    areas = []
    for item in raw:
        if isinstance(item, str):
            areas.append({"id": item, "name": item})
        elif isinstance(item, dict):
            aid = item.get("id") or item.get("area_id") or ""
            if aid:
                areas.append({"id": aid, "name": item.get("name") or aid})
    return ok(account=account["name"], areas=areas, total=len(areas))


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
    refused = _refuse_camera_file_service(domain, service)
    if refused is not None:
        return refused
    if domain not in SERVICE_DOMAIN_ALLOWLIST:
        extra = (_CAMERA_SNAPSHOT_HINT if domain == "camera"
                 else "Allowed domains: " + ", ".join(sorted(SERVICE_DOMAIN_ALLOWLIST)))
        return err(
            f"domain '{domain}' is not in the allowlist",
            hint=extra,
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
    if not only_domain:
        return err(
            "domain is required",
            hint="e.g. domain=light. Unfiltered list_services dumps the whole registry.",
        )
    code, body = _http(account, "GET", "/api/services")
    if code != 200 or not isinstance(body, list):
        return unavailable(f"HA services unreachable: {body}")
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
    # quote(): a bare "+00:00" in the query decodes to a space and HA 400s.
    path = (f"/api/history/period/{quote(start.isoformat())}"
            f"?filter_entity_id={quote(eid, safe='.,')}"
            f"&end_time={quote(end.isoformat())}"
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


def _cal_time(val: Any) -> str:
    if isinstance(val, dict):
        return val.get("dateTime") or val.get("date") or ""
    return val if isinstance(val, str) else ""


def _trim_event(ev: dict) -> dict:
    desc = ev.get("description") or ""
    if isinstance(desc, str) and len(desc) > 200:
        desc = desc[:200] + "…"
    out = {
        "summary": ev.get("summary") or "",
        "start": _cal_time(ev.get("start")),
        "end": _cal_time(ev.get("end")),
    }
    loc = ev.get("location") or ""
    if loc:
        out["location"] = loc
    if desc:
        out["description"] = desc
    return out


def _fetch_calendar_events(account: dict, entity_id: str,
                           start: datetime, end: datetime) -> tuple[int, list]:
    path = (
        f"/api/calendars/{quote(entity_id, safe='.')}"
        f"?start={quote(start.isoformat())}"
        f"&end={quote(end.isoformat())}"
    )
    code, body = _http(account, "GET", path, timeout=15)
    if code != 200 or not isinstance(body, list):
        return code, []
    return code, [_trim_event(e) for e in body[:CALENDAR_MAX_EVENTS]
                  if isinstance(e, dict)]


def t_calendar_events(args: dict) -> dict:
    """Upcoming events. Rolling window from now — avoids UTC-midnight
    'today' being wrong for a house that isn't in UTC."""
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    eid = (args.get("entity_id") or "").strip()
    if eid and not eid.startswith("calendar."):
        return err("entity_id must be a calendar.* entity")
    days = min(max(int(args.get("days", 1) or 1), 1), CALENDAR_MAX_DAYS)
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=days)

    if eid:
        cals = [{"entity_id": eid, "name": eid}]
    else:
        code, listing = _http(account, "GET", "/api/calendars")
        if code != 200 or not isinstance(listing, list):
            return unavailable(f"HA calendars unreachable: {listing}")
        cals = []
        for c in listing[:CALENDAR_MAX_CALENDARS]:
            if not isinstance(c, dict):
                continue
            cid = c.get("entity_id") or ""
            if cid.startswith("calendar."):
                cals.append({"entity_id": cid, "name": c.get("name") or cid})
        if not cals:
            return ok(account=account["name"], days=days, calendars=[], total=0)

    out = []
    total = 0
    for cal in cals:
        code, events = _fetch_calendar_events(account, cal["entity_id"], start, end)
        if eid and code == 404:
            return err("entity not found")
        if eid and code != 200:
            return unavailable(f"HA calendar unreachable: HTTP {code}")
        if code != 200:
            continue
        total += len(events)
        out.append({**cal, "events": events, "total": len(events)})
    return ok(account=account["name"], days=days, calendars=out, total=total)


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
    refused = _refuse_camera_file_service(domain, service)
    if refused is not None:
        return refused
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
    code, body, ctype = _http_bytes(account, f"{prefix}{eid}",
                                    timeout=CAMERA_IMAGE_TIMEOUT)
    if code == 404:
        return err("entity not found")
    if code == 401:
        return unavailable(
            "Home Assistant rejected the stored credential; "
            "reconnect the account in the dashboard"
        )
    if code != 200:
        return _camera_http_err(code, body)
    still, mime = _extract_still(body, ctype)
    if not still or not mime:
        return err(f"HA did not return an image (content-type {ctype!r})")
    if len(still) > CAMERA_IMAGE_MAX_BYTES:
        audit("homeassistant", "camera_image.too_large",
              account=account["name"], entity_id=eid, bytes=len(still))
        return err(
            f"still is {len(still)} bytes (cap {CAMERA_IMAGE_MAX_BYTES})",
        )
    path = _write_still(eid, still, mime)
    if not path:
        return err("could not write still to the OpenClaw workspace")
    media = _agent_media_path(path)
    audit("homeassistant", "camera_image.ok",
          account=account["name"], entity_id=eid, bytes=len(still),
          path=path)
    # Path only — never attach the JPEG. OpenClaw truncates MCP results
    # well below a camera still, strips local MEDIA: from MCP tools, and
    # does not relay ImageContent to Telegram. The message tool can.
    return ok(
        account=account["name"],
        entity_id=eid,
        mime_type=mime,
        size=len(still),
        path=path,
        media=media,
        hint=_SEND_STILL_HINT,
    )


HB_ALIAS_PREFIX = "[HomeBrain]"


def _stamp_alias(alias: str) -> str:
    alias = (alias or "").strip() or "HomeBrain automation"
    if alias.startswith(HB_ALIAS_PREFIX):
        return alias
    return f"{HB_ALIAS_PREFIX} {alias}"


def _automation_id(raw: str, alias: str) -> str:
    given = (raw or "").strip()
    if given:
        return given
    slug = "".join(c.lower() if c.isalnum() else "_" for c in alias)
    slug = "_".join(p for p in slug.split("_") if p)[:40] or "auto"
    return f"hb_{slug}"


def t_automation_list(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    code, body = _http(account, "GET", "/api/states")
    if code != 200 or not isinstance(body, list):
        return unavailable(f"HA states unreachable: {body}")
    out = []
    for s in body:
        if not isinstance(s, dict):
            continue
        eid = s.get("entity_id") or ""
        if not eid.startswith("automation."):
            continue
        attrs = s.get("attributes") or {}
        out.append({
            "entity_id": eid,
            "id": attrs.get("id") or "",
            "name": attrs.get("friendly_name") or eid,
            "state": s.get("state"),
        })
    return ok(account=account["name"], automations=out, total=len(out))


def t_automation_get(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    aid = str(args.get("id") or "").strip()
    if not aid:
        return err("id is required")
    code, body = _http(
        account, "GET", f"/api/config/automation/config/{quote(aid, safe='')}")
    if code == 404:
        return err("automation not found")
    if code != 200:
        return unavailable(f"HA automation config unreachable: HTTP {code}")
    return ok(account=account["name"], id=aid, config=body)


def t_automation_upsert(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    config = args.get("config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            return err("config must be a JSON object")
    if not isinstance(config, dict):
        return err("config is required (JSON object)")
    config = dict(config)
    alias = _stamp_alias(str(config.get("alias") or args.get("alias") or ""))
    config["alias"] = alias
    aid = _automation_id(str(args.get("id") or config.get("id") or ""), alias)
    config["id"] = aid
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    summary = (f"Home Assistant ({account['name']}): upsert automation "
               f"{aid}: {json.dumps(config, sort_keys=True)[:1500]}")
    payload = {"account": account["name"], "id": aid, "config": config}
    if not confirm:
        action_id = Consent.issue("homeassistant", summary, payload, chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "homeassistant", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account"))
    if redeem_account is None:
        return err("account from confirmation is no longer configured")
    aid = redeemed["id"]
    config = redeemed["config"]
    code, resp = _http(
        redeem_account, "POST",
        f"/api/config/automation/config/{quote(aid, safe='')}",
        config, timeout=15)
    if code not in (200, 201):
        audit("homeassistant", "automation_upsert.fail",
              account=redeem_account["name"], id=aid, code=code)
        return err(f"HA automation upsert failed (code {code}): {resp}")
    audit("homeassistant", "automation_upsert.ok",
          account=redeem_account["name"], id=aid)
    return ok(account=redeem_account["name"], id=aid, alias=config.get("alias"))


def t_automation_delete(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    aid = str(args.get("id") or "").strip()
    if not aid:
        return err("id is required")
    given_alias = str(args.get("alias") or "").strip()
    code, body = _http(
        account, "GET", f"/api/config/automation/config/{quote(aid, safe='')}")
    if code == 404:
        return err("automation not found")
    if code != 200 or not isinstance(body, dict):
        return unavailable(f"HA automation config unreachable: HTTP {code}")
    stored_alias = str(body.get("alias") or "").strip()
    if stored_alias:
        if not given_alias:
            return err("alias is required to delete this automation",
                       hint=f"Stored alias: {stored_alias}")
        if given_alias != stored_alias:
            return err("alias does not match")
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    summary = (f"Home Assistant ({account['name']}): delete automation "
               f"{aid}" + (f" ({stored_alias})" if stored_alias else ""))
    payload = {"account": account["name"], "id": aid, "alias": stored_alias}
    if not confirm:
        action_id = Consent.issue("homeassistant", summary, payload, chat_id)
        return consent_required(action_id, summary)
    redeemed = Consent.verify(confirm, "homeassistant", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    redeem_account = _pick_account(redeemed.get("account"))
    if redeem_account is None:
        return err("account from confirmation is no longer configured")
    code, resp = _http(
        redeem_account, "DELETE",
        f"/api/config/automation/config/{quote(redeemed['id'], safe='')}")
    if code == 404:
        return err("automation not found")
    if code not in (200, 201):
        return err(f"HA automation delete failed (code {code}): {resp}")
    audit("homeassistant", "automation_delete.ok",
          account=redeem_account["name"], id=redeemed["id"])
    return ok(account=redeem_account["name"], deleted=redeemed["id"])


# ---------------------------------------------------------------------------
# Device setup — vendor-neutral. Pairing state lives in Home Assistant (flow
# ids, permit windows), so every call here is stateless and multi-turn chats
# just pass the flow_id / device_id back.
# ---------------------------------------------------------------------------

_FLOW = "/api/config/config_entries/flow"
_OPTIONS_FLOW = "/api/config/config_entries/options/flow"
DIAGNOSTICS_MAX_CHARS = 12000
NEW_DEVICES_MAX_WAIT = 45


def _ws_guard(fn):
    """HA connection failures and timeouts (websocket or REST) become an
    `unavailable` envelope, not a traceback."""
    def run(args: dict) -> dict:
        try:
            return fn(args)
        except ImportError:
            return unavailable("python3-websocket is not installed on this HomeBrain")
        except (OSError, RuntimeError, ConnectionError, ValueError) as e:
            return unavailable(f"Home Assistant did not answer: {e}")
    return run


def _entries(account: dict) -> dict[str, dict]:
    (rows,) = _ws_results(account, {"type": "config_entries/get"})
    return {e["entry_id"]: e for e in rows or []}


def _translations(account: dict, domain: str, category: str) -> dict:
    try:
        (res,) = _ws_results(account, {
            "type": "frontend/get_translations", "language": "en",
            "category": category, "integration": [domain], "config_flow": True})
        return (res or {}).get("resources") or {}
    except Exception:
        return {}


# Edit forms prefill current values as `suggested_value`. HA's frontend sends
# them back; HA itself does not, so an omitted field would be wiped. Keep them
# per flow and merge them under the model's answer.
_FLOW_PREFILL: dict[str, dict] = {}


def _suggested(f: dict) -> Any:
    d = f.get("description")
    return d.get("suggested_value") if isinstance(d, dict) else None


# Integrations rarely mark credentials as password fields, so go by name too.
_SECRET_NAME = re.compile(r"(^|_)(pass(word|phrase|wd)?|secret|token|key|psk|pin)($|_)", re.I)


def _field(f: dict, label: str) -> dict:
    """One form field from HA's serialized schema, compacted for the model."""
    name = f.get("name") or ""
    out: dict[str, Any] = {"name": name, "required": bool(f.get("required"))}
    if label and label != name:
        out["label"] = label
    sel = f.get("selector")
    if isinstance(sel, dict) and sel:
        kind, cfg = next(iter(sel.items()))
        cfg = cfg or {}
        out["type"] = kind
        raw = cfg.get("options") if kind == "select" else None
        secret = kind == "text" and cfg.get("type") == "password"
    else:
        out["type"] = f.get("type")
        raw = f.get("options")
        secret = False
    secret = secret or bool(_SECRET_NAME.search(name))
    if secret:
        out["secret"] = True
    # The current value (suggested) wins over the schema default, as in HA's UI.
    val = _suggested(f)
    if val is None:
        val = f.get("default")
    if val is not None:
        # A stored credential never goes back out; an omitted field keeps it.
        if secret and val != "":
            out["stored"] = True
        else:
            out["default"] = val
    if raw:
        opts = []
        for o in raw[:80]:
            if isinstance(o, dict):
                v, lbl = o.get("value"), o.get("label")
            elif isinstance(o, (list, tuple)) and o:
                v, lbl = o[0], o[-1]
            else:
                v, lbl = o, o
            opts.append(v if lbl in (None, v) else {"value": v, "label": lbl})
        out["options"] = opts
    return out


def _flow_view(account: dict, res: Any, domain: str, options: bool) -> dict:
    """Shape a flow step: fields, menu choices, HA's own instruction text."""
    if not isinstance(res, dict):
        return err(f"unexpected flow response: {str(res)[:200]}")
    kind = res.get("type")
    step = res.get("step_id") or ""
    cat = "options" if options else "config"
    tr = _translations(account, domain, cat)
    ph = res.get("description_placeholders") or {}

    def t(key: str, default: str = "") -> str:
        text = tr.get(f"component.{domain}.{cat}.{key}") or default
        for k, v in ph.items():
            text = text.replace("{" + k + "}", str(v))
        return text

    out: dict[str, Any] = {"flow_id": res.get("flow_id"), "type": kind,
                           "domain": domain}
    if kind in ("form", "menu"):
        out["step"] = step
        for k in ("title", "description"):
            if t(f"step.{step}.{k}"):
                out[k] = t(f"step.{step}.{k}")
    fid = res.get("flow_id") or ""
    _FLOW_PREFILL.pop(fid, None)
    if kind == "form":
        schema = res.get("data_schema") or []
        out["fields"] = [_field(f, t(f"step.{step}.data.{f.get('name')}")) for f in schema]
        pre = {f["name"]: v for f in schema if f.get("name") and (v := _suggested(f)) is not None}
        if pre and fid:
            while len(_FLOW_PREFILL) >= 32:
                _FLOW_PREFILL.pop(next(iter(_FLOW_PREFILL)))
            _FLOW_PREFILL[fid] = pre
        if res.get("errors"):
            out["errors"] = {k: t(f"error.{v}", v) for k, v in res["errors"].items()}
        out["hint"] = ("Fill the fields. A value you lack may be in ha.diagnostics "
                       "of a related device already in HA; ask the owner only for "
                       "the rest. Then call again with flow_id and input={name: value}. "
                       "Omitted fields keep their default; null clears one.")
    elif kind == "menu":
        opts = res.get("menu_options") or []
        out["options"] = [{"id": o, "label": t(f"step.{step}.menu_options.{o}", o)}
                          for o in opts]
        out["hint"] = 'Call again with flow_id and input={"next_step_id": "<option id>"}.'
    elif kind == "create_entry":
        out["title"] = res.get("title")
        if isinstance(res.get("result"), dict):
            out["entry_id"] = res["result"].get("entry_id")
        out["hint"] = ("Saved. ha.new_devices lists what was added; "
                       "ha.device_update names it and sets its room.")
    elif kind == "abort":
        out["reason"] = t(f"abort.{res.get('reason')}", res.get("reason") or "")
    elif kind == "external":
        out["url"] = res.get("url")
        out["hint"] = ("Send the owner this link to finish in a browser, then "
                       "call again with flow_id and no input.")
    else:
        out["hint"] = "Home Assistant is still working; call again with flow_id and no input."
    return ok(**out)


@_ws_guard
def t_integration_setup(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    domain = (args.get("domain") or "").strip()
    entry_id = (args.get("entry_id") or "").strip()
    flow_id = (args.get("flow_id") or "").strip()
    user_input = args.get("input")
    if isinstance(user_input, str):
        try:
            user_input = json.loads(user_input)
        except json.JSONDecodeError:
            return err("input must be a JSON object")

    if flow_id:
        # Config and options flows share one id space but not one URL; try
        # the config flow first so the model never has to carry the kind.
        method = ("DELETE" if args.get("abort")
                  else "POST" if user_input is not None else "GET")
        body = None
        if method == "POST":
            if not isinstance(user_input, dict):
                return err("input must be a JSON object")
            merged = {**_FLOW_PREFILL.get(flow_id, {}), **user_input}
            body = {k: v for k, v in merged.items() if v is not None}
        options = False
        code, res = _http(account, method, f"{_FLOW}/{flow_id}", body, timeout=60)
        if code == 404:
            options = True
            code, res = _http(account, method, f"{_OPTIONS_FLOW}/{flow_id}",
                              body, timeout=60)
        if code == 404:
            return err("flow not found (finished, cancelled or HA restarted)",
                       hint="Start again with domain or entry_id.")
        if code not in (200, 201):
            return err(f"HA flow step failed (code {code}): {res}")
        if method == "DELETE":
            audit("homeassistant", "integration_setup.abort",
                  account=account["name"], flow_id=flow_id)
            return ok(account=account["name"], aborted=flow_id)
        handler = res.get("handler") if isinstance(res, dict) else ""
        dom = (_entries(account).get(handler, {}).get("domain", handler)
               if options else handler)
        view = _flow_view(account, res, dom, options)
        audit("homeassistant", "integration_setup.step", account=account["name"],
              domain=dom, type=view.get("type"), step=view.get("step"))
        return view

    if entry_id:
        entry = _entries(account).get(entry_id)
        if not entry:
            return err("entry_id not found", hint="Call ha.integration_setup with no arguments to list entries.")
        code, res = _http(account, "POST", _OPTIONS_FLOW,
                          {"handler": entry_id, "show_advanced_options": True}, timeout=60)
        if code not in (200, 201):
            return err(f"'{entry['title']}' has no Configure options (code {code}): {res}")
        audit("homeassistant", "integration_setup.options", account=account["name"],
              domain=entry["domain"], entry_id=entry_id)
        return _flow_view(account, res, entry["domain"], True)

    if domain:
        try:
            (manifest,) = _ws_results(account, {"type": "manifest/get", "integration": domain})
        except RuntimeError:
            return err(f"no integration called '{domain}' is installed",
                       hint="Use the domain from the integration's HA URL, e.g. hue, shelly, localtuya.")
        code, res = _http(account, "POST", _FLOW,
                          {"handler": domain, "show_advanced_options": True}, timeout=60)
        if code not in (200, 201):
            return err(f"could not start setup for '{domain}' (code {code}): {res}")
        view = _flow_view(account, res, domain, False)
        iot = (manifest or {}).get("iot_class") or ""
        if iot.startswith("cloud"):
            view["privacy"] = (f"{manifest.get('name', domain)} is a cloud integration "
                               f"({iot}): its data goes through the vendor's servers. "
                               "Tell the owner before continuing.")
        if manifest and not manifest.get("is_built_in", True):
            view["custom_integration"] = True
        audit("homeassistant", "integration_setup.start", account=account["name"],
              domain=domain, iot_class=iot)
        return view

    (progress,) = _ws_results(account, {"type": "config_entries/flow/progress"})
    discovered = [{
        "flow_id": f.get("flow_id"), "domain": f.get("handler"),
        "step": f.get("step_id"),
        "source": (f.get("context") or {}).get("source"),
        "name": ((f.get("context") or {}).get("title_placeholders") or {}).get("name"),
    } for f in progress or []]
    configurable = [{"entry_id": e["entry_id"], "domain": e["domain"], "title": e["title"]}
                    for e in _entries(account).values() if e.get("supports_options")]
    return ok(account=account["name"], discovered=discovered, configurable=configurable,
              hint=("domain=<integration> starts a new setup; entry_id opens an "
                    "entry's Configure menu (e.g. to add a device to a hub); "
                    "flow_id continues a discovered one."))


def _device_name(d: dict) -> str:
    return d.get("name_by_user") or d.get("name") or ""


def _device_rows(account: dict) -> tuple[list[dict], dict, list[dict]]:
    devs, ents = _ws_results(
        account, {"type": "config/device_registry/list"},
        {"type": "config/entity_registry/list_for_display"})
    return devs or [], _entries(account), (ents or {}).get("entities") or []


def _device_view(d: dict, entries: dict, ents: list[dict]) -> dict:
    mine = [e["ei"] for e in ents if e.get("di") == d["id"]]
    domains = sorted({entries.get(x, {}).get("domain", "?")
                      for x in d.get("config_entries") or []})
    return {"device_id": d["id"], "name": _device_name(d),
            "manufacturer": d.get("manufacturer"), "model": d.get("model"),
            "integration": ",".join(domains), "area": d.get("area_id"),
            "entities": mine[:20], "ready": bool(mine)}


def _find_device(devs: list[dict], ents: list[dict], ref: str) -> dict | None:
    """A device by its id or by any of its entity ids — the model usually
    knows `vacuum.x`, never HA's internal device id."""
    if "." in ref:
        ref = next((e.get("di") for e in ents if e.get("ei") == ref), None) or ""
    return next((d for d in devs if d["id"] == ref), None) if ref else None


@_ws_guard
def t_new_devices(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    minutes = min(max(int(args.get("minutes") or 60), 1), 7 * 24 * 60)
    wait_s = min(max(int(args.get("wait_s") or 0), 0), NEW_DEVICES_MAX_WAIT)
    started = time.time()
    while True:
        devs, entries, ents = _device_rows(account)
        devs = [d for d in devs if d.get("entry_type") != "service"]
        recent = [d for d in devs if (d.get("created_at") or 0) >= started - minutes * 60]
        if not wait_s or any((d.get("created_at") or 0) >= started for d in recent) \
                or time.time() - started >= wait_s:
            break
        time.sleep(3)
    recent.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    out = [_device_view(d, entries, ents) for d in recent[:20]]
    hint = ("Ask the owner what to call each device and which room it is in, "
            "then ha.device_update.")
    if any(not d["ready"] for d in out):
        hint += (" A device with no entities is still being set up; battery "
                 "devices need a button press every few seconds until ready.")
    return ok(account=account["name"], minutes=minutes, devices=out,
              total=len(out), hint=hint)


ZIGBEE_JOIN_MAX_S = 254  # ZHA's permit ceiling


@_ws_guard
def t_zigbee_join(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    seconds = min(max(int(args.get("seconds") or 120), 10), ZIGBEE_JOIN_MAX_S)
    body: dict[str, Any] = {"duration": seconds}
    via = (args.get("via") or "").strip()
    if via:
        # ZHA waits for the router's reply, so an offline one hangs the call
        # for over a minute — check first.
        devs, _, ents = _device_rows(account)
        via_id = (_find_device(devs, ents, via) or {}).get("id")
        (zdevs,) = _ws_results(account, {"type": "zha/devices"})
        dev = next((d for d in zdevs or [] if via_id and d.get("device_reg_id") == via_id), None)
        if not dev or dev.get("device_type") not in ("Router", "Coordinator"):
            return err("via must be the device_id of a mains-powered Zigbee device")
        if not dev.get("available"):
            return err(f"{dev.get('user_given_name') or dev.get('name')} is offline "
                       f"(last seen {dev.get('last_seen')}); pick another or omit via")
        body["ieee"] = dev["ieee"]
    # Through a router HA waits for that device's reply, which can take ~20 s.
    code, resp = _http(account, "POST", "/api/services/zha/permit", body, timeout=60)
    if code == 400 and "not found" in str(resp).lower():
        return err("Zigbee (ZHA) is not set up on this Home Assistant",
                   hint="ha.integration_setup domain=zha sets it up on the USB stick.")
    if code not in (200, 201):
        return err(f"HA refused to open joining (code {code}): {resp}")
    audit("homeassistant", "zigbee_join.ok", account=account["name"],
          seconds=seconds, via=via or None)
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return ok(account=account["name"], seconds=seconds,
              open_until=until.isoformat(timespec="seconds"),
              hint=("Tell the owner to put the device in pairing mode now (its "
                    "manual's reset), then call ha.new_devices with wait_s=45."))


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "_".join(p for p in re.split(r"[^a-z0-9]+", text.lower()) if p)


@_ws_guard
def t_device_update(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    did = (args.get("device_id") or "").strip()
    name = (args.get("name") or "").strip()
    area = (args.get("area") or "").strip()
    wanted = args.get("entity_ids") or {}
    if isinstance(wanted, str):
        try:
            wanted = json.loads(wanted)
        except json.JSONDecodeError:
            wanted = None
    if not isinstance(wanted, dict):
        return err("entity_ids must be an object {old_entity_id: new_entity_id}")
    if not did or not (name or area or wanted):
        return err("device_id and at least one of name / area / entity_ids are required")
    devs, entries, ents = _device_rows(account)
    dev = _find_device(devs, ents, did)
    if not dev:
        return err("device not found", hint="Pass a device_id or one of its entity ids.")
    did = dev["id"]
    mine = [e["ei"] for e in ents if e.get("di") == did]

    # Validate every rename before touching anything.
    plan: dict[str, str] = {}
    if name and args.get("rename_entities"):
        old, new = _slug(_device_name(dev)), _slug(name)
        for ei in mine:
            dom, obj = ei.split(".", 1)
            if old and new and old != new and (obj == old or obj.startswith(old + "_")):
                plan[ei] = f"{dom}.{new}{obj[len(old):]}"
    for src, dst in wanted.items():
        src, dst = str(src).strip(), str(dst).strip()
        if src not in mine:
            return err(f"{src} is not an entity of this device", hint="Its entities: " + ", ".join(mine))
        dom = src.split(".", 1)[0]
        dst = dst if "." in dst else f"{dom}.{dst}"
        if not re.fullmatch(rf"{re.escape(dom)}\.[a-z0-9_]+", dst):
            return err(f"{dst}: keep the '{dom}.' domain and use only a-z, 0-9 and _")
        plan[src] = dst

    upd: dict[str, Any] = {"type": "config/device_registry/update", "device_id": did}
    if name:
        upd["name_by_user"] = name
    if area:
        (areas,) = _ws_results(account, {"type": "config/area_registry/list"})
        hit = next((a for a in areas or []
                    if area.casefold() in (a["area_id"].casefold(), a["name"].casefold())),
                   None)
        if not hit:
            (hit,) = _ws_results(account, {"type": "config/area_registry/create", "name": area})
        upd["area_id"] = hit["area_id"]
    updated = _ws_results(account, upd)[0] if (name or area) else None
    renamed, failed = {}, []
    for src, dst in plan.items():
        if src == dst:
            continue
        try:
            _ws_results(account, {"type": "config/entity_registry/update",
                                  "entity_id": src, "new_entity_id": dst})
            renamed[src] = dst
        except RuntimeError as x:
            failed.append(f"{src} ({x})")
    kept = failed + ([e for e in mine if e not in plan]
                     if plan or args.get("rename_entities") else [])
    audit("homeassistant", "device_update.ok", account=account["name"],
          device_id=did, name=name or None, area=upd.get("area_id"),
          renamed=renamed or None)
    view = _device_view(updated if isinstance(updated, dict) and updated.get("id")
                        else {**dev, **upd}, entries, ents)
    view["entities"] = [renamed.get(x, x) for x in view["entities"]]
    return ok(account=account["name"], device=view,
              renamed=renamed or None, unchanged=kept or None)


@_ws_guard
def t_device_remove(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    did = (args.get("device_id") or "").strip()
    if not did:
        return err("device_id is required")
    whole = bool(args.get("remove_integration"))
    devs, entries, ents = _device_rows(account)
    dev = _find_device(devs, ents, did)
    if not dev:
        return err("device not found", hint="Pass a device_id or one of its entity ids.")
    did = dev["id"]
    eid = dev.get("primary_config_entry") or (dev.get("config_entries") or [""])[0]
    entry = entries.get(eid) or {}
    siblings = sum(1 for d in devs if eid in (d.get("config_entries") or []))
    label = f"{_device_name(dev)} ({entry.get('domain', '?')})"
    summary = (f"Home Assistant ({account['name']}): delete integration entry "
               f"'{entry.get('title')}' ({entry.get('domain')}) and its {siblings} device(s)"
               if whole else f"Home Assistant ({account['name']}): remove device {label}")
    confirm = args.get("confirmation_token")
    chat_id = args.get("_chat_id")
    if not confirm:
        payload = {"account": account["name"], "device_id": did,
                   "entry_id": eid, "whole": whole}
        return consent_required(Consent.issue("homeassistant", summary, payload, chat_id),
                                summary)
    redeemed = Consent.verify(confirm, "homeassistant", chat_id)
    if not redeemed:
        return err("confirmation_token invalid or expired")
    if redeemed["whole"]:
        code, resp = _http(account, "DELETE",
                           f"/api/config/config_entries/entry/{redeemed['entry_id']}",
                           timeout=60)
        if code not in (200, 201):
            return err(f"HA could not delete the entry (code {code}): {resp}")
    else:
        try:
            _ws_results(account, {"type": "config/device_registry/remove_config_entry",
                                  "device_id": redeemed["device_id"],
                                  "config_entry_id": redeemed["entry_id"]})
        except RuntimeError as x:
            return err(f"{entry.get('domain')} refused to remove the device: {x}",
                       hint=(f"remove_integration=true deletes the whole "
                             f"'{entry.get('title')}' entry ({siblings} device(s))."))
    audit("homeassistant", "device_remove.ok", account=account["name"],
          device_id=redeemed["device_id"], entry_id=redeemed["entry_id"],
          whole=redeemed["whole"])
    return ok(account=account["name"], removed=label,
              integration_removed=entry.get("title") if redeemed["whole"] else None)


@_ws_guard
def t_diagnostics(args: dict) -> dict:
    account, ebody = _account_or_err(args)
    if ebody is not None:
        return ebody
    did = (args.get("device_id") or "").strip()
    if not did:
        return err("device_id is required")
    devs, entries, ents = _device_rows(account)
    dev = _find_device(devs, ents, did)
    if not dev:
        return err("device not found", hint="Pass a device_id or one of its entity ids.")
    did = dev["id"]
    eid = dev.get("primary_config_entry") or (dev.get("config_entries") or [""])[0]
    code, body = _http(account, "GET",
                       f"/api/diagnostics/config_entry/{eid}/device/{did}", timeout=30)
    if code != 200 or not isinstance(body, dict):
        return err(f"{entries.get(eid, {}).get('domain', 'this integration')} "
                   f"offers no device diagnostics (code {code})")
    data: Any = body.get("data")
    path = (args.get("path") or "").strip()
    for key in [p for p in path.split(".") if p]:
        if isinstance(data, dict) and key in data:
            data = data[key]
        elif isinstance(data, list) and key.isdigit() and int(key) < len(data):
            data = data[int(key)]
        else:
            return err(f"no '{key}' under '{path}'")
    if len(json.dumps(data, default=str)) > DIAGNOSTICS_MAX_CHARS:
        if not isinstance(data, dict):
            return err("too large to return; narrow it with a longer path")
        return ok(account=account["name"], path=path or None,
                  keys={k: len(json.dumps(v, default=str)) for k, v in data.items()},
                  hint="Too large to return whole; call again with path=<key> (chars per key shown).")
    return ok(account=account["name"], path=path or None, diagnostics=data)


_ACCOUNT_PROP = {
    "type": "string",
    "description": "HA account name. Required if several are configured.",
}

TOOLS = [
    {"name": "ha.list_accounts",
     "description": "List HA accounts (name, url).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ha.health",
     "description": "Ping a HA instance.",
     "inputSchema": {"type": "object",
                     "properties": {"account": _ACCOUNT_PROP}}},
    {"name": "ha.entity_search",
     "description": "Search entities by id or friendly name. Returns id, name, state.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "account": _ACCOUNT_PROP},
                     "required": ["query"]}},
    {"name": "ha.state",
     "description": "Current state and attributes for one entity_id. Cameras: ha.camera_image.",
     "inputSchema": {"type": "object",
                     "properties": {"entity_id": {"type": "string"},
                                    "account": _ACCOUNT_PROP},
                     "required": ["entity_id"]}},
    {"name": "ha.area_list",
     "description": "List HA areas (id and name).",
     "inputSchema": {"type": "object",
                     "properties": {"account": _ACCOUNT_PROP}}},
    {"name": "ha.list_services",
     "description": "Describe one domain's services and fields. domain is required.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "domain": {"type": "string",
                        "description": "e.g. light"},
         },
         "required": ["domain"],
     }},
    {"name": "ha.template",
     "description": "Read-only Jinja2 against HA state (counts, filters).",
     "inputSchema": {"type": "object",
                     "properties": {"template": {"type": "string"},
                                    "account": _ACCOUNT_PROP},
                     "required": ["template"]}},
    {"name": "ha.history",
     "description": "State-change history for one entity. hours default 24, max 168.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "entity_id": {"type": "string"},
             "hours": {"type": "integer",
                       "description": "Lookback hours (default 24, max 168)."},
             "account": _ACCOUNT_PROP,
         },
         "required": ["entity_id"],
     }},
    {"name": "ha.calendar_events",
     "description": "Upcoming events. entity_id optional (all calendars). days default 1, max 14.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "entity_id": {"type": "string",
                           "description": "calendar.family — omit for every calendar"},
             "days": {"type": "integer",
                      "description": "Forward window from now (default 1, max 14)."},
             "account": _ACCOUNT_PROP,
         },
     }},
    {"name": "ha.call_service",
     "description": "Call an allowlisted HA service. Other domains: ha.call_service_raw.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "domain": {"type": "string"},
             "service": {"type": "string"},
             "target": {"type": "object",
                        "description": "{entity_id: light.kitchen}"},
             "service_data": {"type": "object"},
             "confirmation_token": {"type": "string"},
         },
         "required": ["domain", "service"],
     }},
    {"name": "ha.call_service_raw",
     "description": (
         "Call any HA service except restart/stop/reload_core_config "
         "and camera.snapshot (use ha.camera_image)."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "domain": {"type": "string"},
             "service": {"type": "string"},
             "target": {"type": "object",
                        "description": "{entity_id: light.kitchen}"},
             "service_data": {"type": "object"},
             "confirmation_token": {"type": "string"},
         },
         "required": ["domain", "service"],
     }},
    {"name": "ha.camera_image",
     "description": (
         "Save a camera or image still on this HomeBrain. Returns `media` — "
         "send with the message tool (media=path). Not camera.snapshot."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "entity_id": {"type": "string",
                           "description": "camera.front_door"},
             "account": _ACCOUNT_PROP,
         },
         "required": ["entity_id"],
     }},
    {"name": "ha.automation_list",
     "description": "List UI automations (id, name, state). No consent.",
     "inputSchema": {"type": "object",
                     "properties": {"account": _ACCOUNT_PROP}}},
    {"name": "ha.automation_get",
     "description": "Read one UI automation config by id. No consent.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "id": {"type": "string"},
             "account": _ACCOUNT_PROP,
         },
         "required": ["id"],
     }},
    {"name": "ha.automation_upsert",
     "description": "Create or replace a UI automation.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "id": {"type": "string",
                    "description": "Stable id we generate if omitted."},
             "config": {"type": "object",
                        "description": "Trigger/action body. Alias is stamped."},
             "confirmation_token": {"type": "string"},
         },
         "required": ["config"],
     }},
    {"name": "ha.automation_delete",
     "description": "Delete a UI automation. Consent. Id and alias must match.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "id": {"type": "string"},
             "alias": {"type": "string",
                       "description": "Required when the automation has an alias."},
             "confirmation_token": {"type": "string"},
         },
         "required": ["id"],
     }},
    {"name": "ha.integration_setup",
     "description": (
         "Set up a device/service in HA step by step. No args: discovered + "
         "configurable. domain: new setup; entry_id: Configure menu; "
         "flow_id+input: answer step; abort."
     ),
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "domain": {"type": "string", "description": "Integration, e.g. hue"},
             "entry_id": {"type": "string"},
             "flow_id": {"type": "string"},
             "input": {"type": "object", "description": "Step answers {field: value}"},
             "abort": {"type": "boolean"},
         },
     }},
    {"name": "ha.zigbee_join",
     "description": "Open the Zigbee network for new devices. seconds max 254; via = device_id of a powered device near the new one.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "seconds": {"type": "integer", "description": "Default 120"},
             "via": {"type": "string", "description": "Device or entity id"},
         },
     }},
    {"name": "ha.new_devices",
     "description": "Devices recently added to HA, with entities. wait_s (max 45) waits for a new one.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "minutes": {"type": "integer", "description": "Lookback (default 60)"},
             "wait_s": {"type": "integer"},
         },
     }},
    {"name": "ha.device_update",
     "description": "Rename a device, set its room (created if missing). rename_entities: entity ids follow the name; entity_ids={old: new} sets exact ones.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "device_id": {"type": "string", "description": "Device id or one of its entity ids"},
             "name": {"type": "string"},
             "area": {"type": "string", "description": "Room name"},
             "rename_entities": {"type": "boolean"},
             "entity_ids": {"type": "object", "description": "{old_entity_id: new_entity_id}"},
         },
         "required": ["device_id"],
     }},
    {"name": "ha.device_remove",
     "description": "Remove a device. remove_integration=true deletes its whole integration entry. Replacing it? Read ha.diagnostics first: that data is gone after.",
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "device_id": {"type": "string", "description": "Device id or one of its entity ids"},
             "remove_integration": {"type": "boolean"},
             "confirmation_token": {"type": "string"},
         },
         "required": ["device_id"],
     }},
    {"name": "ha.diagnostics",
     "description": "A device's raw integration data (ids, data points, value ranges). path drills in: a.b",
     "inputSchema": {
         "type": "object",
         "properties": {
             "account": _ACCOUNT_PROP,
             "device_id": {"type": "string", "description": "Device id or one of its entity ids"},
             "path": {"type": "string"},
         },
         "required": ["device_id"],
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
    "ha.calendar_events": t_calendar_events,
    "ha.call_service": t_call_service,
    "ha.call_service_raw": t_call_service_raw,
    "ha.camera_image": t_camera_image,
    "ha.automation_list": t_automation_list,
    "ha.automation_get": t_automation_get,
    "ha.automation_upsert": t_automation_upsert,
    "ha.automation_delete": t_automation_delete,
    "ha.integration_setup": t_integration_setup,
    "ha.zigbee_join": t_zigbee_join,
    "ha.new_devices": t_new_devices,
    "ha.device_update": t_device_update,
    "ha.device_remove": t_device_remove,
    "ha.diagnostics": t_diagnostics,
}


def dispatch(name: str, args: dict) -> dict:
    fn = DISPATCH.get(name)
    if not fn:
        return err(f"unknown tool: {name}")
    return fn(args)


if __name__ == "__main__":
    serve("homebrain-homeassistant", "0.7.0", TOOLS, dispatch)
