#!/usr/bin/env bash
# Cloudbase installer
#
#   Primary (panel):
#     curl -fsSL https://raw.githubusercontent.com/Tonioot/Cloudbase/main/install.sh | sudo bash
#
#   Node (the panel's "Add node" dialog shows this with a fresh invite code):
#     curl -fsSL https://raw.githubusercontent.com/Tonioot/Cloudbase/main/install.sh \
#       | sudo bash -s -- --node --main-url https://panel.example.com --invite-code <code>
#
#   From a clone, the code stays where it is:
#     sudo bash install.sh
#
# Safe to run again: it updates an existing installation in place.
set -euo pipefail

REPO_URL="https://github.com/Tonioot/Cloudbase.git"
DEFAULT_DIR="/opt/cloudbase"
SERVICE_NAME="cloudbase"
CLI_NAME="cloudbase"
LEGACY_CLI_NAME="pdmanager"
LOG_FILE="/var/log/cloudbase-install.log"

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; RED=$'\033[0;31m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

die()  { printf '\n  %b✗%b %s\n\n' "$RED" "$RESET" "$1" >&2; exit 1; }
note() { printf '  %b·%b %s\n' "$DIM" "$RESET" "$1"; }
warn() { printf '  %b!%b %s\n' "$YELLOW" "$RESET" "$1"; }

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Role
  --primary               Install the panel (default when nothing else is given)
  --node                  Install as a node of an existing primary
  --main-url <url>        The primary's panel URL (node)
  --invite-code <code>    Invite code from "Add node" in the panel (node)
  --node-name <name>      Name shown in the panel (node, default: hostname)

Installation
  --dir <path>            Where the code goes (default: /opt/cloudbase, or the
                          clone this script is in)
  --branch <name>         Git branch to install (default: main)
  --user <name>           User the service runs as (default: the user that ran sudo)
  -y, --yes               Don't ask anything; use defaults
  -h, --help              Show this help
EOF
}

# ── Options ───────────────────────────────────────────────────────────────────
ROLE=""
MAIN_URL=""
INVITE_CODE=""
NODE_NAME=""
INSTALL_DIR=""
BRANCH="main"
RUN_USER=""
ASSUME_YES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --primary)      ROLE="primary" ;;
    --node)         ROLE="node" ;;
    --main-url)     MAIN_URL="${2:-}"; shift ;;
    --invite-code)  INVITE_CODE="${2:-}"; shift ;;
    --node-name)    NODE_NAME="${2:-}"; shift ;;
    --dir)          INSTALL_DIR="${2:-}"; shift ;;
    --branch)       BRANCH="${2:-}"; shift ;;
    --user)         RUN_USER="${2:-}"; shift ;;
    -y|--yes)       ASSUME_YES=true ;;
    --with-nginx|--with-service|--with-cli) ;;  # old flags: everything is always installed
    -h|--help)      usage; exit 0 ;;
    *)              usage; die "Unknown option: $1" ;;
  esac
  shift
done

# Interactive questions read from the terminal, so they also work with curl | bash.
can_ask() { ! $ASSUME_YES && { : </dev/tty; } 2>/dev/null; }
ask() { local reply; read -r -p "  $1" reply </dev/tty; printf '%s' "$reply"; }

# ── Basics ────────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run the installer as root, e.g.:  curl -fsSL …/install.sh | sudo bash"
[[ -d /run/systemd/system ]] || die "Cloudbase needs systemd, which isn't running on this system."

: > "$LOG_FILE"
chmod 600 "$LOG_FILE"
STATE_DIR="$(mktemp -d)"
trap 'rm -rf "$STATE_DIR"' EXIT

if [[ -z "$RUN_USER" ]]; then
  RUN_USER="${SUDO_USER:-root}"
fi
id "$RUN_USER" &>/dev/null || die "User '$RUN_USER' doesn't exist."
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[[ -n "$RUN_HOME" && -d "$RUN_HOME" ]] || die "User '$RUN_USER' has no home directory."

# Everything in ~/.cloudbase and the code directory belongs to the service user.
as_user() {
  if [[ "$RUN_USER" == "root" ]]; then
    HOME="$RUN_HOME" "$@"
  else
    sudo -u "$RUN_USER" -H "$@"
  fi
}

