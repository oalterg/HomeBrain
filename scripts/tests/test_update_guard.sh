#!/usr/bin/env bash
#
# Unit tests for the update.sh downgrade guard. The decision logic lives in
# common.sh (version_lt / parse_nc_tag / detect_downgrade) precisely so it can
# be exercised here with no Docker, no network, and no root — runs the same on
# a Linux target and a macOS dev box.
#
#   bash scripts/tests/test_update_guard.sh
#
# Exit status: 0 if every case passes, 1 otherwise.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON="$SCRIPT_DIR/../common.sh"

# common.sh mkdir's /var/log/homebrain and probes the GPU at source time; on a
# dev box that is harmless noise, and we only want the pure helpers. Silence it.
# shellcheck source=../common.sh disable=SC1091
source "$COMMON" 2>/dev/null

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

# version_lt expectations
lt()     { if version_lt "$1" "$2"; then ok "$1 < $2"; else bad "$1 < $2 (expected true)"; fi; }
not_lt() { if version_lt "$1" "$2"; then bad "NOT $1 < $2 (expected false)"; else ok "NOT $1 < $2"; fi; }

# detect_downgrade expectations:
#   down/safe <label> <inst_ch> <inst_ref> <tgt_ch> <tgt_ref> <inst_nc> <tgt_nc>
down() { if detect_downgrade "$2" "$3" "$4" "$5" "$6" "$7" >/dev/null; then ok "$1"; else bad "$1 (expected DOWNGRADE)"; fi; }
safe() { if detect_downgrade "$2" "$3" "$4" "$5" "$6" "$7" >/dev/null; then bad "$1 (expected allowed)"; else ok "$1"; fi; }

echo "== version_lt =="
lt 32.0.3 32.0.9
lt 1.1.0 1.2.0
lt 1.9.0 1.10.0          # numeric, not lexical ordering
lt 31.0.10 32.0.0
lt 32.0.3-rc1 32.0.9     # non-numeric trailing junk is stripped
not_lt 32.0.9 32.0.3
not_lt 32.0.9 32.0.9     # equal is not strictly-less-than
not_lt 1.2.0 1.1.0
not_lt 1.10.0 1.9.0

echo "== parse_nc_tag =="
tmp="$(mktemp)"
printf '  nextcloud:\n    image: nextcloud:32.0.9-apache\n' > "$tmp"
got="$(parse_nc_tag "$tmp")"
if [ "$got" = "32.0.9" ]; then ok "parse_nc_tag -> 32.0.9"; else bad "parse_nc_tag -> '$got'"; fi
: > "$tmp"  # no nextcloud line
got="$(parse_nc_tag "$tmp")"
if [ -z "$got" ]; then ok "parse_nc_tag -> empty when absent"; else bad "parse_nc_tag -> '$got' (expected empty)"; fi
rm -f "$tmp"

echo "== detect_downgrade: should BLOCK =="
# The reported incident: beta (main, NC 32.0.9) -> stable v1.1.0 (NC 32.0.3).
down "beta->stable + NC regress (user incident)" beta main   stable v1.1.0 32.0.9 32.0.3
down "beta->stable, NC equal"                    beta main   stable v1.1.0 32.0.9 32.0.9
down "dev->stable (dev tracks main too)"         dev  main   stable v1.1.0 32.0.9 32.0.9
down "stable->older stable tag"                  stable v1.2.0 stable v1.1.0 32.0.9 32.0.9
down "NC regress, no version.json"               ""   ""      stable v1.1.0 32.0.9 32.0.3
down "NC regress within same stable tag"         stable v1.1.0 stable v1.1.0 32.0.9 32.0.3

echo "== detect_downgrade: should ALLOW =="
safe "beta->beta, NC equal (routine)"   beta main   beta main   32.0.9 32.0.9
safe "beta->beta, NC forward"           beta main   beta main   32.0.8 32.0.9
safe "dev->dev (both track main)"       dev  main   dev  main   32.0.9 32.0.9
safe "stable->dev (forward to main)"    stable v1.1.0 dev  main  32.0.3 32.0.9
safe "stable->newer stable tag"         stable v1.1.0 stable v1.2.0 32.0.3 32.0.9
safe "stable->beta (forward)"           stable v1.1.0 beta main   32.0.3 32.0.9
safe "stable re-run same tag"           stable v1.1.0 stable v1.1.0 32.0.9 32.0.9
safe "fresh install (no signals)"       ""   ""      stable v1.1.0 ""     32.0.3
safe "beta->beta, NC unknown"           beta main   beta main   ""     ""

echo "== nc_status_needs_upgrade =="
needs() { if printf '%s' "$2" | nc_status_needs_upgrade; then ok "$1"; else bad "$1 (expected needs-upgrade)"; fi; }
clean() { if printf '%s' "$2" | nc_status_needs_upgrade; then bad "$1 (expected up-to-date)"; else ok "$1"; fi; }
needs "needsDbUpgrade: true"  "$(printf -- '  - installed: true\n  - needsDbUpgrade: true\n  - maintenance: false\n')"
clean "needsDbUpgrade: false" "$(printf -- '  - installed: true\n  - needsDbUpgrade: false\n  - maintenance: false\n')"
clean "field absent"          "$(printf -- '  - installed: true\n  - versionstring: 32.0.9\n')"
needs "tab-indented true"     "$(printf -- '\t- needsDbUpgrade:   true\n')"

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
