#!/usr/bin/env python3
"""Unit tests for src/recovery.py — the recovery-phrase core.

Runnable two ways:
    python3 scripts/tests/test_recovery.py      # standalone, no deps
    pytest scripts/tests/test_recovery.py       # if pytest is installed
"""
import os
import re
import sys
import base64

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "src"))

import recovery  # noqa: E402


def test_wordlist_loads_and_pins():
    assert recovery.wordlist_ok(), "bundled wordlist failed integrity/load check"
    pool = recovery._load_pool()
    # 7776 canonical minus the 4 hyphenated EFF entries.
    assert len(pool) == 7772, f"pool size {len(pool)} != 7772"
    assert all(re.fullmatch(r"[a-z]+", w) for w in pool)


def test_generate_password_is_single_token():
    pw = recovery.generate_password(6)
    assert " " not in pw and "'" not in pw and '"' not in pw and "\\" not in pw
    assert len(pw.split("-")) == 6
    # Must satisfy the new-password policy so a generated pw is always rotatable.
    assert recovery.is_valid_new_password(pw), pw


def test_generate_phrase_word_count_and_uniqueness():
    p1 = recovery.generate_phrase(6)
    p2 = recovery.generate_phrase(6)
    assert len(p1.split(" ")) == 6
    assert p1 != p2, "two phrases collided — CSPRNG not used?"


def test_word_count_bounds():
    for bad in (0, 3, 13, 100):
        try:
            recovery.generate_words(bad)
            assert False, f"expected RecoveryError for count={bad}"
        except recovery.RecoveryError:
            pass


def test_normalize_is_idempotent_and_forgiving():
    phrase = "Apple   RIVER\tmountain"
    n = recovery.normalize_phrase(phrase)
    assert n == "apple river mountain"
    assert recovery.normalize_phrase(n) == n
    # hyphenated words survive normalization unchanged
    assert recovery.normalize_phrase("Drop-Down  River") == "drop-down river"


def test_hash_verify_roundtrip():
    phrase = recovery.generate_phrase(6)
    rec = recovery.build_recovery_record(phrase, 6, 1_700_000_000)
    assert recovery.verify_phrase(
        phrase, rec["RECOVERY_SCRYPT_SALT"], rec["RECOVERY_SCRYPT_HASH"], rec["RECOVERY_PARAMS"]
    )
    # Case/spacing-insensitive match.
    assert recovery.verify_phrase(
        "  " + phrase.upper() + "  ",
        rec["RECOVERY_SCRYPT_SALT"], rec["RECOVERY_SCRYPT_HASH"], rec["RECOVERY_PARAMS"]
    )


def test_verify_rejects_wrong_and_garbage():
    phrase = recovery.generate_phrase(6)
    rec = recovery.build_recovery_record(phrase, 6, 1_700_000_000)
    salt, h, params = rec["RECOVERY_SCRYPT_SALT"], rec["RECOVERY_SCRYPT_HASH"], rec["RECOVERY_PARAMS"]
    assert not recovery.verify_phrase("totally wrong phrase words here", salt, h, params)
    assert not recovery.verify_phrase(phrase, "", h, params)
    assert not recovery.verify_phrase(phrase, salt, h, "")
    assert not recovery.verify_phrase(phrase, salt, h, "bogus$params")
    assert not recovery.verify_phrase(phrase, "!!notbase64!!", h, params)


def test_verify_honors_stored_params_after_default_bump(monkeypatch=None):
    # Mint a record at low cost, then raise the module defaults; the old hash
    # must still verify because verify parses the stored params.
    phrase = recovery.generate_phrase(5)
    salt = base64.b64encode(b"0123456789abcdef").decode()
    low = recovery._scrypt(phrase, base64.b64decode(salt), 1 << 14, 8, 1, 32)
    rec_hash = base64.b64encode(low).decode()
    params = "scrypt$n=16384$r=8$p=1$dklen=32"
    orig_n = recovery.SCRYPT_N
    try:
        recovery.SCRYPT_N = 1 << 16  # bump default
        assert recovery.verify_phrase(phrase, salt, rec_hash, params)
    finally:
        recovery.SCRYPT_N = orig_n


def test_new_password_policy():
    assert recovery.is_valid_new_password("correct-horse-battery-staple-quux-zap")
    assert recovery.is_valid_new_password("Tr0ub4dour.Plus")
    assert not recovery.is_valid_new_password("has spaces here")
    assert not recovery.is_valid_new_password("has'quote")
    assert not recovery.is_valid_new_password('has"quote')
    assert not recovery.is_valid_new_password("back\\slash")
    assert not recovery.is_valid_new_password("short")          # < 8
    assert not recovery.is_valid_new_password("")
    assert not recovery.is_valid_new_password("a" * 200)        # > 128


def _run_standalone():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
