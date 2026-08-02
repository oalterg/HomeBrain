# Disclosed credentials — what to rotate, in what order

**Status:** open. Nothing here has been rotated. Written 2026-08-02 at the owner's request;
no production box was touched to produce it.

The gitignored `misc` file at the repo root holds live credentials for the production boxes.
It has been read into AI agent sessions repeatedly, which is what makes this a disclosure rather
than a filing problem: every one of those sessions sent the file's contents to a model provider,
and neither you nor I can un-send them. The July product review flagged it; this is the follow-up.

**One piece of good news, checked rather than assumed:** `misc` has never been committed. It is
absent from every ref in the repository (`git log --all -- misc` is empty) and is not tracked
today. There is no history to rewrite, and no public exposure. The exposure is agent sessions and
an unencrypted file on a laptop.

**Treat every credential below as known to a third party.** Moving the file into the Vault does
not change that — the values already left. Only new values do.

---

## Inventory

Values are deliberately not reproduced here. This table describes what each credential *is*, so
you can decide the order to burn them in.

| # | Credential | What it opens | If someone has it |
|---|---|---|---|
| 1 | **Recovery phrase** (6 words, production box) | Resets the master password *without knowing it* | Complete takeover of the box: dashboard, Nextcloud, Home Assistant, Vault. Highest blast radius on this list. |
| 2 | **Master password** (production, and the AMD64 test server) | Dashboard, Nextcloud, Home Assistant, Vault | Same as above, minus the need to reset anything. |
| 3 | **Home Assistant long-lived token** (berlin, valid into 2036) | Full HA REST/WebSocket API | Every device HA controls — locks, cameras, alarms — plus anything HA is configured to shell out to. Does not expire on its own. |
| 4 | **Registrar secret** (+ URL) | `Authorization: Bearer` on the Cloudflare Worker in `registrar/worker.js` | Creates Zitadel accounts in your org at will. Account-creation abuse against the identity provider, billed to you. |
| 5 | **Pangolin `NEWT_ID` / `NEWT_SECRET`** (in the provisioning command lines) | Connects a tunnel client as *your site* | Impersonating the box at the tunnel edge — traffic for your domain can be answered by someone else's machine. |
| 6 | **SSH logins** for the production and test boxes | Shell on the box, and the boxes' SSH passwords are weak (`admin`) | Root, via the `docker` group. See the trust-boundary note in `AGENTS.md`. |
| 7 | **Factory password** (`42`, in the provisioning command lines) | The box before setup completes, and during the handover window | Low on a long-provisioned box; high on a freshly reset one. |
| 8 | **An account pair** (`OliAidana` + password) | Unclear from the file | I could not identify the service from the file, and did not guess. You know what it is — rotate it or strike it from this list. |

---

## Order

Dependencies first, so nothing you rotate gets rolled back by a step that follows it.

1. **Recovery phrase (#1) before the master password (#2).** The phrase resets the password, so a
   rotated password is worth nothing while an old phrase is live.
2. **Master password (#2).** Rotating it re-derives the dependent secrets (Vault, the agent's
   self-MCP token) as part of the same flow.
3. **HA token (#3).** Independent of #2 — the token is separate from the HA login password that
   the master-password rotation touches. It must be revoked in Home Assistant itself.
4. **SSH (#6).** Independent, and the cheapest to do.
5. **Registrar secret (#4) and Pangolin credentials (#5).** Both need coordination outside the
   box — a Cloudflare Worker env var and the Pangolin control plane — and both will break a box
   that still holds the old value until it is updated.
6. **Factory password (#7)** only matters at the next reset or reprovision. Use a new one then.

---

## Steps

### 1. Recovery phrase
Dashboard → **Settings → Recovery Phrase → Regenerate**. The new phrase is shown once; write it
down before closing the page. The old phrase stops working the moment the new hash is stored
(only a scrypt hash is kept — the box cannot show you either phrase again).

**Verify:** Settings → Recovery Phrase reports a phrase is set, and the old phrase is rejected by
the "verify" step of the reset flow.

### 2. Master password
Dashboard → **Settings → Master Password → change**. This drives `rotate_master_password.sh`,
which re-credentials each running service *before* rewriting `.env`, so a failure leaves the box
as it was rather than half-rotated.

**Verify, on the box, not from the confirmation message:**
- Log out and back into the dashboard with the new password.
- Log into Nextcloud as the admin user with the new password.
- Log into Home Assistant with the new password. *This one has lied before:* HA rotation reported
  success for months while writing nothing (#145). Do not skip it.
- The old password is rejected everywhere.

### 3. Home Assistant long-lived token
In Home Assistant: profile → **Security → Long-lived access tokens** → delete the token → create a
new one. Then Dashboard → **Settings → Agent Integrations → Home Assistant** and store the new
token, so the agent keeps working.

**Verify:** the old token returns `401` from `GET /api/` on the HA base URL, and the new one
returns `200`. A revoked HA token fails immediately — there is no cache to wait out.

### 4. Registrar secret
Set a new `REGISTRAR_SECRET` on the Cloudflare Worker (dashboard or `wrangler secret put`). Then
put the same value in `/opt/homebrain/factory_config` on each box that signs users up, as
`REGISTRAR_SECRET`. Boxes holding the old secret get `401 Unauthorized: Invalid Secret` from the
Worker, and only during signup — nothing else on the box uses it.

**Verify:** a signup from a rotated box succeeds; a request with the old secret returns 401.

### 5. Pangolin `NEWT_ID` / `NEWT_SECRET`
Regenerate the site credentials in the Pangolin control plane, then Dashboard → **Connectivity →
Update Tunnel** with the new pair (this rewrites `.env` and redeploys the `newt` container).

**Verify:** the tunnel reports connected, and the public Nextcloud and Home Assistant hostnames
answer from outside the LAN.

### 6. SSH
On each box: `passwd` for every account that has a password, choosing something not derived from
the product name. Better, while you are there:

```
# /etc/ssh/sshd_config.d/homebrain.conf
PasswordAuthentication no
PermitRootLogin no
```

after installing your public key with `ssh-copy-id`. Password auth is the single biggest practical
risk on this list, because item 6 hands over root by way of the `docker` group.

**Verify:** key login works, password login is refused, and you still have a session open while
you test — do not lock yourself out of a box you cannot reach physically.

### 7. Factory password
Not rotatable in place; it is baked into `factory_config` at provisioning time. Choose a new one at
the next reprovision, and do not reuse `42`.

---

## Then deal with the file itself

Rotating without changing how the credentials are stored just resets the clock.

- **Do not re-create `misc` with the new values.** The reason the old ones leaked is that they sat
  in a plaintext file inside a repository that agents read.
- Put the new values in the Vault (Vaultwarden is already in the stack, backed up and tunnelled).
- If a note file is unavoidable, keep it outside the repository tree entirely — an agent working
  in this directory can read anything under it, and `.gitignore` governs commits, not reads.
- `AGENTS.md` is the place to say "never read `misc`" if you keep it. A rule an agent can see is
  worth more than one it cannot.

## What was not done

Nothing was rotated, and no production box was touched — that was the instruction. Once you have
run the steps above, the honest end state is that every value in the old file is dead. Until then,
assume all of them are live in someone else's hands.
