#!/usr/bin/env python3
"""E2E: owner email, Nextcloud mail, email.send_owner, alerts, email prompts.

Real dashboard API, real occ, the real email MCP over stdio, real SMTP/IMAP.
A throwaway GreenMail stands in for the owner's mail provider. Disposable
boxes only: it removes every connected email account and clears the owner
email, before and after.

    docker run -d --name hb-e2e-greenmail \
      -p $LAN_IP:465:3465 -p $LAN_IP:993:3993 \
      -e GREENMAIL_OPTS="-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 \
        -Dgreenmail.auth.disabled=false -Dgreenmail.users.login=email \
        -Dgreenmail.users=agent:agentpw@agent.test,owner:ownerpw@owner.test,personal:personalpw@personal.test" \
      greenmail/standalone:2.1.5
    sudo systemctl stop homebrain-email-watch      # it would race the wake check
    sudo E2E_MAIL_HOST=$LAN_IP /opt/homebrain/venv/bin/python scripts/e2e/owner_email.py
    sudo systemctl start homebrain-email-watch

The LAN IP, not loopback: the Nextcloud container must reach the same server.
"""
import email
import imaplib
import json
import os
import smtplib
import ssl
import subprocess
import sys
import time
from email.message import EmailMessage

import requests

HB = "/opt/homebrain"
sys.path.insert(0, f"{HB}/scripts")
sys.path.insert(0, f"{HB}/src")
BASE = "http://127.0.0.1:8000"
MAIL = os.environ["E2E_MAIL_HOST"]
NC = "homebrain-nextcloud-1"
AGENT, AGENT_PW = "agent@agent.test", "agentpw"
OWNER, OWNER_PW = "owner@owner.test", "ownerpw"
PERSONAL, PERSONAL_PW = "personal@personal.test", "personalpw"

fails = []