# Code location: the clone this script runs from, or /opt/cloudbase.
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
IN_PLACE=false
if [[ -z "$INSTALL_DIR" && -f "$SCRIPT_PATH" ]]; then
  script_dir="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
  if [[ -f "$script_dir/backend/main.py" ]]; then
    INSTALL_DIR="$script_dir"
    IN_PLACE=true
  fi
fi
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_DIR}"
if [[ -f "$INSTALL_DIR/backend/main.py" && ! -d "$INSTALL_DIR/.git" ]]; then
  IN_PLACE=true
fi

. /etc/os-release 2>/dev/null || true
OS_NAME="${PRETTY_NAME:-Linux}"
ARCH="$(uname -m)"

PKG_MGR=""
for m in apt-get:apt dnf:dnf yum:yum pacman:pacman zypper:zypper; do
  if command -v "${m%%:*}" &>/dev/null; then PKG_MGR="${m##*:}"; break; fi
done
[[ -n "$PKG_MGR" ]] || die "No supported package manager found (apt, dnf, yum, pacman or zypper)."

# ── Banner and role ───────────────────────────────────────────────────────────
printf '\n%b' "$BOLD"
cat <<'EOF'
   _________ .__                   .______.
   \_   ___ \|  |   ____  __ __  __| _/\_ |__ _____    ______ ____
   /    \  \/|  |  /  _ \|  |  \/ __ |  | __ \\__  \  /  ___// __ \
   \     \___|  |_(  <_> )  |  / /_/ |  | \_\ \/ __ \_\___ \\  ___/
    \______  /____/\____/|____/\____ |  |___  (____  /____  >\___  >
           \/                       \/      \/     \/     \/     \/
EOF
printf '%b\n' "$RESET"

if [[ -z "$ROLE" && ( -n "$MAIN_URL" || -n "$INVITE_CODE" ) ]]; then
  ROLE="node"
fi
if [[ -z "$ROLE" ]] && can_ask; then
  printf '  What should this server be?\n\n'
  printf '    %b1%b  Primary — runs the panel, routes traffic, can run apps itself\n' "$BOLD" "$RESET"
  printf '    %b2%b  Node    — runs app instances for an existing primary\n\n' "$BOLD" "$RESET"
  case "$(ask 'Choose [1]: ')" in
    2) ROLE="node" ;;
    *) ROLE="primary" ;;
  esac
  printf '\n'
fi
ROLE="${ROLE:-primary}"

if [[ "$ROLE" == "node" ]]; then
  if [[ -z "$MAIN_URL" || -z "$INVITE_CODE" ]] && [[ -f "$INSTALL_DIR/backend/agent_state.json" ]]; then
    : # already registered; keep the existing connection
  elif [[ -z "$MAIN_URL" || -z "$INVITE_CODE" ]]; then
    can_ask || die "A node needs --main-url and --invite-code. Click \"Add node\" in the panel for the full command."
    note "Click \"Add node\" on the primary's panel for these."
    [[ -n "$MAIN_URL" ]]    || MAIN_URL="$(ask 'Panel URL:   ')"
    [[ -n "$INVITE_CODE" ]] || INVITE_CODE="$(ask 'Invite code: ')"
    printf '\n'
    [[ -n "$MAIN_URL" && -n "$INVITE_CODE" ]] || die "Both the panel URL and the invite code are needed."
  fi
  NODE_NAME="${NODE_NAME:-$(hostname)}"
fi

# ── Pre-flight checks ─────────────────────────────────────────────────────────
printf '  %bInstalling Cloudbase as %s%b\n' "$BOLD" "$( [[ $ROLE == node ]] && echo "a node" || echo "the primary")" "$RESET"
note "$OS_NAME · $ARCH"
note "Code in $INSTALL_DIR, runs as '$RUN_USER', data in $RUN_HOME/.cloudbase"
[[ "$ROLE" == "node" && -n "$MAIN_URL" ]] && note "Connects to $MAIN_URL as '$NODE_NAME'"

case "$ARCH" in
  x86_64|aarch64|arm64|armv7l) ;;
  *) warn "Architecture $ARCH is untested — Docker images may not be available for it." ;;
esac

mem_mb="$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
if (( mem_mb > 0 && mem_mb < 900 )); then
  warn "Only ${mem_mb} MB of memory. Builds of larger apps may fail; 1 GB or more is recommended."
fi

