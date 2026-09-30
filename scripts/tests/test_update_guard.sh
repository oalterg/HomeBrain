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

echo "== get_runtime_profiles =="
# Isolate tunnel detection from the developer machine's environment.
unset PANGOLIN_ENDPOINT NEWT_ID NEWT_SECRET CF_TOKEN_NC CF_TOKEN_HA PANGOLIN_DOMAIN
docker() {
    if [ "$1" = "ps" ]; then
        echo "homebrain-proton-bridge-1"
        return 0
    fi
    printf 'unexpected docker %s\n' "$*" >&2
    return 1
}
got="$(get_runtime_profiles 2>/dev/null || true)"
if printf '%s' "$got" | grep -q -- '--profile proton-bridge'; then
    ok "proton-bridge profile kept when the container is running"
else
    bad "proton-bridge profile kept when the container is running (got '$got')"
fi
docker() {
    if [ "$1" = "ps" ]; then
        return 0
    fi
    return 1
}
got="$(get_runtime_profiles 2>/dev/null || true)"
if printf '%s' "$got" | grep -q -- 'proton-bridge'; then
    bad "no proton-bridge profile when the container is down (got '$got')"
else
    ok "no proton-bridge profile when the container is down"
fi
unset -f docker

UPDATE_SH="$SCRIPT_DIR/../update.sh"
if grep -q 'get_runtime_profiles' "$UPDATE_SH"; then
    ok "update.sh compose pull/up uses get_runtime_profiles"
else
    bad "update.sh compose pull/up uses get_runtime_profiles"
fi

echo "== pin_lags =="
if pin_lags 2026.7.35 2026.7.1-2; then ok "an install behind its pin lags"; else bad "an install behind its pin lags"; fi
if pin_lags 2026.8.33 2026.8.33; then bad "an install at its pin does not lag"; else ok "an install at its pin does not lag"; fi
if pin_lags 2026.8.33 ""; then bad "an unknown install is not drift"; else ok "an unknown install is not drift"; fi
if pin_lags "" 2026.8.33; then bad "no pin, no drift"; else ok "no pin, no drift"; fi

echo "== installed_openclaw_version =="
iv_tmp="$(mktemp -d)"
echo '{"openclaw":{"version":"2026.7.35"}}' > "$iv_tmp/record.json"
echo '{"llama_cpp":{"tag":"b10361"}}' > "$iv_tmp/no-openclaw.json"
openclaw() { [[ "$1" == "--version" ]] && echo "OpenClaw 2026.7.1-2 (0790d9f)"; }
got=$(installed_openclaw_version "$iv_tmp/record.json")
[[ "$got" == 2026.7.35 ]] && ok "the install record wins" || bad "the install record wins (got $got)"
got=$(installed_openclaw_version "$iv_tmp/no-openclaw.json")
[[ "$got" == 2026.7.1-2 ]] && ok "without a record, the CLI's own version" || bad "without a record, the CLI's own version (got $got)"
got=$(installed_openclaw_version "$iv_tmp/missing.json")
[[ "$got" == 2026.7.1-2 ]] && ok "a missing record file falls back to the CLI" || bad "a missing record file falls back to the CLI (got $got)"
unset -f openclaw
got=$(PATH=/nonexistent installed_openclaw_version "$iv_tmp/no-openclaw.json")
[[ -z "$got" ]] && ok "neither record nor CLI: unknown" || bad "neither record nor CLI: unknown (got $got)"
rm -rf "$iv_tmp"

