"""Remote access over a mesh VPN on the owner's own account.

Flask-free. The dashboard routes and the self-test share this module. See
docs/plans/REMOTE_ACCESS.md for the two rules it serves: HomeBrain operates no
server, and no third-party server can decipher the traffic.

The box joins the owner's Tailscale network. Four local services are then
published on the box's own name, with TLS ending on the box:

    https://<name>            Nextcloud        (public address optional)
    https://<name>:8443       Vault            (public address optional)
    https://<name>:10000      Home Assistant   (public address optional)
    https://<name>:4443       Dashboard        (private network only, always)

443, 8443 and 10000 are the only ports a public address (Funnel) may use, and
there are three apps, so the dashboard takes a fourth port that is never made
public. It holds the master password.

Everything that touches the outside world goes through run(), which tests
replace. See scripts/tests/test_mesh.py.
"""
from __future__ import annotations

import json
import re
import subprocess

TAILSCALE = "tailscale"
HOSTNAME = "homebrain"

# Published path -> (port on the box's name, local target). One owner for this
# fact: app.py derives every away-from-home URL from urls() below.
APPS = {
    "nextcloud": (443, "http://127.0.0.1:8080"),
    "vault": (8443, "http://127.0.0.1:8082"),
    "homeassistant": (10000, "http://127.0.0.1:8123"),
}
DASHBOARD = (4443, "http://127.0.0.1:8000")

# Printed when a feature is switched off for the owner's network. The CLI then
# either waits or exits 0 (cmd/tailscale/cli enableFeatureInteractive), so the
# link — not the exit code — is the signal.
_ENABLE_URL = re.compile(r"https://login\.tailscale\.com/\S+")
_SECRET = re.compile(r"disablement-secret:[0-9A-Fa-f]+")
_NODEKEY = re.compile(r"^nodekey:[0-9a-f]{64}$")
_TLPUB = re.compile(r"^tlpub:[0-9a-f]{64}$")


