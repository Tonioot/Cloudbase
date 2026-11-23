#!/usr/bin/env bash
# Sets up disk limits for databases and the counting of apps' internet traffic. Safe to run again.
#
#   setup-quota.sh [user]
#
# user   the account Cloudbase runs as (default: the account that ran sudo)
#
# It installs the root helper (see cloudbase-quota), the sudo rule that lets that one account run only that
# helper, and a systemd unit that mounts the databases' disks at boot, before Docker starts the databases.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

TARGET_USER="${1:-${SUDO_USER:-root}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="/usr/local/lib/cloudbase/quota"
SUDOERS_FILE="/etc/sudoers.d/cloudbase-quota"
UNIT_FILE="/etc/systemd/system/cloudbase-disks.service"
DISK_DIR="/var/lib/cloudbase-disks"

# 1. the programs it uses: fallocate/losetup/mount (util-linux), mkfs.ext4/resize2fs (e2fsprogs), iptables
missing=()
for program in fallocate losetup mkfs.ext4 resize2fs iptables; do
  command -v "$program" >/dev/null 2>&1 || missing+=("$program")
done
if (( ${#missing[@]} )); then
  echo "Installing what is missing: ${missing[*]}"
  if   command -v apt-get >/dev/null 2>&1; then DEBIAN_FRONTEND=noninteractive apt-get install -y util-linux e2fsprogs iptables
  elif command -v dnf     >/dev/null 2>&1; then dnf install -y util-linux e2fsprogs iptables
  elif command -v yum     >/dev/null 2>&1; then yum install -y util-linux e2fsprogs iptables
  elif command -v pacman  >/dev/null 2>&1; then pacman -S --noconfirm --needed util-linux e2fsprogs iptables
  elif command -v zypper  >/dev/null 2>&1; then zypper --non-interactive install util-linux e2fsprogs iptables
  else echo "Install ${missing[*]} by hand, then run this script again." >&2; exit 1
  fi
fi

# 2. the helper, and where the disks live
mkdir -p /usr/local/lib/cloudbase
install -o root -g root -m 755 "$SCRIPT_DIR/cloudbase-quota" "$HELPER"
mkdir -p "$DISK_DIR"
chmod 755 "$DISK_DIR"

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

# 4. the disks are mounted at boot, before Docker starts the databases on them
cat >"$UNIT_FILE" <<EOF
[Unit]
Description=Cloudbase database disks
After=local-fs.target
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${HELPER} boot

[Install]
WantedBy=multi-user.target
EOF
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
  systemctl enable cloudbase-disks.service >/dev/null 2>&1 || true
fi

echo "Disk limits and traffic counting ready for user ${TARGET_USER}"