echo "== needrestart leaves HomeBrain jobs alone =="
nr_tmp="$(mktemp -d)"
NEEDRESTART_GUARD="$nr_tmp/absent/homebrain.conf" install_needrestart_guard
[[ ! -e "$nr_tmp/absent/homebrain.conf" ]] && ok "no needrestart, nothing written" || bad "no needrestart, nothing written"
mkdir -p "$nr_tmp/conf.d"
NEEDRESTART_GUARD="$nr_tmp/conf.d/homebrain.conf"
install_needrestart_guard
if command -v perl >/dev/null 2>&1; then
    # Load it the way needrestart.conf does, then match unit names the way
    # needrestart's restart loop does: first key in sorted order wins.
    verdicts=$(perl -e '
        our %nrconf = (override_rc => { qr(^dbus) => 0 });
        do $ARGV[0]; die $@ if $@;
        for my $rc (@ARGV[1..$#ARGV]) {
            my $restart = 1;
            for my $re (sort keys %{$nrconf{override_rc}}) {
                next unless $rc =~ /$re/;
                $restart = $nrconf{override_rc}{$re}; last;
            }
            print "$rc=$restart ";
        }' "$NEEDRESTART_GUARD" \
        homebrain-update.service homebrain-ai-setup.service homebrain-backup.service \
        homebrain-offsite.service homebrain-picture.service homebrain-video.service \
        homebrain-gpu-recover.service homebrain-manager.service homebrain-media.service \
        homebrain-update-helper.service dbus.service 2>&1)
    want="homebrain-update.service=0 homebrain-ai-setup.service=0 homebrain-backup.service=0 homebrain-offsite.service=0 homebrain-picture.service=0 homebrain-video.service=0 homebrain-gpu-recover.service=0 homebrain-manager.service=1 homebrain-media.service=1 homebrain-update-helper.service=1 dbus.service=0 "
    [[ "$verdicts" == "$want" ]] && ok "jobs are never restarted, daemons still are" \
        || bad "jobs are never restarted, daemons still are: $verdicts"
else
    bad "perl is needed to check the needrestart guard"
fi
rm -rf "$nr_tmp"

echo "== update rsync keeps box state =="
# Every INSTALL_DIR path the code writes that the tarball does not ship must
# survive the sync. Run update.sh's own exclude list through a real
# `rsync --delete` from a tree that lacks them all.
REPO_ROOT="$SCRIPT_DIR/../.."
excludes=()
while IFS= read -r pat; do
    excludes+=("--exclude=$pat")
done < <(sed -n '/^rsync -a --delete/,/"\$INSTALL_DIR\/"/p' "$UPDATE_SH" \
    | grep -oE "exclude='[^']+'" | sed -E "s/^exclude='(.*)'$/\1/")
state=()
while IFS= read -r name; do
    [[ -e "$REPO_ROOT/$name" ]] || state+=("$name")
done < <(grep -rhoE '(\$INSTALL_DIR|\$\{INSTALL_DIR\}|\{INSTALL_DIR\}|/opt/homebrain)/[A-Za-z0-9_.][A-Za-z0-9_.-]*' \
            "$REPO_ROOT/scripts" "$REPO_ROOT/src" --include='*.sh' --include='*.py' \
         | sed -E 's#.*/##; /\.$/d' | sort -u)
if [[ ${#excludes[@]} -lt 5 || ${#state[@]} -lt 5 ]]; then
    bad "found the rsync excludes (${#excludes[@]}) and the box-state paths (${#state[@]})"
else
    sync_tmp="$(mktemp -d)"
    mkdir -p "$sync_tmp/src/scripts" "$sync_tmp/dst"
    touch "$sync_tmp/src/scripts/update.sh"
    for name in "${state[@]}"; do
        echo keep > "$sync_tmp/dst/$name"
    done
    echo stale > "$sync_tmp/dst/removed_upstream.sh"
    rsync -a --delete "${excludes[@]}" "$sync_tmp/src/" "$sync_tmp/dst/"
    for name in "${state[@]}"; do
        if [[ -e "$sync_tmp/dst/$name" ]]; then
            ok "update keeps $name"
        else
            bad "update deletes $name (add --exclude='$name' to update.sh)"
        fi
    done
    if [[ -e "$sync_tmp/dst/removed_upstream.sh" ]]; then
        bad "update still deletes files the tarball dropped"
    else
        ok "update still deletes files the tarball dropped"
    fi
    rm -rf "$sync_tmp"
fi

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