disk_parent="$INSTALL_DIR"
while [[ ! -d "$disk_parent" ]]; do disk_parent="$(dirname "$disk_parent")"; done
disk_mb="$(df -Pm "$disk_parent" 2>/dev/null | awk 'NR==2 {print $4}')"
if [[ -n "$disk_mb" ]] && (( disk_mb < 5000 )); then
  warn "Only ${disk_mb} MB free disk space. Docker images add up quickly; 10 GB or more is recommended."
fi

port_owner() {
  command -v ss &>/dev/null || return 0
  ss -ltnpH "sport = :$1" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -n 1
}
if [[ "$ROLE" == "primary" ]]; then
  for p in 80 443; do
    owner="$(port_owner "$p")"
    if [[ -n "$owner" && "$owner" != "nginx" ]]; then
      warn "Port $p is in use by '$owner'. Cloudbase's nginx needs it for domains and HTTPS."
    fi
  done
fi
printf '\n'

if can_ask; then
  case "$(ask 'Continue? [Y/n] ')" in
    n|N|no|No) die "Cancelled." ;;
  esac
  printf '\n'
fi

# ── Steps ─────────────────────────────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
STEP=0
if [[ "$ROLE" == "primary" ]]; then TOTAL=8; else TOTAL=7; fi
$IN_PLACE && TOTAL=$((TOTAL - 1))

add_warning() { printf '%s\n' "$1" >> "$STATE_DIR/warnings"; }

# Runs a step with its output in the log. Steps run in a background subshell, so
# `set -e` applies inside them and anything they need later goes to $STATE_DIR.
run_step() {
  local title="$1"; shift
  STEP=$((STEP + 1))
  printf '\n── [%d/%d] %s\n' "$STEP" "$TOTAL" "$title" >> "$LOG_FILE"
  ( set -e; "$@" ) >> "$LOG_FILE" 2>&1 < /dev/null &
  local pid=$! spin='|/-\' i=0
  if [[ -t 1 ]]; then
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r  %b%s%b  %s' "$DIM" "${spin:i++%4:1}" "$RESET" "$title"
      sleep 0.15
    done
  fi
  if wait "$pid"; then
    printf '\r  %b✓%b  %s%b  %d/%d%b\n' "$GREEN" "$RESET" "$title" "$DIM" "$STEP" "$TOTAL" "$RESET"
  else
    printf '\r  %b✗%b  %s\n\n' "$RED" "$RESET" "$title"
    printf '  %bLast lines of %s:%b\n' "$DIM" "$LOG_FILE" "$RESET"
    tail -n 20 "$LOG_FILE" | sed 's/^/    /'
    printf '\n  Fix the problem and run the installer again — it picks up where it left off.\n\n'
    exit 1
  fi
}

pkg_install() {
  case "$PKG_MGR" in
    apt)    apt-get install -y "$@" ;;
    dnf)    dnf install -y "$@" ;;
    yum)    yum install -y "$@" ;;
    pacman) pacman -S --needed --noconfirm "$@" ;;
    zypper) zypper --non-interactive install "$@" ;;
  esac
}

step_packages() {
  [[ "$PKG_MGR" == "apt" ]] && apt-get update
  case "$PKG_MGR" in
    apt)    pkg_install ca-certificates curl git lsof sudo iproute2 python3 python3-venv python3-pip ;;
    pacman) pkg_install ca-certificates curl git lsof sudo iproute2 python python-pip ;;
    zypper) pkg_install ca-certificates curl git lsof sudo iproute2 python3 python3-pip ;;
    *)      pkg_install ca-certificates curl git lsof sudo iproute python3 python3-pip ;;
  esac
}

step_docker() {
  if ! command -v docker &>/dev/null; then
    case "$PKG_MGR" in
      apt|dnf|yum) curl -fsSL https://get.docker.com | sh ;;
      pacman)      pkg_install docker ;;
      zypper)      pkg_install docker ;;
    esac
  fi
  systemctl enable --now docker
  if [[ "$RUN_USER" != "root" ]] && ! id -nG "$RUN_USER" | grep -qw docker; then
    usermod -aG docker "$RUN_USER"
  fi
  docker info >/dev/null

  # Raspberry Pi: Docker's memory stats need the cgroup memory controller. The
  # kernel parameters must stay on the single line of cmdline.txt.
  local cmdline=""
  if   [[ -f /boot/firmware/cmdline.txt ]]; then cmdline=/boot/firmware/cmdline.txt
  elif [[ -f /boot/cmdline.txt ]];          then cmdline=/boot/cmdline.txt
  fi
  if [[ -n "$cmdline" ]] && ! grep -q "cgroup_enable=memory" "$cmdline"; then
    printf '%s cgroup_enable=memory cgroup_memory=1\n' "$(tr '\n' ' ' < "$cmdline" | sed 's/[[:space:]]*$//')" > "$cmdline"
    touch "$STATE_DIR/reboot"
  fi
}

