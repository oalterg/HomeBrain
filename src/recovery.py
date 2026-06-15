"""Recovery-phrase core for HomeBrain.

Pure, dependency-free (stdlib only), and Flask-free so it is trivially
unit-testable. See docs/plans/RECOVERY_PHRASE.md for the full design.

Two orthogonal products live here:

* **B1** — a memorable, hyphen-joined word *password* used as the generated
  master password (`generate_password`). Hyphen-joined so the value stays a
  single shell/compose/SQL-safe token, exactly like today's 16-char random
  password — no spaces, no quoting surprises.
* **B2** — an independent, space-joined recovery *phrase* (`generate_phrase`)
  whose scrypt hash is the only thing stored on disk. It can reset the master
  password but is never itself a service credential.

Both draw from the EFF large diceware wordlist (7776 words, ~12.925 bits/word).
The generation pool excludes the four hyphenated EFF entries (drop-down,
felt-tip, t-shirt, yo-yo) so every selected word is pure ``[a-z]`` — that keeps
hyphen-joining unambiguous and lets phrase normalization collapse whitespace
without ever touching a word's internal characters.
"""

import os
import re
import hmac
import base64
import secrets
import hashlib
import unicodedata

# --- Wordlist -------------------------------------------------------------

WORDLIST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "res", "eff_large_wordlist.txt"
)
WORDLIST_LEN = 7776                 # canonical EFF large wordlist length
WORDLIST_VERSION = "eff_large_v1"
# sha256 of res/eff_large_wordlist.txt — tamper/corruption detection only.
# Recovery never needs cross-box reproducibility (each box hashes its own
# phrase against its own copy), so a mismatch is a warning, not a hard failure.
WORDLIST_SHA256 = "6d557f0693958fb5e650b68b5bee585eb82cf4da32965505c789e924743bc522"

# --- Phrase / password shape ---------------------------------------------

DEFAULT_PHRASE_WORDS = 6            # recovery code  -> ~77.5 bits
DEFAULT_PASSWORD_WORDS = 6         # B1 master pw   -> ~77.5 bits
MIN_WORDS = 4                       # ~51.7 bits floor
MAX_WORDS = 12

# --- scrypt parameters (stdlib hashlib.scrypt — no third-party deps) ------
# n=2^15,r=8,p=1 costs ~tens of ms per verify: comfortable for a rate-limited
# endpoint, punishing for offline brute force of a 77-bit secret.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
# OpenSSL needs maxmem >= 128 * n * r (= 32 MiB here). Give headroom.
SCRYPT_MAXMEM = 64 * 1024 * 1024

# New master password accepted at recovery time. Deliberately excludes quotes,
# backslash, whitespace and shell/SQL metacharacters so the value is safe to
# pass through MariaDB `IDENTIFIED BY`, `occ`, the HA auth CLI, and `.env`
# (which is consumed both by `set -a; source` in bash and `--env-file` in
# Compose) with zero escaping divergence. Generated passphrases ([a-z-]) pass.
NEW_PASSWORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@!#%+=:?-]{7,127}$")
NEW_PASSWORD_RULE = (
    "8–128 chars; letters, digits and _.@!#%+=:?- only "
    "(no spaces, quotes or backslashes)."
)


class RecoveryError(Exception):
    """Wordlist missing/corrupt, or bad recovery parameters."""


_pool_cache = None


def _load_pool():
    """Return the pure-``[a-z]`` generation pool, cached. Raises RecoveryError
    if the bundled wordlist is missing or truncated."""
    global _pool_cache
    if _pool_cache is not None:
        return _pool_cache
    try:
        with open(WORDLIST_PATH, "r", encoding="utf-8") as f:
            words = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except OSError as exc:
        raise RecoveryError(f"recovery wordlist unreadable at {WORDLIST_PATH}: {exc}") from exc
    if len(words) != WORDLIST_LEN:
        raise RecoveryError(
            f"recovery wordlist has {len(words)} words, expected {WORDLIST_LEN}"
        )
    pool = [w for w in words if re.fullmatch(r"[a-z]+", w)]
    if len(pool) < 1000:  # sanity floor; real pool is 7772
        raise RecoveryError("recovery wordlist pool implausibly small after filtering")
    _pool_cache = pool
    return pool


