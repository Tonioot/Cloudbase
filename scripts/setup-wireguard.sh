#!/usr/bin/env bash
# Sets up the node network (WireGuard) for Cloudbase on this server. Safe to run again.
#
#   setup-wireguard.sh [user] [--hub]
#
# user   the account Cloudbase runs as (default: the account that ran sudo)
# --hub  this is the primary: also open the WireGuard port in the firewall
#
# It installs wireguard-tools, the root helper Cloudbase uses to manage the interface (see
# cloudbase-wireguard), the sudo rule that lets that one account run only that helper, and a
# systemd unit that brings the interface up at boot, before Docker (instances publish their
# port on the node-network address, which must exist when their container starts).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

TARGET_USER=""
HUB=0
for arg in "$@"; do
  case "$arg" in
    --hub) HUB=1 ;;
    *) TARGET_USER="$arg" ;;
  esac
done
TARGET_USER="${TARGET_USER:-${SUDO_USER:-root}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="/usr/local/lib/cloudbase/wireguard"
SUDOERS_FILE="/etc/sudoers.d/cloudbase-wireguard"
UNIT_FILE="/etc/systemd/system/cloudbase-wg.service"
PORT="${CLOUDBASE_WG_PORT:-51820}"

# 1. wireguard-tools
if ! command -v wg >/dev/null 2>&1 || ! command -v wg-quick >/dev/null 2>&1; then
  echo "Installing wireguard-tools"
  if   command -v apt-get >/dev/null 2>&1; then DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard-tools
  elif command -v dnf     >/dev/null 2>&1; then dnf install -y wireguard-tools
  elif command -v yum     >/dev/null 2>&1; then yum install -y wireguard-tools
  elif command -v pacman  >/dev/null 2>&1; then pacman -S --noconfirm wireguard-tools
  elif command -v zypper  >/dev/null 2>&1; then zypper --non-interactive install wireguard-tools
  else echo "Install wireguard-tools by hand, then run this script again." >&2; exit 1
  fi
fi

# The kernel must have WireGuard (built in or as a module; all current distribution kernels do)
if ! modprobe wireguard 2>/dev/null && ! ip link add cbwg-probe type wireguard 2>/dev/null; then
  echo "This kernel has no WireGuard support, so the node network cannot work here." >&2
  exit 1
fi
ip link del cbwg-probe 2>/dev/null || true

# 2. the helper
mkdir -p /usr/local/lib/cloudbase
install -o root -g root -m 755 "$SCRIPT_DIR/cloudbase-wireguard" "$HELPER"

# 3. sudo: that account may run that helper, and nothing else through this rule
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
cat >"$TMP" <<EOF
Defaults:${TARGET_USER} !requiretty
${TARGET_USER} ALL=(root) NOPASSWD: ${HELPER} *
EOF
visudo -cf "$TMP" >/dev/null
install -o root -g root -m 440 "$TMP" "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" >/dev/null

# 4. up at boot, before Docker. It only starts when the interface has been configured before.
cat >"$UNIT_FILE" <<EOF
[Unit]
Description=Cloudbase node network (WireGuard)
After=network-online.target
Wants=network-online.target
Before=docker.service
ConditionPathExists=/etc/cloudbase-wg/state.json

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${HELPER} boot
ExecStop=${HELPER} down

[Install]
WantedBy=multi-user.target
EOF
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
  systemctl enable cloudbase-wg.service >/dev/null 2>&1 || true
fi

# 5. firewall (only when ufw is in use): traffic over the node network is trusted; the primary also listens
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow in on cbwg0 >/dev/null
  if [[ $HUB -eq 1 ]]; then
    ufw allow "${PORT}/udp" >/dev/null
    echo "Firewall: opened ${PORT}/udp for the node network"
  fi
fi

echo "Node network ready for user ${TARGET_USER}"
if [[ $HUB -eq 1 ]]; then
  echo "Open UDP port ${PORT} on your router/cloud firewall if nodes connect from outside this network."
fi
