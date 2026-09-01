"""Household roster merge and Home Assistant member helpers.

Flask-free. Token cache lives in the manager session in app.py.
See docs/plans/HOUSEHOLD_ACCOUNTS.md §3.1 and §3.5.
"""
from __future__ import annotations

import json

import requests

HA_URL = "http://127.0.0.1:8123"
HA_CLIENT_ID = f"{HA_URL}/"
HA_WS = "ws://127.0.0.1:8123/api/websocket"
HA_GROUP = "system-users"
VAULT_DOMAIN = "homebrain.local"


class HouseholdError(Exception):
    """A service said no. Message is for the dashboard."""


def vault_email_for(uid):
    return f"{uid.strip().lower()}@{VAULT_DOMAIN}"


def merge_roster(nc_users, vault_users, ha_users, owner_uid,
                 reserved=(), sealed_uids=()):
    """Union of the three services, keyed by Nextcloud uid.

    nc_users: {uid: {display_name, quota, last_seen}}
    vault_users: iterable of {email, ...}
    ha_users: iterable of {username, name, user_id, group_ids}
    """
    reserved = set(reserved)
    sealed = set(sealed_uids)
    owner = owner_uid or ""

    vault_by_local = {}
    unmatched_vault = []
    for v in vault_users or []:
        email = (v.get("email") or "").strip().lower()
        local = email.split("@", 1)[0] if email.endswith(f"@{VAULT_DOMAIN}") else None
        if local and local not in reserved and local != owner:
            vault_by_local[local] = v
        else:
            unmatched_vault.append({"kind": "vault", "email": email,
                                    "name": v.get("name") or email})

    ha_by_user = {}
    unmatched_ha = []
    for h in ha_users or []:
        username = (h.get("username") or "").strip()
        if not username:
            continue
        if username == owner or username in reserved:
            continue
        if username in (nc_users or {}) and username != owner:
            ha_by_user[username] = h
        else:
            unmatched_ha.append({"kind": "home", "username": username,
                                 "name": h.get("name") or username})

    members = []
    for uid, acct in (nc_users or {}).items():
        if uid in reserved or uid == owner:
            continue
        vault = vault_by_local.pop(uid, None)
        home = ha_by_user.get(uid)
        members.append({
            "user": uid,
            "name": (acct or {}).get("display_name") or uid,
            "quota": (acct or {}).get("quota") or "default",
            "last_seen": (acct or {}).get("last_seen") or "never",
            "files": True,
            "vault": vault is not None,
            "home": home is not None,
            "sealed": uid in sealed,
        })

    for uid, v in vault_by_local.items():
        if uid in (nc_users or {}):
            continue
        unmatched_vault.append({"kind": "vault",
                                "email": (v.get("email") or "").strip().lower(),
                                "name": v.get("name") or uid})

    members.sort(key=lambda m: m["name"].lower())
    unmatched = unmatched_vault + unmatched_ha
    return {"members": members, "unmatched": unmatched}


def ha_usernames_from_auth_list(payload):
    """Pull homeassistant-provider usernames out of config/auth/list."""
    users = payload.get("users", payload) if isinstance(payload, dict) else payload
    if not isinstance(users, list):
        return []
    out = []
    for u in users:
        username = None
        for cred in u.get("credentials") or []:
            if cred.get("type") == "homeassistant" or cred.get("auth_provider_type") == "homeassistant":
                data = cred.get("data") or {}
                username = data.get("username")
                break
        if username is None:
            username = u.get("username")
        if not username:
            continue
        out.append({
            "username": username,
            "name": u.get("name") or username,
            "user_id": u.get("id") or u.get("user_id"),
            "group_ids": u.get("group_ids") or [],
            "is_owner": bool(u.get("is_owner")),
        })
    return out


