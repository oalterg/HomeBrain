# Inbound agent content — threat model

**Status:** open. Distillation only. No implementation plan yet.
**Date:** 2026-08-15
**Parent:** [`PRODUCT_REVIEW_2026-08.md`](PRODUCT_REVIEW_2026-08.md) §B1
**Essence:** A prompt-injected email or a browsed page can reach a root shell.

This is the audit Phase 2 asked for. A later plan must make product decisions, not silent enables. Do not treat this file as a to-do list.

---

## 1. Stance (already decided)

The agent is root-equivalent: `docker` group, NOPASSWD sudo, `tools.exec.mode=full`, `tools.elevated.enabled`. That is the correct model for “the owner never needs a shell.” Pretending a path deny-list contains a process that can `docker run -v /:/host` would be worse.

The security boundary is Telegram pairing plus a loopback-bound gateway. Not the tool sandbox.

Consequences, already written in the August review:

- Do not sandbox the agent.
- MCP consent (Read / Act / Reveal) and audit logs bind *tool* calls. They do not bind `exec`.
- Treat channel pairing, and anything that widens what the agent will read, as privilege escalation.
- Email-to-agent is the scariest surface on the box. Body fetch should stay default-off.
- There is still one agent, and it is the owner. Household members get Nextcloud without the house keys.

Hermes Agent’s conservative defaults (manual exec approval, hardline blocklist, container backends, context-file scanning) fight this stance. Switching runtimes, or running both, is out of scope. Steal the *instinct* (untrusted text is data, not instructions) without importing the architecture.

---

## 2. The chain

```
untrusted text enters the prompt
        │  email header, auto-fetched body, browsed page, Nextcloud note
        ▼
model follows it
        │
        ▼
exec / docker / HA / vault.reveal
        │  no second human gate
        ▼
root-equivalent action
```

Consent on `email.fetch` would only insert a “read this body?” tap. If the owner already asked “what’s in that email?”, they will tap it. After that, nothing else stands between the body and a root shell.

---

## 3. Surfaces, as they actually behave

Verified against the tree on 2026-08-15.

### Email — headers always in-prompt

`email.list_unread` and `email.search` (`scripts/mcp-email.py`) return `From`, `To`, `Subject`, date. No consent. By design (Read tier). A subject line is already untrusted text in the same context as the system prompt. Weaker than a body; enough for this class of attack.

IMAP TEXT search matches bodies server-side but the tool still returns headers only.

### Email — bodies supposed to be Reveal, currently not gated

`email.fetch` is consent-gated and audited in the handler, 50k cap, raw body, no wrapping, no “untrusted content” delimiter, no scan.

On a live box that gate is a no-op. `HOMEBRAIN_MCP_CONSENT` is hardcoded `"false"` in `src/integrations.py:_mcp_consent_env` pending an upstream OpenClaw approvals PR. `scripts/mcp_common.py:serve` auto-redeems the token. Body fetch is **not** default-off, despite §1.

`email.send_direct` is default-off (`HOMEBRAIN_EMAIL_SEND_DIRECT`). Attachments land on disk as `media`; they are not pasted into the prompt.

### Browser — no Read/Reveal split

`config/openclaw.json`: `tools.profile: "full"`, `alsoAllow: ["browser"]`, headless, `noSandbox` (asserted in `patch_openclaw_config`). Page text is tool output. No consent, no wrapping, no scan.

### Nextcloud notes — full content, no consent

`nc.notes_get` returns note content ungated. Shared notes are another inbound channel. `nc.files_list` is metadata only; `nc.files_get` is consent-gated in code and therefore auto-confirmed today, same as email fetch.

### Telegram

The paired owner is trusted. That is the product boundary. Forwarding a malicious email or a link into the chat is the owner widening what the agent will read — privilege escalation, not a bug.

### What these controls actually bind

| Control | Binds | Does not bind |
|---|---|---|
| Telegram pairing + loopback gateway | *Who* talks to the agent | *What* the agent then reads |
| Read / Act / Reveal | MCP tool class | `exec` |
| MCP consent (when on) | Act/Reveal MCP calls | Browser, headers, notes_get, exec |
| Audit logs | After the fact | Anything in the turn |
| `email.send_direct` default-off | Outbound mail | Inbound text |
| No ClawHub | Skill supply chain | Email, web, notes |

---

## 4. Gaps vs the written stance

1. **Body fetch is not default-off.** The review said it should be. Consent is auto-confirmed. Connecting email *is* enabling body fetch.
2. **Browser is silently on** whenever the AI stack is. Connecting email is an explicit Connections row; the browser tool is not.
3. **No isolation between untrusted text and `exec`.** Once the body or page is in the prompt, the same turn can call a root-equivalent tool.
4. **No wrapping.** Tool results dump inbound text as undifferentiated model input.
5. **`nc.notes_get` was never given a Reveal tier.**

Turning consent back on would help Act (draft, archive, HA writes, vault.reveal). It would not close this.

---

## 5. What a later plan must decide

Each of these is a product call, not an engineering preference. Silent enable is how the current gaps happened.

1. **Is connected email allowed to fetch bodies without a tap?** Today: yes, in practice. Stance: no. Pick one and make the box match.
2. **Does “summarize my inbox” count as consent to read bodies?** If yes, the tap is theater. If no, list/search must stay headers-only and fetch must stay off until an explicit per-message confirm the *owner* sees — which needs the upstream approvals PR, or a HomeBrain-side substitute that does not brick the OpenClaw schema.
3. **Is the browser tool on by default?** If inbound web is the same class as email, it needs the same explicit enable (Connections row, or a Settings toggle), not `alsoAllow` in the seed config.
4. **May untrusted text share a turn with `exec`?** Options: live with it (current, honest); wrap + instruct the model to treat it as data (mitigation, not a boundary); refuse exec in a turn that just ingested Reveal content (behavior change, may break “read this and then do X”).
5. **Are Nextcloud notes owner-authored or inbound?** Shared notes from household members are closer to email than to the owner’s own files.

Do not add: agent sandbox, `tools.elevated.allowFrom` as a second sender gate, a Hermes dual-stack, ClawHub, WhatsApp, or a cloud auxiliary model for “injection scanning.”

---

## 6. Pointers

| What | Where |
|---|---|
| Stance | this file §1; August review §B1; `utilities.sh` comments on `tools.exec` / `tools.elevated` |
| Consent currently off | `src/integrations.py:_mcp_consent_env`; `scripts/mcp_common.py` auto-redeem |
| Email tiers | `scripts/mcp-email.py` header; `email.fetch` / `email.list_unread` |
| Browser | `config/openclaw.json`; `patch_openclaw_config` `.browser.noSandbox` |
| Notes | `scripts/mcp-nextcloud.py:t_notes_get` |
| Exec policy | `config/openclaw.json` `tools.exec.mode=full`; `approvals.exec.enabled=false` |
| Why not Hermes | agent-is-root vs conservative exec defaults; appliance glue is OpenClaw-shaped |