step_web() {
  pkg_install nginx
  if ! command -v certbot &>/dev/null && ! pkg_install certbot; then
    if ! { [[ "$PKG_MGR" == "zypper" ]] && pkg_install python3-certbot; }; then
      add_warning "certbot couldn't be installed, so automatic HTTPS won't work. Install certbot and run: cloudbase nginx permissions"
    fi
  fi
  systemctl enable --now nginx
}

step_code() {
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    chown -R "$RUN_USER": "$INSTALL_DIR"
    as_user git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    as_user git -C "$INSTALL_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
    as_user git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
  else
    if [[ -e "$INSTALL_DIR" ]] && [[ -n "$(ls -A "$INSTALL_DIR")" ]]; then
      echo "$INSTALL_DIR exists, isn't empty and isn't a Cloudbase checkout"
      return 1
    fi
    mkdir -p "$INSTALL_DIR"
    chown "$RUN_USER": "$INSTALL_DIR"
    as_user git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
}

step_python() {
  # Earlier installers ran parts of this as root; give everything back to the service user.
  chown -R "$RUN_USER": "$INSTALL_DIR"
  [[ -d "$RUN_HOME/.cloudbase" ]] && chown -R "$RUN_USER": "$RUN_HOME/.cloudbase"
  as_user mkdir -p "$RUN_HOME/.cloudbase/apps" "$RUN_HOME/.cloudbase/logs" "$RUN_HOME/.cloudbase/certs"

  # Data from the old name of the project
  if [[ -d "$RUN_HOME/.pdmanager" && ! -f "$RUN_HOME/.cloudbase/cloudbase.db" ]]; then
    for f in pdmanager.db:cloudbase.db credentials:credentials secret_key:secret_key; do
      [[ -f "$RUN_HOME/.pdmanager/${f%%:*}" ]] && as_user cp "$RUN_HOME/.pdmanager/${f%%:*}" "$RUN_HOME/.cloudbase/${f##*:}"
    done
  fi

  cd "$INSTALL_DIR/backend"
  if [[ ! -x venv/bin/pip ]]; then
    rm -rf venv
    as_user python3 -m venv venv
  fi
  as_user venv/bin/pip install --upgrade pip
  as_user venv/bin/pip install -r requirements.txt
}

step_nginx_config() {
  mkdir -p /var/www/cloudbase/maintenance
  chmod 755 /var/www/cloudbase/maintenance
  bash "$INSTALL_DIR/scripts/setup-nginx-permissions.sh" "$RUN_USER"

  mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
  # The stock Debian/Ubuntu site also claims default_server and would make `nginx -t` fail.
  rm -f /etc/nginx/sites-enabled/default
  # Unknown hostnames must never end up at a random app. Cloudbase replaces this
  # with its own page once the panel has a domain.
  cat > /etc/nginx/sites-available/cloudbase-default <<'NGINX_EOF'
# Cloudbase default catch-all — rejects requests for unknown hostnames.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    return 444;
}
NGINX_EOF
  ln -sf /etc/nginx/sites-available/cloudbase-default /etc/nginx/sites-enabled/cloudbase-default
  if nginx -t; then
    systemctl reload nginx
  else
    add_warning "nginx's configuration test fails — check it with: sudo nginx -t"
  fi
}

step_admin() {
  [[ -f "$RUN_HOME/.cloudbase/credentials" ]] && return 0
  local pass
  pass="$(python3 -c "import secrets, string; a = string.ascii_letters + string.digits; print(''.join(secrets.choice(a) for _ in range(20)))")"
  cd "$INSTALL_DIR/backend"
  as_user env CB_PASS="$pass" venv/bin/python -c "import os, auth; auth.save_hashed_password(auth.hash_password(os.environ['CB_PASS']))"
  printf '%s' "$pass" | as_user tee "$RUN_HOME/.cloudbase/.first-run-password" > /dev/null
  printf '%s' "$pass" > "$STATE_DIR/password"
}

