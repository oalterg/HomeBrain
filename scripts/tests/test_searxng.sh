#!/usr/bin/env bash
#
# SearXNG web search: the plugin pin, the per-box secret, and the settings
# the agent depends on. No Docker, npm or OpenClaw needed.
#
#   bash scripts/tests/test_searxng.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

pass=0
fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n        %s\n' "$1" "$2"; fail=$((fail + 1)); }

extract_fn() {
    awk -v fn="$2" '
        $0 == fn "() {" { inside = 1 }
        inside          { print }
        inside && $0 == "}" { exit }
    ' "$1"
}
eval "$(extract_fn "$ROOT/scripts/utilities.sh" install_searxng_plugin)"
eval "$(extract_fn "$ROOT/scripts/common.sh" ensure_searxng_secret)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
CALLS="$TMP/calls"
log_info() { :; }
log_warn() { echo "warn $*" >> "$CALLS"; }
# Stand-ins for the OpenClaw CLI: `installed` holds the version inspect reports.
openclaw() { :; }
searxng_plugin_version() { cat "$TMP/installed" 2>/dev/null || true; }
run_as_admin() {
    echo "$*" >> "$CALLS"
    case "$*" in
        *'plugins install'*) [[ -f "$TMP/install_fails" ]] || echo "${4##*@}" > "$TMP/installed" ;;
    esac
}

echo "== install_searxng_plugin =="
SEARXNG_PLUGIN_VERSION=2026.7.35

: > "$CALLS"; rm -f "$TMP/installed"
install_searxng_plugin
if grep -q 'plugins install @openclaw/searxng-plugin@2026.7.35 --force --accept-capabilities' "$CALLS" && [[ "$(cat "$TMP/installed")" == 2026.7.35 ]]; then
    ok "missing plugin installs at the pin, consenting to its capabilities"
else
    bad "missing plugin installs at the pin, consenting to its capabilities" "$(cat "$CALLS")"
fi

: > "$CALLS"
install_searxng_plugin
if ! grep -q 'plugins install' "$CALLS"; then
    ok "plugin at the pin is left alone"
else
    bad "plugin at the pin is left alone" "reinstalled"
fi

echo 2026.7.1 > "$TMP/installed"; : > "$CALLS"
install_searxng_plugin
if grep -q -- '--force' "$CALLS" && [[ "$(cat "$TMP/installed")" == 2026.7.35 ]]; then
    ok "an older plugin is replaced with --force"
else
    bad "an older plugin is replaced with --force" "$(cat "$CALLS")"
fi

echo 2026.7.1 > "$TMP/installed"; touch "$TMP/install_fails"; : > "$CALLS"
install_searxng_plugin
if grep -q '^warn .*did not install' "$CALLS"; then
    ok "a failed install is reported, not assumed"
else
    bad "a failed install is reported, not assumed" "no warning"
fi
rm -f "$TMP/install_fails"

echo "== ensure_searxng_secret =="
update_env_var() { echo "$1=$2" >> "$TMP/env"; }
unset SEARXNG_SECRET
ensure_searxng_secret
first="$(printenv SEARXNG_SECRET || true)"
if [[ "$first" =~ ^[0-9a-f]{64}$ ]] && grep -q "^SEARXNG_SECRET=$first$" "$TMP/env"; then
    ok "empty secret is generated, stored and exported for compose"
else
    bad "empty secret is generated, stored and exported for compose" "got '${first}'"
fi
ensure_searxng_secret
if [[ "$SEARXNG_SECRET" == "$first" && "$(wc -l < "$TMP/env" | tr -d ' ')" == 1 ]]; then
    ok "an existing secret is kept"
else
    bad "an existing secret is kept" "rotated"
fi

echo "== shipped config =="
settings="$ROOT/config/searxng/settings.yml"
if grep -qE '^\s+- json$' "$settings"; then
    ok "settings enable the JSON API OpenClaw calls"
else
    bad "settings enable the JSON API OpenClaw calls" "json format missing"
fi
if grep -q '"127.0.0.1:8888:8080"' "$ROOT/docker-compose.yml"; then
    ok "SearXNG publishes on loopback only"
else
    bad "SearXNG publishes on loopback only" "port binding changed"
fi
pin="$(jq -r '.openclaw.searxng_plugin' "$ROOT/config/versions.json")"
oc="$(jq -r '.openclaw.version' "$ROOT/config/versions.json")"
if [[ "$pin" == "$oc" ]]; then
    ok "plugin pin matches the OpenClaw pin"
else
    bad "plugin pin matches the OpenClaw pin" "plugin $pin, openclaw $oc"
fi

echo
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
