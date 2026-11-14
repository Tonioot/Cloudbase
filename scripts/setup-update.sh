#!/usr/bin/env bash
# Installs the root helper that lets Cloudbase finish its own updates (see cloudbase-update). Safe to run again.
#
#   setup-update.sh [user]
#
# user   the account Cloudbase runs as (default: the account that ran sudo)
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

TARGET_USER="${1:-${SUDO_USER:-root}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="/usr/local/lib/cloudbase/update"
SUDOERS_FILE="/etc/sudoers.d/cloudbase-update"

mkdir -p /usr/local/lib/cloudbase
install -o root -g root -m 755 "$SCRIPT_DIR/cloudbase-update" "$HELPER"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
cat >"$TMP" <<EOF
Defaults:${TARGET_USER} !requiretty
${TARGET_USER} ALL=(root) NOPASSWD: ${HELPER} check, ${HELPER} run, ${HELPER} run *
EOF
visudo -cf "$TMP" >/dev/null
install -o root -g root -m 440 "$TMP" "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" >/dev/null

echo "Update helper ready for user ${TARGET_USER}"