def check(cond, label, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def env():
    out = {}
    for line in open(f"{HB}/.env"):
        if "=" in line and not line.startswith("#"):
            k, v = line.rstrip("\n").split("=", 1)
            out[k] = v.strip("'\"")
    return out


def occ(*args):
    p = subprocess.run(["docker", "exec", "-u", "www-data", NC, "php", "occ", *args],
                       capture_output=True, text=True)
    return p.stdout.strip()


def nc_sys(key):
    return occ("config:system:get", key)


def inbox(user, pw):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    c = imaplib.IMAP4_SSL(MAIL, 993, ssl_context=ctx)
    c.login(user, pw)
    c.select("INBOX")
    _, data = c.uid("search", None, "ALL")
    out = []
    for uid in data[0].split():
        _, d = c.uid("fetch", uid, "(BODY.PEEK[])")
        m = email.message_from_bytes(d[0][1])
        out.append({"from": m["From"], "to": m["To"], "subject": m["Subject"],
                    "reply_to": m["Reply-To"],
                    "body": (m.get_payload(decode=True) or b"").decode(errors="replace")
                    if not m.is_multipart() else ""})
    c.logout()
    return out


def wait_for(fn, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(1)
    return fn()


s = requests.Session()
r = s.post(BASE + "/login", data={"password": env()["MANAGER_PASSWORD"]})
check(r.status_code == 200, "dashboard login", r.text[:100])


def status():
    d = s.get(BASE + "/api/integrations/status").json()
    return next(x for x in d["integrations"] if x["key"] == "email")


def add(name, user, pw, agent):
    return s.post(BASE + "/api/integrations/email/add", json={
        "name": name, "user": user, "imap_host": MAIL, "imap_port": 993,
        "smtp_host": MAIL, "smtp_port": 465, "password": pw, "agent_mailbox": agent})


def set_owner(addr):
    return s.post(BASE + "/api/integrations/email/owner", json={"owner_email": addr})


# --- clean slate ------------------------------------------------------------
for a in status().get("accounts", []):
    s.post(BASE + "/api/integrations/email/remove", json={"name": a["name"]})
set_owner("")
time.sleep(2)
before_owner = len(inbox(OWNER, OWNER_PW))

# --- 1. accounts: the owner inbox first, so "first account" != agent ----------
check(add("Personal", PERSONAL, PERSONAL_PW, False).status_code == 200, "add owner inbox first")
check(add("Agent", AGENT, AGENT_PW, True).status_code == 200, "add agent mailbox second")
st = status()
check((st.get("system_sender") or {}).get("user") == AGENT,
      "system sender prefers the agent mailbox over the first account", st.get("system_sender"))

# --- 2. validation -------------------------------------------------------------
r = set_owner(AGENT)
check(r.status_code == 400 and "agent" in r.json().get("error", ""), "owner email = agent mailbox refused", r.text)
r = set_owner("not-an-email")
check(r.status_code == 400, "malformed owner email refused", r.text)

# --- 3. save owner email --------------------------------------------------------
t0 = time.time()
r = set_owner("Owner <OWNER@owner.test>")
d = r.json()
print(f"     save took {time.time() - t0:.1f}s -> {d}")
check(r.status_code == 200 and d.get("owner_email") == OWNER, "owner email saved (normalized)", d)
check(d.get("sender") == AGENT and not d.get("test_error") and not d.get("nextcloud_error"),
      "save reports sender, test sent, Nextcloud updated", d)
check(env().get("NOTIFY_EMAIL") == OWNER, ".env NOTIFY_EMAIL written")
check(status().get("owner_email") == OWNER, "status shows owner email")

check(nc_sys("mail_smtpmode") == "smtp", "NC mail_smtpmode=smtp", nc_sys("mail_smtpmode"))
check(nc_sys("mail_smtphost") == MAIL and nc_sys("mail_smtpport") == "465"
      and nc_sys("mail_smtpsecure") == "ssl", "NC host/port/ssl from the agent account",
      (nc_sys("mail_smtphost"), nc_sys("mail_smtpport"), nc_sys("mail_smtpsecure")))
check(nc_sys("mail_from_address") == "agent" and nc_sys("mail_domain") == "agent.test",
      "NC sends From the agent mailbox")
check(nc_sys("mail_smtpname") == AGENT and nc_sys("mail_smtpauth") == "true", "NC SMTP auth as agent")
check(nc_sys("lost_password_link") == "disabled", "NC lost-password link disabled")
info = json.loads(occ("user:info", env().get("NEXTCLOUD_ADMIN_USER", "admin"), "--output=json"))
check(info.get("email") == OWNER, "NC admin email = owner", info.get("email"))

msgs = wait_for(lambda: [m for m in inbox(OWNER, OWNER_PW)[before_owner:]
                         if m["subject"] == "HomeBrain will email you here"])
check(bool(msgs) and msgs[0]["from"] == AGENT, "dashboard test email reached owner, From agent",
      msgs)

# --- 4. Nextcloud itself sends -----------------------------------------------------
# GreenMail's certificate is self-signed; a real provider's is not. Relax peer
# verification for this check only, then put the generated value back.
occ("config:system:set", "mail_smtpstreamoptions", "ssl", "verify_peer", "--value=false", "--type=boolean")
occ("config:system:set", "mail_smtpstreamoptions", "ssl", "verify_peer_name", "--value=false", "--type=boolean")
occ("config:system:set", "mail_smtpstreamoptions", "ssl", "allow_self_signed", "--value=true", "--type=boolean")
n0 = len(inbox(OWNER, OWNER_PW))
pw = env()["NEXTCLOUD_ADMIN_PASSWORD"]
port = env().get("NEXTCLOUD_PORT", "8080")
r = requests.post(f"http://127.0.0.1:{port}/index.php/settings/admin/mailtest",
                  auth=(env().get("NEXTCLOUD_ADMIN_USER", "admin"), pw),
                  headers={"OCS-APIRequest": "true", "Host": "nc-homebrain.local"})
print(f"     NC mailtest HTTP {r.status_code}: {r.text[:200]}")
msgs = wait_for(lambda: inbox(OWNER, OWNER_PW)[n0:])
check(r.status_code == 200 and bool(msgs) and "agent.test" in (msgs[0]["from"] or ""),
      "Nextcloud's own test email reached the owner, From agent.test", msgs)
occ("config:system:delete", "mail_smtpstreamoptions")

# --- 5. agent tools over the real MCP stdio server -----------------------------------
import integrations  # noqa: E402

spec = integrations._spec_email()
check(spec["env"].get("HOMEBRAIN_OWNER_EMAIL") == OWNER, "MCP spec carries the owner email")
child_env = {**os.environ, **spec["env"]}
p = subprocess.Popen([spec["command"], *spec["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True, env=child_env)


def rpc(i, method, params=None):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
    p.stdin.flush()
    return json.loads(p.stdout.readline())


def call(i, name, args):
    res = rpc(i, "tools/call", {"name": name, "arguments": args})["result"]
    return json.loads(res["content"][0]["text"])


rpc(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "e2e"}})
tools = {t["name"]: t for t in rpc(2, "tools/list")["result"]["tools"]}
check("email.send_owner" in tools and "to" not in tools["email.send_owner"]["inputSchema"]["properties"],
      "agent sees email.send_owner, with no `to` parameter")
la = call(3, "email.list_accounts", {})
check(la.get("owner_email") == OWNER and la.get("send_owner_from") == AGENT,
      "list_accounts tells the agent the owner address and sender", la)
sd = call(4, "email.send_direct", {"account": "Agent", "to": "stranger@personal.test", "subject": "x"})
check(not sd.get("ok") and "disabled" in sd.get("error", ""), "send_direct still off for third parties", sd)
n0 = len(inbox(OWNER, OWNER_PW))
so = call(5, "email.send_owner", {"subject": "Your summary", "body": "Here is what you asked for."})
check(so.get("ok") and so.get("to") == OWNER, "email.send_owner succeeds with send_direct off", so)
msgs = wait_for(lambda: inbox(OWNER, OWNER_PW)[n0:])
check(bool(msgs) and msgs[0]["from"] == AGENT and msgs[0]["subject"] == "Your summary",
      "send_owner mail arrived From agent", msgs)
so = call(6, "email.send_owner", {"subject": "x", "to": "stranger@personal.test"})
check(so.get("to") == OWNER, "a smuggled `to` is ignored; still the owner", so)
p.stdin.close()
p.wait(timeout=10)

# --- 6. health alert email ------------------------------------------------------------
import healthcheck  # noqa: E402

tgt = healthcheck.resolve_email_target(env())
check(tgt and tgt[0]["user"] == AGENT and tgt[1] == OWNER, "alerts resolve to agent -> owner", tgt and tgt[0]["user"])
n0 = len(inbox(OWNER, OWNER_PW))
check(healthcheck.send_email(env(), tgt[0], tgt[1], "HomeBrain needs attention", "Disk is full."),
      "alert email sent")
msgs = wait_for(lambda: inbox(OWNER, OWNER_PW)[n0:])
check(bool(msgs) and msgs[0]["from"] == AGENT, "alert arrived From agent (replies reach the agent)", msgs)

# --- 7. owner replies -> agent wake prompt -----------------------------------------------
r = s.post(BASE + "/api/channels/email", json={"enabled": True, "allow_from": [OWNER]})
check(r.status_code == 200, "email prompts enabled with owner as From", r.text)
import email_watch  # noqa: E402

woken = []
email_watch._wake_async = woken.append
accounts = email_watch.load_accounts()
agent_acc = next(a for a in accounts if a["user"] == AGENT)
state = {}
key = email_watch.integrations_key()
email_watch.poll_account(agent_acc, email_watch.load_channel(), [AGENT], state, key)  # seed
reply = EmailMessage()
reply["From"], reply["To"], reply["Subject"] = OWNER, AGENT, "Re: HomeBrain needs attention"
reply.set_content("Please clean up old camera footage.\n\n> Disk is full.")
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
with smtplib.SMTP_SSL(MAIL, 465, context=ctx) as sm:
    sm.login(OWNER, OWNER_PW)
    sm.send_message(reply)
time.sleep(2)
email_watch.poll_account(agent_acc, email_watch.load_channel(), [AGENT], state, key)
check(len(woken) == 1, "owner's reply woke one agent turn", len(woken))
if woken:
    check("email.send_owner (send_direct is off)" in woken[0] and "camera footage" in woken[0],
          "wake prompt tells the agent to answer with email.send_owner", woken[0][:300])
s.post(BASE + "/api/channels/email", json={"enabled": False})

# --- 8. account changes: Nextcloud follows the sender -------------------------------------
s.post(BASE + "/api/integrations/email/agent-mailbox", json={"name": "Agent", "agent_mailbox": False})
check(wait_for(lambda: nc_sys("mail_from_address") == "personal"),
      "agent flag off -> Nextcloud sends from the next account", nc_sys("mail_from_address"))
s.post(BASE + "/api/integrations/email/agent-mailbox", json={"name": "Agent", "agent_mailbox": True})
check(wait_for(lambda: nc_sys("mail_from_address") == "agent"), "agent flag back on -> agent again")
s.post(BASE + "/api/integrations/email/remove", json={"name": "Agent"})
check(wait_for(lambda: nc_sys("mail_smtpname") == PERSONAL), "agent removed -> Nextcloud uses the remaining account")
s.post(BASE + "/api/integrations/email/remove", json={"name": "Personal"})
check(wait_for(lambda: nc_sys("mail_smtpmode") == "null"), "last account removed -> Nextcloud mail off")
check(nc_sys("mail_smtppassword") == "", "removed account's password not left in Nextcloud",
      "len=%d" % len(nc_sys("mail_smtppassword")))

# --- 9. clear owner email --------------------------------------------------------------------
r = set_owner("")
check(r.status_code == 200 and env().get("NOTIFY_EMAIL", "x") == "", "owner email cleared", r.text)
info = json.loads(occ("user:info", env().get("NEXTCLOUD_ADMIN_USER", "admin"), "--output=json"))
check(not info.get("email"), "NC admin email cleared", info.get("email"))

print(f"\n{len(fails)} failed" + (": " + "; ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
