"""Unit tests for the at-rest secret helpers in scripts/mcp_common.py.

The bugs these pin, both found on 2026-08-01 on a live box:

1. `restore.sh` split .env lines on every '=' instead of the first, eating the
   base64 padding that a Fernet key always ends in. The stored key went 44 → 43
   characters on every restore and `Fernet()` then refused it outright.
   `fernet_key()` re-pads on read so an already-truncated box still decrypts.

2. Every MCP server's `_decrypt()` caught `Exception` and returned the blob
   unchanged, so the caller sent `gAAAAA...` to Home Assistant as a bearer
   token. The user saw a bare 401 with nothing anywhere naming the key.
   `decrypt_secret()` returns "" and says so on stderr instead.

Run:  python3 -m pytest scripts/tests/test_mcp_crypto.py
"""
import os
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mcp_common  # noqa: E402
from mcp_common import decrypt_secret, fernet_key  # noqa: E402

KEY = Fernet.generate_key().decode()          # 44 chars, exactly one '=' of padding
TRUNCATED = KEY.rstrip("=")                   # what an old restore left on disk
SECRET = "long-lived-ha-token"
BLOB = Fernet(KEY.encode()).encrypt(SECRET.encode()).decode()


@pytest.fixture(autouse=True)
def _reset_warning_latch():
    """decrypt_secret warns once per process; each test gets a fresh latch."""
    mcp_common._decrypt_warned = False


# --- fernet_key ------------------------------------------------------------

def test_a_generated_fernet_key_is_44_chars_with_one_pad():
    assert len(KEY) == 44
    assert KEY.endswith("=") and not KEY.endswith("==")


def test_truncated_key_is_repadded():
    assert len(TRUNCATED) == 43
    assert fernet_key(TRUNCATED) == KEY


def test_repadding_a_correct_key_changes_nothing():
    assert fernet_key(KEY) == KEY


def test_empty_key_stays_empty():
    assert fernet_key("") == ""
    assert fernet_key("   ") == ""


# --- decrypt_secret --------------------------------------------------------

def test_decrypts_with_a_correct_key():
    assert decrypt_secret(BLOB, KEY) == SECRET


def test_decrypts_with_the_truncated_key_from_a_restored_box():
    """The regression. Before the fix this raised inside Fernet() and the
    caller was handed the ciphertext."""
    assert decrypt_secret(BLOB, TRUNCATED) == SECRET


def test_a_wrong_key_yields_empty_never_the_ciphertext(capsys):
    other = Fernet.generate_key().decode()
    out = decrypt_secret(BLOB, other)
    assert out == ""
    assert not out.startswith("gAAAAA")
    assert "could not be decrypted" in capsys.readouterr().err



def test_a_garbage_key_yields_empty_and_warns_once(capsys):
    assert decrypt_secret(BLOB, "not-a-key") == ""
    assert decrypt_secret(BLOB, "not-a-key") == ""
    assert capsys.readouterr().err.count("could not be decrypted") == 1


def test_no_key_passes_plaintext_through_for_development():
    assert decrypt_secret("plain-password", "") == "plain-password"


def test_legacy_plaintext_survives_a_valid_key():
    """Accounts written before at-rest encryption hold plaintext. Those must
    keep working rather than being blanked as undecryptable."""
    assert decrypt_secret("plain-password", KEY) == "plain-password"


def test_empty_blob_is_empty():
    assert decrypt_secret("", KEY) == ""
    assert decrypt_secret("", "") == ""
