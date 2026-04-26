#!/usr/bin/env bash
# Internal script called by the dashboard update flow.
# Usage: update-deps.sh [llama_cpp|openclaw|all]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"
source "$SCRIPT_DIR/utilities.sh"

case "${1:-all}" in
  llama_cpp) install_llamacpp ;;
  openclaw)  setup_openclaw ;;
  all)       install_llamacpp; setup_openclaw ;;
  *)         echo "Unknown target: $1"; exit 1 ;;
esac