def run(argv, timeout=20):
    """(returncode, combined output) of a tailscale CLI call.

    Never raises. A timeout returns 124 with whatever was printed so far: the
    CLI prints an approval link and then waits when the owner's network has a
    feature switched off, and that link is the part we need.
    """
    try:
        r = subprocess.run([TAILSCALE, *argv], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        err = e.stderr or b""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return 124, out + err
    except FileNotFoundError:
        return 127, "tailscale is not installed"
    except Exception as e:  # pragma: no cover - defensive
        return 1, str(e)


def _json(argv, timeout=20):
    rc, out = run(argv, timeout=timeout)
    if rc == 127:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def urls(dns_name):
    """Away-from-home URLs for a box whose mesh name is dns_name."""
    if not dns_name:
        return {}
    out = {}
    for app, (port, _target) in APPS.items():
        out[app] = f"https://{dns_name}" if port == 443 else f"https://{dns_name}:{port}"
    out["dashboard"] = f"https://{dns_name}:{DASHBOARD[0]}"
    return out


def host_port(dns_name, app):
    """`name` or `name:port` — the form the *_TRUSTED_DOMAINS keys carry."""
    port = APPS[app][0]
    return dns_name if port == 443 else f"{dns_name}:{port}"


def _published(serve):
    """{port: is_public} from `tailscale serve status --json`."""
    out = {}
    serve = serve or {}
    funnel = serve.get("AllowFunnel") or {}
    for port in (serve.get("TCP") or {}):
        try:
            p = int(port)
        except (TypeError, ValueError):
            continue
        out[p] = any(k.endswith(f":{p}") and v for k, v in funnel.items())
    return out


def status():
    """Everything the dashboard card and the self-test need, in one read.

    state is one of: not_installed, stopped, needs_login, needs_approval,
    starting, running. Anything the daemon reports that we do not recognise is
    "stopped", never "running".
    """
    st = _json(["status", "--json"])
    if st is None:
        return {"state": "not_installed"}
    backend = st.get("BackendState", "")
    state = {"Running": "running", "NeedsLogin": "needs_login",
             "Starting": "starting", "NeedsMachineAuth": "needs_approval"}.get(backend, "stopped")
    me = st.get("Self") or {}
    dns_name = (me.get("DNSName") or "").rstrip(".")
    out = {
        "state": state,
        "auth_url": st.get("AuthURL") or "",
        "dns_name": dns_name if state == "running" else "",
        "ips": me.get("TailscaleIPs") or [],
        "tailnet": ((st.get("CurrentTailnet") or {}).get("Name") or ""),
        # Empty while HTTPS certificates are switched off for the network.
        "https_ready": bool(st.get("CertDomains")),
        "version": (st.get("Version") or "").split("-")[0],
        "lock": {"enabled": False, "signed": False, "pending": []},
        "published": {},
        "public": False,
    }
    if state != "running":
        return out

    lock = _json(["lock", "status", "--json"]) or {}
    out["lock"] = {
        "enabled": bool(lock.get("Enabled")),
        # This box's own key is signed. False on a locked network means the
        # box is locked out and must be signed from a trusted device.
        "signed": bool(lock.get("NodeKeySigned")),
        "pending": [
            {"name": (p.get("DNSName") or "").rstrip("."),
             "nodekey": p.get("NodeKey") or ""}
            for p in (lock.get("FilteredPeers") or [])
            if _NODEKEY.match(p.get("NodeKey") or "")
        ],
    }
    published = _published(_json(["serve", "status", "--json"]))
    out["published"] = {str(p): pub for p, pub in sorted(published.items())}
    out["public"] = any(published.get(port) for port, _ in APPS.values())
    out["urls"] = urls(dns_name)
    return out


def connect():
    """Start a sign-in. Returns {"auth_url", "qr"} or {"error"}.

    `up --json` prints the sign-in link at once and then waits for the owner
    to finish in a browser. We do not wait with it: the daemon keeps the
    pending sign-in, and status() shows the link until it completes.
    """
    rc, out = run(["up", f"--hostname={HOSTNAME}", "--json", "--timeout=8s"], timeout=15)
    if rc == 127:
        return {"error": "The private network software is not installed."}
    try:
        data = json.loads(out[: out.rindex("}") + 1])
    except ValueError:
        data = {}
    if data.get("BackendState") == "Running":
        return {"connected": True}
    if data.get("AuthURL"):
        return {"auth_url": data["AuthURL"], "qr": data.get("QR", "")}
    st = _json(["status", "--json"]) or {}
    if st.get("BackendState") == "Running":
        return {"connected": True}
    if st.get("AuthURL"):
        return {"auth_url": st["AuthURL"], "qr": ""}
    return {"error": (out.strip().splitlines() or ["No sign-in link was offered."])[-1][:200]}


def publish(public):
    """Publish the three apps and the dashboard on the box's name.

    public=True gives the apps a public address (Funnel); the dashboard is
    never public. Returns {"ok": True} or {"error", "enable_url"} — the link
    is the page where the owner switches the feature on for their network.
    """
    for port, target in APPS.values():
        verb = "funnel" if public else "serve"
        rc, out = run([verb, "--bg", f"--https={port}", target], timeout=25)
        bad = _publish_error(rc, out, public)
        if bad:
            return bad
    port, target = DASHBOARD
    rc, out = run(["serve", "--bg", f"--https={port}", target], timeout=25)
    bad = _publish_error(rc, out, False)
    if bad:
        return bad

    # Trust what the daemon reports, not our exit codes.
    got = _published(_json(["serve", "status", "--json"]))
    for port, _target in APPS.values():
        if got.get(port) is not bool(public):
            return {"error": f"Port {port} did not come up as "
                             f"{'public' if public else 'private'}."}
    if got.get(DASHBOARD[0]) is not False:
        return {"error": "The dashboard did not come up on the private network."}
    return {"ok": True}


def _publish_error(rc, out, public):
    link = _ENABLE_URL.search(out or "")
    if link:
        what = "A public address" if public else "HTTPS"
        return {"error": f"{what} is switched off for your network. Open the link, "
                         "allow it, then try again.",
                "enable_url": link.group(0)}
    if rc != 0:
        return {"error": ((out or "").strip().splitlines() or ["failed"])[-1][:200]}
    return None


def lock_init():
    """Turn on Tailnet Lock with this box as the signing device.

    Returns {"secrets": [...]} — printed once by the CLI and never again. They
    are the only way to turn the lock off if this box is lost. Deliberately no
    --gen-disablement-for-support: that hands Tailscale a secret that undoes
    the lock, which is the party the lock is there to exclude.
    """
    lock = _json(["lock", "status", "--json"]) or {}
    if lock.get("Enabled"):
        return {"error": "The lock is already on."}
    key = lock.get("PublicKey") or ""
    if not _TLPUB.match(key):
        return {"error": "This box has no lock key yet. Connect it first."}
    rc, out = run(["lock", "init", "--gen-disablements=2", "--confirm", key], timeout=60)
    secrets = _SECRET.findall(out or "")
    if rc != 0 or not secrets:
        return {"error": ((out or "").strip().splitlines() or ["failed"])[-1][:200]}
    return {"secrets": secrets}


def lock_sign(nodekey):
    """Approve one waiting device. nodekey comes from status()["lock"]["pending"]."""
    if not _NODEKEY.match(nodekey or ""):
        return {"error": "Not a device key."}
    rc, out = run(["lock", "sign", nodekey], timeout=30)
    if rc != 0:
        return {"error": ((out or "").strip().splitlines() or ["failed"])[-1][:200]}
    return {"ok": True}


def disconnect():
    """Unpublish everything and sign the box out. Idempotent."""
    run(["serve", "reset"])
    rc, out = run(["logout"], timeout=30)
    if rc not in (0, 127) and "not logged in" not in (out or "").lower() \
            and "logged out" not in (out or "").lower():
        return {"error": ((out or "").strip().splitlines() or ["failed"])[-1][:200]}
    return {"ok": True}


def health(st=None):
    """(ok, summary) for a box that has remote access configured."""
    st = st or status()
    if st["state"] != "running":
        return False, "The private network is not connected."
    if st["lock"]["enabled"] and not st["lock"]["signed"]:
        return False, "This box is locked out of the private network and needs signing."
    missing = [name for name, (port, _t) in APPS.items()
               if str(port) not in st["published"]]
    if str(DASHBOARD[0]) not in st["published"]:
        missing.append("dashboard")
    if missing:
        return False, "Not published: " + ", ".join(missing) + "."
    return True, "Connected and published."
