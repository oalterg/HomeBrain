#!/bin/bash
# `init` (once, interactively): create the pass store Bridge keeps its
# credentials in, then open the Bridge CLI to `login`.
# Otherwise: run Bridge and expose its loopback-only IMAP (1143) and SMTP
# (1025) on the container ports compose publishes to the host's loopback.
set -euo pipefail

if [[ "${1:-}" == "init" ]]; then
    if [[ ! -f /root/.password-store/.gpg-id ]]; then
        gpg --batch --pinentry-mode loopback --passphrase '' \
            --quick-gen-key proton-bridge default default never
        pass init proton-bridge
    fi
    exec bridge --cli
fi

socat TCP-LISTEN:143,fork,reuseaddr TCP:127.0.0.1:1143 &
socat TCP-LISTEN:25,fork,reuseaddr TCP:127.0.0.1:1025 &
exec bridge --noninteractive