def ha_mint_token(user, password, timeout=15):
    """login_flow → token. Returns access_token or raises HouseholdError."""
    cid = HA_CLIENT_ID
    try:
        r = requests.post(
            f"{HA_URL}/auth/login_flow",
            json={"client_id": cid, "handler": ["homeassistant", None],
                  "redirect_uri": cid},
            timeout=timeout)
        flow_id = r.json().get("flow_id")
        if not flow_id:
            raise HouseholdError("Home Assistant did not start a login")
        r = requests.post(
            f"{HA_URL}/auth/login_flow/{flow_id}",
            json={"client_id": cid, "username": user, "password": password},
            timeout=timeout)
        body = r.json()
        if body.get("type") != "create_entry":
            raise HouseholdError("Home Assistant rejected the admin login")
        code = body.get("result")
        if isinstance(code, dict):
            code = code.get("code") or code.get("auth_code")
        r = requests.post(
            f"{HA_URL}/auth/token",
            data={"grant_type": "authorization_code", "code": code,
                  "client_id": cid},
            timeout=timeout)
        token = r.json().get("access_token")
        if not token:
            raise HouseholdError("Home Assistant did not issue a token")
        return token
    except HouseholdError:
        raise
    except Exception as e:
        raise HouseholdError(f"Home Assistant is unreachable ({e})") from e


def ha_ws_call(token, commands, timeout=20):
    """Run WS commands in one session. Each command is a dict without id.
    Returns a list of result payloads (the `result` field, or the whole
    message if success is false)."""
    import websocket as ws_client

    ws = ws_client.create_connection(HA_WS, timeout=timeout)
    try:
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            raise HouseholdError("Home Assistant websocket did not greet")
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(ws.recv())
        if auth.get("type") != "auth_ok":
            raise HouseholdError("Home Assistant websocket auth failed")
        results = []
        for i, cmd in enumerate(commands, start=1):
            frame = dict(cmd)
            frame["id"] = i
            ws.send(json.dumps(frame))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == i and msg.get("type") == "result":
                    if not msg.get("success"):
                        err = msg.get("error") or {}
                        raise HouseholdError(
                            err.get("message") or "Home Assistant refused the command")
                    results.append(msg.get("result"))
                    break
        return results
    finally:
        try:
            ws.close()
        except Exception:
            pass


def ha_list_users(token):
    result = ha_ws_call(token, [{"type": "config/auth/list"}])[0]
    return ha_usernames_from_auth_list(result if result is not None else {})


def ha_created_user(payload):
    """config/auth/create result → (user_id, group_ids).

    Home Assistant wraps the new user in `{user: {...}}`. Older shapes
    returned the id as a string or a flat dict."""
    if isinstance(payload, str) and payload:
        return payload, [HA_GROUP]
    if not isinstance(payload, dict):
        return None, []
    user = payload["user"] if isinstance(payload.get("user"), dict) else payload
    uid = user.get("id") or user.get("user_id")
    groups = user.get("group_ids") or [HA_GROUP]
    return uid, groups


def ha_create_member(token, name, uid, password):
    """system-users, never system-admin. Returns user_id."""
    created = ha_ws_call(token, [{
        "type": "config/auth/create",
        "name": name or uid,
        "group_ids": [HA_GROUP],
        "local_only": False,
    }])[0] or {}
    user_id, groups = ha_created_user(created)
    if not user_id:
        raise HouseholdError("Home Assistant did not return a user id")
    groups = created.get("group_ids") or [HA_GROUP]
    if HA_GROUP not in groups or "system-admin" in groups:
        # Refuse to continue if we somehow minted an admin.
        try:
            ha_ws_call(token, [{"type": "config/auth/delete", "user_id": user_id}])
        except HouseholdError:
            pass
        raise HouseholdError("Home Assistant would have made them an admin")
    ha_ws_call(token, [{
        "type": "config/auth_provider/homeassistant/create",
        "user_id": user_id,
        "username": uid,
        "password": password,
    }])
    try:
        ha_ws_call(token, [{"type": "person/create", "name": name or uid,
                            "user_id": user_id}])
    except HouseholdError:
        pass  # person entity is nice-to-have
    return user_id


def ha_change_password(token, user_id, password):
    ha_ws_call(token, [{
        "type": "config/auth_provider/homeassistant/admin_change_password",
        "user_id": user_id,
        "password": password,
    }])


def ha_delete_member(token, username, user_id):
    ha_ws_call(token, [
        {"type": "config/auth_provider/homeassistant/delete", "username": username},
        {"type": "config/auth/delete", "user_id": user_id},
    ])