def wordlist_ok():
    """True if the bundled wordlist loads and matches the pinned sha256.
    Cheap health probe for the dashboard; never raises."""
    try:
        _load_pool()
        with open(WORDLIST_PATH, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        return digest == WORDLIST_SHA256
    except Exception:
        return False


# --- Generation -----------------------------------------------------------

def generate_words(count=DEFAULT_PHRASE_WORDS):
    """Pick ``count`` words uniformly at random (CSPRNG) from the pool."""
    if not (MIN_WORDS <= count <= MAX_WORDS):
        raise RecoveryError(f"word count {count} out of range [{MIN_WORDS},{MAX_WORDS}]")
    pool = _load_pool()
    return [secrets.choice(pool) for _ in range(count)]


def generate_phrase(count=DEFAULT_PHRASE_WORDS):
    """B2 recovery phrase: space-joined (classic diceware, easy to transcribe)."""
    return " ".join(generate_words(count))


def generate_password(count=DEFAULT_PASSWORD_WORDS):
    """B1 master password: hyphen-joined, single shell/compose/SQL-safe token."""
    return "-".join(generate_words(count))


# --- Normalization & validation -------------------------------------------

def normalize_phrase(phrase):
    """Canonicalize a typed-back phrase before hashing/comparing.

    Lowercase + NFKC, strip, and collapse any run of whitespace to a single
    space. Hyphens (and all other in-word characters) are left untouched, so a
    phrase generated with single-space joins round-trips exactly regardless of
    how the user spaced or cased their input.
    """
    if phrase is None:
        return ""
    text = unicodedata.normalize("NFKC", str(phrase)).lower().strip()
    return re.sub(r"\s+", " ", text)


def is_valid_new_password(password):
    """True if ``password`` is a safe new master password (see NEW_PASSWORD_RE)."""
    return bool(password) and bool(NEW_PASSWORD_RE.match(password))


# --- Hash & verify ---------------------------------------------------------

def _params_str(n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN):
    return f"scrypt$n={n}$r={r}$p={p}$dklen={dklen}"


def _parse_params(params):
    if not params or not params.startswith("scrypt$"):
        raise RecoveryError(f"unsupported recovery KDF params: {params!r}")
    kv = {}
    for part in params.split("$")[1:]:
        if "=" not in part:
            raise RecoveryError(f"malformed recovery params: {params!r}")
        k, v = part.split("=", 1)
        kv[k] = int(v)
    try:
        return kv["n"], kv["r"], kv["p"], kv["dklen"]
    except KeyError as exc:
        raise RecoveryError(f"recovery params missing field: {exc}") from exc


def _scrypt(phrase, salt, n, r, p, dklen):
    return hashlib.scrypt(
        normalize_phrase(phrase).encode("utf-8"),
        salt=salt,
        n=n, r=r, p=p, dklen=dklen,
        maxmem=SCRYPT_MAXMEM,
    )


def hash_phrase(phrase, salt=None):
    """Hash a phrase with a fresh (or supplied) 16-byte salt.

    Returns a dict ready to persist:
    ``{salt, hash, params}`` (salt/hash base64, params a parseable string).
    """
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = _scrypt(phrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    return {
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
        "params": _params_str(),
    }


def verify_phrase(phrase, salt_b64, hash_b64, params):
    """Constant-time check of ``phrase`` against a stored salt/hash/params.

    Parses ``params`` so hashes minted under older cost settings keep verifying
    after the defaults are bumped. Returns False (never raises) on any bad
    input, so callers can treat it as a pure predicate.
    """
    try:
        if not (salt_b64 and hash_b64 and params):
            return False
        n, r, p, dklen = _parse_params(params)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = _scrypt(phrase, salt, n, r, p, dklen)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def build_recovery_record(phrase, word_count, created_at):
    """Map a freshly-minted phrase to the RECOVERY_* keys persisted in .env.
    The plaintext phrase is intentionally NOT part of the record."""
    h = hash_phrase(phrase)
    return {
        "RECOVERY_SCRYPT_SALT": h["salt"],
        "RECOVERY_SCRYPT_HASH": h["hash"],
        "RECOVERY_PARAMS": h["params"],
        "RECOVERY_WORDLIST_VERSION": WORDLIST_VERSION,
        "RECOVERY_WORD_COUNT": str(word_count),
        "RECOVERY_CREATED_AT": str(int(created_at)),
    }