step_register() {
  cd "$INSTALL_DIR/backend"
  as_user sh -c 'mkdir -p "$HOME/.cloudbase" && printf node-only > "$HOME/.cloudbase/mode"'
  if [[ -z "$MAIN_URL" || -z "$INVITE_CODE" ]]; then
    echo "No new invite given — keeping the existing registration"
    return 0
  fi
  # A new invite replaces an earlier connection.
  rm -f agent_state.json
  as_user venv/bin/python node_agent.py --main-url "$MAIN_URL" --invite-code "$INVITE_CODE" \
    --node-name "$NODE_NAME" --exit-after-registration
  [[ -f agent_state.json ]]
}

step_service() {
  printf '#!/usr/bin/env bash\nexec /bin/bash "%s/start.sh" "$@"\n' "$INSTALL_DIR" > "/usr/local/bin/$CLI_NAME"
  printf '#!/usr/bin/env bash\nexec /usr/local/bin/%s "$@"\n' "$CLI_NAME" > "/usr/local/bin/$LEGACY_CLI_NAME"
  chmod 755 "/usr/local/bin/$CLI_NAME" "/usr/local/bin/$LEGACY_CLI_NAME"

  cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Cloudbase
After=network.target docker.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/bin/bash $INSTALL_DIR/start.sh run
Restart=on-failure
RestartSec=5
KillMode=process
TimeoutStopSec=15
Delegate=yes

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"

  # The first start installs nothing new, but give it time to come up.
  local port
  port="$(awk '/^server:/{s=1;next} /^[^[:space:]#]/{s=0} s && $1=="port:"{print $2; exit}' "$RUN_HOME/.cloudbase/config.yaml" 2>/dev/null || true)"
  [[ "$port" =~ ^[0-9]+$ ]] || port=7823
  printf '%s' "$port" > "$STATE_DIR/port"
  for _ in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$port/api/health" >/dev/null 2>&1 && return 0
    sleep 2
  done
  journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
  echo "Cloudbase didn't answer on port $port within two minutes"
  return 1
}

run_step "Installing system packages" step_packages
run_step "Installing Docker" step_docker
[[ "$ROLE" == "primary" ]] && run_step "Installing nginx and certbot" step_web
$IN_PLACE || run_step "Getting Cloudbase ($BRANCH)" step_code
run_step "Setting up Python environment" step_python
if [[ "$ROLE" == "primary" ]]; then
  run_step "Configuring nginx" step_nginx_config
  run_step "Creating administrator account" step_admin
else
  run_step "Registering with the primary" step_register
fi
run_step "Starting Cloudbase" step_service

# ── Summary ───────────────────────────────────────────────────────────────────
PORT="$(cat "$STATE_DIR/port" 2>/dev/null || echo 7823)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP="${IP:-<server-ip>}"

printf '\n'
if [[ -f "$STATE_DIR/warnings" ]]; then
  while IFS= read -r line; do warn "$line"; done < "$STATE_DIR/warnings"
  printf '\n'
fi

if [[ "$ROLE" == "primary" ]]; then
  printf '  %bCloudbase is running.%b\n\n' "$BOLD" "$RESET"
  printf '    Panel      %bhttp://%s:%s%b\n' "$BOLD" "$IP" "$PORT" "$RESET"
  if [[ -f "$STATE_DIR/password" ]]; then
    printf '    Username   admin\n'
    printf '    Password   %b%s%b\n' "$GREEN" "$(cat "$STATE_DIR/password")" "$RESET"
    printf '               %bStore it now — it isn'\''t shown again.%b\n' "$DIM" "$RESET"
  fi
  printf '\n  Next steps\n'
  note "Make ports 80 and 443 reachable from the internet"
  note "Connect a domain to the panel under Settings → Domain & SSL for HTTPS"
  note "Close port $PORT once the panel has a domain"
  note "The Guides page in the panel covers the rest"
else
  printf '  %bThis server is now a Cloudbase node.%b\n\n' "$BOLD" "$RESET"
  if [[ -n "$MAIN_URL" ]]; then
    note "It appears under Nodes on $MAIN_URL within a few seconds"
  fi
  note "Add instances to it from the panel; this server has no panel of its own"
  note "Check the connection with: cloudbase node-status"
fi
printf '\n'
note "Installer log: $LOG_FILE"
if [[ -f "$STATE_DIR/reboot" ]]; then
  printf '\n'
  warn "Reboot once to enable Docker's memory statistics:  sudo reboot"
fi
printf '\n'
