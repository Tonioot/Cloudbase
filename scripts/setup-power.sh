#!/usr/bin/env bash
# Sets up what Cloudbase needs from root to look after the power of this server. Safe to run again.
#
#   setup-power.sh [user]
#
# user   the account Cloudbase runs as (default: the account that ran sudo)
#
# 1. the helper that restarts the server (cloudbase-power) and the sudo rule that lets that one account run it
# 2. read access to the processor's energy counter (RAPL), which kernels since 2020 only give to root, so the
#    node can show how much power it uses. The counter can in theory leak what a program computes (PLATYPUS),
#    which is why it was closed; on a server that runs only your own software that is not a concern.
# 3. a Raspberry Pi: membership of the video group, which vcgencmd needs to read the power chip and the
#    power supply state
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

TARGET_USER="${1:-${SUDO_USER:-root}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="/usr/local/lib/cloudbase/power"
SUDOERS_FILE="/etc/sudoers.d/cloudbase-power"
TMPFILES="/etc/tmpfiles.d/cloudbase-rapl.conf"

# 1. restart helper
mkdir -p /usr/local/lib/cloudbase
install -o root -g root -m 755 "$SCRIPT_DIR/cloudbase-power" "$HELPER"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
cat >"$TMP" <<EOT
Defaults:${TARGET_USER} !requiretty
${TARGET_USER} ALL=(root) NOPASSWD: ${HELPER} check, ${HELPER} reboot
EOT
visudo -cf "$TMP" >/dev/null
install -o root -g root -m 440 "$TMP" "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" >/dev/null

# 2. RAPL, re-applied at every boot by systemd-tmpfiles. Only the packages (intel-rapl:0, :1, …) are read.
if [[ -d /sys/devices/virtual/powercap/intel-rapl ]] && command -v systemd-tmpfiles >/dev/null 2>&1; then
  echo 'z /sys/devices/virtual/powercap/intel-rapl/intel-rapl:[0-9]/energy_uj 0444 - - -' > "$TMPFILES"
  systemd-tmpfiles --create "$TMPFILES" || echo "Could not open the processor's energy counter; power use stays hidden"
fi

# 3. Raspberry Pi
if command -v vcgencmd >/dev/null 2>&1 && getent group video >/dev/null && [[ "$TARGET_USER" != "root" ]]; then
  usermod -aG video "$TARGET_USER"
fi

echo "Power readings and restart ready for user ${TARGET_USER}"
