"""Day-2 activation: what is still left after the wizard.

Flask-free. The Status card and homebrain.setup_status share payload().
Done is live state (paired Telegram, backup timer, off-site enabled,
a household phone) plus explicit skips. Do not snapshot "paired" into
the skip file — a restore that already paired must stay quiet.
"""
from __future__ import annotations

import json
import os

HOMEBRAIN_HOME = os.environ.get("HOMEBRAIN_HOME", "/home/homebrain")
OPENCLAW_CONFIG = os.path.join(HOMEBRAIN_HOME, ".openclaw", "openclaw.json")
BACKUP_CRON_FILE = "/etc/cron.d/homebrain-backup"
BACKUP_TIMER_FILE = "/etc/systemd/system/homebrain-backup.timer"
STATE_DIR = "/var/lib/homebrain"
ACTIVATION_FILE = os.path.join(STATE_DIR, "activation.json")

SKIP_OFFSITE = "offsite"
SKIP_PHONE = "phone"
SKIPPABLE = (SKIP_OFFSITE, SKIP_PHONE)

LAN_NAMES = (
    "homebrain.local",
    "nc-homebrain.local",
    "vault-homebrain.local",
    "ha-homebrain.local",
)

# Consequence-first copy. Buttons are verbs that exist on the dashboard.
STEPS = {
    "recovery": {
        "id": "recovery",
        "title": "Set up your recovery phrase",
        "detail": (
            "If you forget the master password there is no way back into the "
            "Dashboard, Nextcloud or Home Assistant short of wiping the device."
        ),
        "cta": "Set up recovery phrase",
        "tab": "settings",
        "card": "recovery-card",
    },
    "telegram": {
        "id": "telegram",
        "title": "Pair Telegram",
        "detail": (
            "The agent has nobody to talk to. Health alerts have nowhere to go. "
            "Once paired, the agent can finish the rest of this list from chat."
        ),
        "cta": "Pair Telegram",
        "tab": "settings",
        "card": "channels-card",
    },
    "backup": {
        "id": "backup",
        "title": "Turn on a backup schedule",
        "detail": "Until you do, nothing is being saved.",
        "cta": "Set backup schedule",
        "tab": "backup",
        "card": "backup-schedule-card",
    },
    "offsite": {
        "id": "offsite",
        "title": "Add an off-site copy",
        "detail": (
            "A backup that only exists on this box is not a backup of the box. "
            "Fire, theft, or a dead disk take it too."
        ),
        "cta": "Set off-site copy",
        "tab": "backup",
        "card": "offsite-card",
        "skip": SKIP_OFFSITE,
        "skip_cta": "Skip — I accept backups live only on this box",
        "skip_cost": (
            "If this box burns, the copy on it burns with it."
        ),
    },
    "phone": {
        "id": "phone",
        "title": "Put this box on a phone",
        "detail": (
            "Nextcloud, Vault, and Home Assistant on a phone need this box's "
            "certificate, and a household account — not admin."
        ),
        "cta": "Add a phone",
        "tab": "household",
        "card": "household-card",
        "skip": SKIP_PHONE,
        "skip_cta": "I only use computers on this network",
        "skip_cost": (
            "Phones will show a certificate warning and cannot back up photos."
        ),
    },
}


def read_openclaw_config(path=None) -> dict:
    path = path or OPENCLAW_CONFIG
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def telegram_is_paired(config: dict | None = None) -> bool:
    """True once pairing approve has written channels.telegram.allowFrom.

    A bot token without an owner is not the agent.
    """
    if config is None:
        config = read_openclaw_config()
    ch = (config.get("channels") or {}).get("telegram") or {}
    allow = ch.get("allowFrom") or []
    return bool(allow)


def backup_is_scheduled(cron_file=None, timer_file=None) -> bool:
    """True once the owner has saved a schedule (cron leftover or systemd timer)."""
    cron_file = cron_file or BACKUP_CRON_FILE
    timer_file = timer_file or BACKUP_TIMER_FILE
    return os.path.exists(cron_file) or os.path.exists(timer_file)


def offsite_is_enabled(env: dict | None) -> bool:
    env = env or {}
    return str(env.get("OFFSITE_ENABLED", "false")).lower() == "true"


def backup_snapshot(env: dict | None) -> dict:
    """Schedule and off-site host — never a password."""
    env = env or {}
    return {
        "scheduled": backup_is_scheduled(),
        "retention": env.get("BACKUP_RETENTION", "8"),
        "hour": env.get("BACKUP_HOUR", "3"),
        "minute": env.get("BACKUP_MINUTE", "0"),
        "day_week": env.get("BACKUP_DAY_WEEK", "*"),
        "day_month": env.get("BACKUP_DAY_MONTH", "*"),
        "offsite_enabled": offsite_is_enabled(env),
        "offsite_type": env.get("OFFSITE_TYPE", "") or "",
        "offsite_host": env.get("OFFSITE_HOST", "") or "",
        "offsite_path": env.get("OFFSITE_PATH", "") or "",
    }


def load_skips(path=None) -> dict:
    path = path or ACTIVATION_FILE
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    skips = data.get("skips") if isinstance(data.get("skips"), dict) else data
    return {k: bool(skips.get(k)) for k in SKIPPABLE}


def set_skip(step: str, path=None) -> tuple[bool, str]:
    if step not in SKIPPABLE:
        return False, "That step cannot be skipped."
    path = path or ACTIVATION_FILE
    skips = load_skips(path)
    skips[step] = True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"skips": skips}, f)
        f.write("\n")
    os.replace(tmp, path)
    return True, ""


def _step(key: str) -> dict:
    src = STEPS[key]
    out = {k: src[k] for k in (
        "id", "title", "detail", "cta", "tab", "card",
        "skip", "skip_cta", "skip_cost",
    ) if k in src}
    return out


def payload(
    *,
    has_gpu: bool,
    recovery_configured: bool,
    wordlist_ok: bool,
    telegram_paired: bool,
    backup_scheduled: bool,
    offsite_enabled: bool,
    skips: dict | None,
    phone_members: int,
    phone_with_device: int,
    phone_unknown: bool = False,
) -> dict:
    skips = skips or {}
    remaining = []
    if wordlist_ok and not recovery_configured:
        remaining.append(_step("recovery"))
    if has_gpu and not telegram_paired:
        remaining.append(_step("telegram"))
    if not backup_scheduled:
        remaining.append(_step("backup"))
    if not offsite_enabled and not skips.get(SKIP_OFFSITE):
        remaining.append(_step("offsite"))
    phone_done = bool(skips.get(SKIP_PHONE) or phone_with_device > 0)
    if not phone_done:
        remaining.append(_step("phone"))
    return {
        "has_gpu": bool(has_gpu),
        "complete": not remaining,
        "remaining": remaining,
        "skipped": [k for k in SKIPPABLE if skips.get(k)],
        "phone": {
            "members": int(phone_members or 0),
            "with_device": int(phone_with_device or 0),
            "unknown": bool(phone_unknown),
        },
    }
