#!/usr/bin/env bash
# Cloudbase installer — also updates, converts and removes an installation.
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
# The cloudbase CLI runs this script for `update`, `connect`, `disconnect` and
# `uninstall`. Running it again on an installed server updates it in place.
set -euo pipefail

REPO_URL="https://github.com/Tonioot/Cloudbase.git"
DEFAULT_DIR="/opt/cloudbase"
SERVICE_NAME="cloudbase"
SERVICE_FILE="/etc/systemd/system/cloudbase.service"
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
  --primary               Install the panel (default for a new server)
  --node                  Install as a node of an existing primary
  --main-url <url>        The primary's panel URL (node)
  --invite-code <code>    Invite code from "Add node" in the panel (node)
  --node-name <name>      Name shown in the panel (node, default: hostname)

Maintenance
  --update                Update an installed server to the latest version
  --uninstall             Remove Cloudbase from this server
  --keep-data             With --uninstall: keep ~/.cloudbase (database, settings)

Installation
  --dir <path>            Where the code goes (default: /opt/cloudbase, or the
                          clone this script is in)
  --branch <name>         Git branch to install (default: main)
  --user <name>           User the service runs as (default: the user that ran sudo)
  -y, --yes               Don't ask anything
  -h, --help              Show this help
EOF
}

# ── Options ───────────────────────────────────────────────────────────────────
ACTION="install"
ROLE=""
MAIN_URL=""
INVITE_CODE=""
NODE_NAME=""
INSTALL_DIR=""
BRANCH="main"
RUN_USER=""
ASSUME_YES=false
KEEP_DATA=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --primary)      ROLE="primary" ;;
    --node)         ROLE="node" ;;
    --main-url)     MAIN_URL="${2:-}"; shift ;;
    --invite-code)  INVITE_CODE="${2:-}"; shift ;;
    --node-name)    NODE_NAME="${2:-}"; shift ;;
    --update)       ACTION="update" ;;
    --uninstall)    ACTION="uninstall" ;;
    --keep-data)    KEEP_DATA=true ;;
    --dir)          INSTALL_DIR="${2:-}"; shift ;;
    --branch)       BRANCH="${2:-}"; shift ;;
    --user)         RUN_USER="${2:-}"; shift ;;
    -y|--yes)       ASSUME_YES=true ;;
    --mode)
      # Older panels showed `cloudbase connect … --mode node-only`.
      [[ "${2:-}" == "node-only" ]] || die "The '${2:-}' mode no longer exists. A server is either a primary or a node."
      ROLE="node"; shift ;;
    --with-nginx|--with-service|--with-cli) ;;  # old flags: everything is always installed
    -h|--help)      usage; exit 0 ;;
    *)              usage; die "Unknown option: $1" ;;
  esac
  shift
done

# Interactive questions read from the terminal, so they also work with curl | bash.
can_ask() { ! $ASSUME_YES && { : </dev/tty; } 2>/dev/null; }
ask() { local reply; read -r -p "  $1" reply </dev/tty; printf '%s' "$reply"; }
confirm() {  # confirm <question> <default y|n>
  local reply
  reply="$(ask "$1 $( [[ $2 == y ]] && echo '[Y/n]' || echo '[y/N]') ")"
  printf '\n'
  [[ -z "$reply" ]] && reply="$2"
  [[ "$reply" =~ ^[Yy] ]]
}

# ── Basics ────────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run this as root, e.g.:  curl -fsSL …/install.sh | sudo bash"
[[ -d /run/systemd/system ]] || die "Cloudbase needs systemd, which isn't running on this system."

touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
printf '\n==== %s  install.sh %s ====\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$ACTION" >> "$LOG_FILE"
STATE_DIR="$(mktemp -d)"
trap 'rm -rf "$STATE_DIR"' EXIT

# An existing installation decides the user and the directory, unless overridden.
INSTALLED=false
unit_user=""; unit_dir=""
if [[ -f "$SERVICE_FILE" ]]; then
  INSTALLED=true
  unit_user="$(sed -n 's/^User=//p' "$SERVICE_FILE" | head -n 1)"
  unit_dir="$(sed -n 's/^WorkingDirectory=//p' "$SERVICE_FILE" | head -n 1)"
fi

RUN_USER="${RUN_USER:-${unit_user:-${SUDO_USER:-root}}}"
id "$RUN_USER" &>/dev/null || die "User '$RUN_USER' doesn't exist."
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[[ -n "$RUN_HOME" && -d "$RUN_HOME" ]] || die "User '$RUN_USER' has no home directory."
DATA_DIR="$RUN_HOME/.cloudbase"

# Everything in ~/.cloudbase and the code directory belongs to the service user.
as_user() {
  if [[ "$RUN_USER" == "root" ]]; then
    HOME="$RUN_HOME" "$@"
  else
    sudo -u "$RUN_USER" -H "$@"
  fi
}

# Code location: --dir, the clone this script runs from, the installed one, or /opt/cloudbase.
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
IN_PLACE=false
if [[ -z "$INSTALL_DIR" && -f "$SCRIPT_PATH" ]]; then
  script_dir="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
  if [[ -f "$script_dir/backend/main.py" ]]; then
    INSTALL_DIR="$script_dir"
    IN_PLACE=true
  fi
fi
INSTALL_DIR="${INSTALL_DIR:-${unit_dir:-$DEFAULT_DIR}}"
AGENT_STATE="$INSTALL_DIR/backend/agent_state.json"

# The primary a node is connected to, if any.
connected_primary() {
  [[ -f "$AGENT_STATE" ]] && sed -n 's/.*"main_url": *"\([^"]*\)".*/\1/p' "$AGENT_STATE"
  return 0
}

. /etc/os-release 2>/dev/null || true
OS_NAME="${PRETTY_NAME:-Linux}"
ARCH="$(uname -m)"

PKG_MGR=""
for m in apt-get:apt dnf:dnf yum:yum pacman:pacman zypper:zypper; do
  if command -v "${m%%:*}" &>/dev/null; then PKG_MGR="${m##*:}"; break; fi
done

banner() {
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
}

# ── Step runner ───────────────────────────────────────────────────────────────
STEPS=()
add_step() { STEPS+=("$1|$2"); }
add_warning() { printf '%s\n' "$1" >> "$STATE_DIR/warnings"; }

# Each step runs in a background subshell with its output in the log, so `set -e`
# applies inside it. Anything a later step or the summary needs goes to $STATE_DIR.
run_steps() {
  local total=${#STEPS[@]} n=0 entry title fn pid spin='|/-\' i
  for entry in "${STEPS[@]}"; do
    title="${entry%%|*}"; fn="${entry##*|}"; n=$((n + 1)); i=0
    printf '\n── [%d/%d] %s\n' "$n" "$total" "$title" >> "$LOG_FILE"
    ( set -e; "$fn" ) >> "$LOG_FILE" 2>&1 < /dev/null &
    pid=$!
    if [[ -t 1 ]]; then
      while kill -0 "$pid" 2>/dev/null; do
        printf '\r  %b%s%b  %s' "$DIM" "${spin:i++%4:1}" "$RESET" "$title"
        sleep 0.15
      done
    fi
    if wait "$pid"; then
      printf '\r  %b✓%b  %s%b  %d/%d%b\n' "$GREEN" "$RESET" "$title" "$DIM" "$n" "$total" "$RESET"
    else
      printf '\r  %b✗%b  %s\n\n' "$RED" "$RESET" "$title"
      printf '  %bLast lines of %s:%b\n' "$DIM" "$LOG_FILE" "$RESET"
      tail -n 20 "$LOG_FILE" | sed 's/^/    /'
      printf '\n  Fix the problem and run this again — finished steps are skipped or repeated safely.\n\n'
      exit 1
    fi
  done
}

print_warnings() {
  [[ -f "$STATE_DIR/warnings" ]] || return 0
  while IFS= read -r line; do warn "$line"; done < "$STATE_DIR/warnings"
  printf '\n'
}

# ══ Uninstall ═════════════════════════════════════════════════════════════════

# nginx sites Cloudbase wrote: the panel, the catch-all and one per app. All of
# them serve pages from /var/www/cloudbase, which is how they're recognised.
cloudbase_nginx_sites() {
  local f
  [[ -d /etc/nginx/sites-available ]] || return 0
  for f in /etc/nginx/sites-available/*; do
    [[ -f "$f" ]] || continue
    if [[ "$(basename "$f")" == cloudbase* ]] || grep -q "/var/www/cloudbase" "$f"; then
      basename "$f"
    fi
  done
}

app_containers() {
  command -v docker &>/dev/null || return 0
  docker ps -a --filter "name=^cloudbase-app-" --format '{{.Names}}' 2>/dev/null || true
}

step_stop() {
  systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl daemon-reload
  rm -f "/usr/local/bin/$CLI_NAME" "/usr/local/bin/$LEGACY_CLI_NAME"
}

step_remove_containers() {
  local names images
  names="$(app_containers)"
  [[ -n "$names" ]] && xargs -r docker rm -f <<< "$names"
  images="$(docker images --format '{{.Repository}}:{{.Tag}}' 'cloudbase/*' 2>/dev/null || true)"
  [[ -n "$images" ]] && xargs -r docker rmi -f <<< "$images"
  return 0
}

step_remove_nginx() {
  local site
  while IFS= read -r site; do
    [[ -n "$site" ]] || continue
    rm -f "/etc/nginx/sites-enabled/$site" "/etc/nginx/sites-available/$site"
  done < "$STATE_DIR/nginx-sites"
  rm -rf /var/www/cloudbase
  rm -f /etc/sudoers.d/cloudbase-nginx
  rm -rf /usr/local/lib/cloudbase
  if command -v nginx &>/dev/null; then
    if nginx -t; then
      systemctl reload nginx || true
    else
      add_warning "nginx's configuration test fails after removing Cloudbase's sites — check it with: sudo nginx -t"
    fi
  fi
}

step_remove_files() {
  # This script may live in the code directory; bash keeps reading it after removal.
  [[ -n "$INSTALL_DIR" && "$INSTALL_DIR" != "/" ]] && rm -rf "$INSTALL_DIR"
  if ! $KEEP_DATA && [[ -n "$RUN_HOME" && -d "$DATA_DIR" ]]; then
    rm -rf "$DATA_DIR"
  fi
  return 0
}

if [[ "$ACTION" == "uninstall" ]]; then
  $INSTALLED || [[ -f "$INSTALL_DIR/backend/main.py" ]] || die "Cloudbase isn't installed on this server."
  banner

  primary_url="$(connected_primary)"
  cloudbase_nginx_sites > "$STATE_DIR/nginx-sites"
  n_sites="$(grep -c . "$STATE_DIR/nginx-sites" || true)"
  n_containers="$(app_containers | grep -c . || true)"

  printf '  %bRemoving Cloudbase from this server%b\n' "$BOLD" "$RESET"
  note "The service and the cloudbase command"
  (( n_containers > 0 )) && note "$n_containers app container(s) and their images"
  (( n_sites > 0 )) && note "$n_sites nginx site(s) Cloudbase created, its maintenance pages and sudo rule"
  note "The code in $INSTALL_DIR"
  if $KEEP_DATA; then
    note "Keeps $DATA_DIR (database, settings, app sources)"
  else
    note "All data in $DATA_DIR: database, users, settings, app sources"
  fi
  printf '\n'
  note "Docker, nginx, certbot and the certificates in /etc/letsencrypt stay installed"
  if [[ -n "$primary_url" ]]; then
    warn "This is a node of $primary_url. Remove it under Nodes there as well."
  fi
  printf '\n'

  if can_ask; then
    confirm "Remove Cloudbase?" n || die "Cancelled. Nothing was changed."
  elif ! $ASSUME_YES; then
    die "Pass --yes to remove Cloudbase without asking."
  fi

  add_step "Stopping Cloudbase" step_stop
  (( n_containers > 0 )) && add_step "Removing app containers" step_remove_containers
  if (( n_sites > 0 )) || [[ -d /var/www/cloudbase || -f /etc/sudoers.d/cloudbase-nginx ]]; then
    add_step "Removing nginx configuration" step_remove_nginx
  fi
  add_step "Removing files" step_remove_files
  run_steps

  printf '\n'
  print_warnings
  printf '  %bCloudbase is removed.%b\n\n' "$BOLD" "$RESET"
  $KEEP_DATA && note "Your data is still in $DATA_DIR — the installer picks it up again"
  note "Remove Docker, nginx or certbot with your package manager if nothing else uses them"
  note "Log: $LOG_FILE"
  printf '\n'
  exit 0
fi

# ══ Install / update ══════════════════════════════════════════════════════════

[[ -n "$PKG_MGR" ]] || die "No supported package manager found (apt, dnf, yum, pacman or zypper)."
if [[ "$ACTION" == "update" ]]; then
  $INSTALLED || die "Cloudbase isn't installed on this server. Install it first."
fi

banner

if [[ -z "$ROLE" && ( -n "$MAIN_URL" || -n "$INVITE_CODE" ) ]]; then
  ROLE="node"
fi
EXPLICIT_PRIMARY=false
[[ "$ROLE" == "primary" ]] && EXPLICIT_PRIMARY=true

# An installed server keeps its role unless asked otherwise.
CURRENT_ROLE=""
if [[ -f "$AGENT_STATE" ]]; then
  CURRENT_ROLE="node"
elif $INSTALLED || [[ -f "$DATA_DIR/credentials" ]]; then
  CURRENT_ROLE="primary"
fi
[[ -z "$ROLE" ]] && ROLE="$CURRENT_ROLE"

if [[ -z "$ROLE" ]] && can_ask; then
  printf '  What should this server be?\n\n'
  printf '    %b1%b  Primary — runs the panel, routes traffic, can run apps itself\n' "$BOLD" "$RESET"
  printf '    %b2%b  Node    — runs app instances for an existing primary\n\n' "$BOLD" "$RESET"
  case "$(ask 'Choose [1]: ')" in
    2)
      printf '\n  On the primary, click %bAdd node%b on the overview and run the command it shows\n' "$BOLD" "$RESET"
      printf '  on this server. It installs Cloudbase and connects the node in one go.\n\n'
      exit 0
      ;;
    *) ROLE="primary" ;;
  esac
  printf '\n'
fi
ROLE="${ROLE:-primary}"

if [[ "$ROLE" == "node" ]]; then
  # Without a new invite, an already registered node keeps its connection.
  if [[ -z "$MAIN_URL" || -z "$INVITE_CODE" ]] && [[ ! -f "$AGENT_STATE" ]]; then
    die "A node needs --main-url and --invite-code. Click \"Add node\" in the panel for the full command."
  fi
  NODE_NAME="${NODE_NAME:-$(hostname)}"
fi

# Node → primary: disconnect from the old primary.
DISCONNECT_NODE=false
OLD_PRIMARY=""
if [[ "$ROLE" == "primary" && "$CURRENT_ROLE" == "node" ]]; then
  OLD_PRIMARY="$(connected_primary)"
  warn "This server is a node of ${OLD_PRIMARY:-another primary}."
  note "As a primary it gets its own panel, nginx and HTTPS, and stops running instances for ${OLD_PRIMARY:-that primary}."
  printf '\n'
  if can_ask; then
    confirm "Disconnect it and make it a primary?" n || die "Cancelled. Nothing was changed."
  elif ! $EXPLICIT_PRIMARY; then
    die "Pass --primary to disconnect it and make it a primary."
  fi
  DISCONNECT_NODE=true
fi

# Primary → node: only when it has no apps, which would otherwise be left unmanaged.
if [[ "$ROLE" == "node" && "$CURRENT_ROLE" == "primary" ]]; then
  n_apps=0
  if [[ -f "$DATA_DIR/cloudbase.db" ]] && command -v python3 &>/dev/null; then
    n_apps="$(python3 -c "import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from applications').fetchone()[0])" "$DATA_DIR/cloudbase.db" 2>/dev/null || echo 0)"
  fi
  if (( n_apps > 0 )); then
    die "This server is a primary with $n_apps app(s). Delete them, or move them with Settings → Export / import, before making it a node."
  fi
  warn "This server is a primary. As a node it has no panel of its own anymore; its users and settings are left unused."
  printf '\n'
  if can_ask; then
    confirm "Make it a node of $MAIN_URL?" n || die "Cancelled. Nothing was changed."
  elif ! $ASSUME_YES; then
    die "Pass --yes to turn this primary into a node."
  fi
fi

# ── Pre-flight checks ─────────────────────────────────────────────────────────
if [[ "$ACTION" == "update" ]]; then
  printf '  %bUpdating Cloudbase (%s)%b\n' "$BOLD" "$ROLE" "$RESET"
else
  printf '  %bInstalling Cloudbase as %s%b\n' "$BOLD" "$( [[ $ROLE == node ]] && echo "a node" || echo "the primary")" "$RESET"
fi
note "$OS_NAME · $ARCH"
note "Code in $INSTALL_DIR, runs as '$RUN_USER', data in $DATA_DIR"
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

if [[ "$ACTION" == "install" ]] && ! $DISCONNECT_NODE && can_ask; then
  confirm "Continue?" y || die "Cancelled."
fi

# ── Install steps ─────────────────────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive

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
  [[ -d "$DATA_DIR" ]] && chown -R "$RUN_USER": "$DATA_DIR"
  as_user mkdir -p "$DATA_DIR/apps" "$DATA_DIR/logs" "$DATA_DIR/certs"

  # Data from the old name of the project
  if [[ -d "$RUN_HOME/.pdmanager" && ! -f "$DATA_DIR/cloudbase.db" ]]; then
    for f in pdmanager.db:cloudbase.db credentials:credentials secret_key:secret_key; do
      [[ -f "$RUN_HOME/.pdmanager/${f%%:*}" ]] && as_user cp "$RUN_HOME/.pdmanager/${f%%:*}" "$DATA_DIR/${f##*:}"
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
  if [[ ! -f /etc/nginx/sites-available/cloudbase-default ]]; then
    cat > /etc/nginx/sites-available/cloudbase-default <<'NGINX_EOF'
# Cloudbase default catch-all — rejects requests for unknown hostnames.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    return 444;
}
NGINX_EOF
  fi
  ln -sf /etc/nginx/sites-available/cloudbase-default /etc/nginx/sites-enabled/cloudbase-default
  if nginx -t; then
    systemctl reload nginx
  else
    add_warning "nginx's configuration test fails — check it with: sudo nginx -t"
  fi
}

step_admin() {
  [[ -f "$DATA_DIR/credentials" ]] && return 0
  local pass
  pass="$(python3 -c "import secrets, string; a = string.ascii_letters + string.digits; print(''.join(secrets.choice(a) for _ in range(20)))")"
  cd "$INSTALL_DIR/backend"
  as_user env CB_PASS="$pass" venv/bin/python -c "import os, auth; auth.save_hashed_password(auth.hash_password(os.environ['CB_PASS']))"
  printf '%s' "$pass" | as_user tee "$DATA_DIR/.first-run-password" > /dev/null
  printf '%s' "$pass" > "$STATE_DIR/password"
}

step_register() {
  cd "$INSTALL_DIR/backend"
  rm -f "$DATA_DIR/mode"  # left by older versions
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

  cat > "$SERVICE_FILE" <<EOF
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
  if $DISCONNECT_NODE; then
    rm -f "$AGENT_STATE" "$DATA_DIR/mode"
  fi

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"

  # Each start syncs the Python packages first, so give it a moment.
  local port
  port="$(awk '/^server:/{s=1;next} /^[^[:space:]#]/{s=0} s && $1=="port:"{print $2; exit}' "$DATA_DIR/config.yaml" 2>/dev/null || true)"
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

add_step "Installing system packages" step_packages
add_step "Installing Docker" step_docker
[[ "$ROLE" == "primary" ]] && add_step "Installing nginx and certbot" step_web
# A clone the installer was started from is used as it is, except when updating.
if ! $IN_PLACE || { [[ "$ACTION" == "update" ]] && [[ -d "$INSTALL_DIR/.git" ]]; }; then
  add_step "Getting Cloudbase ($BRANCH)" step_code
fi
add_step "Setting up Python environment" step_python
if [[ "$ROLE" == "primary" ]]; then
  add_step "Configuring nginx" step_nginx_config
  add_step "Creating administrator account" step_admin
else
  add_step "Registering with the primary" step_register
fi
add_step "Starting Cloudbase" step_service
run_steps

# ── Summary ───────────────────────────────────────────────────────────────────
PORT="$(cat "$STATE_DIR/port" 2>/dev/null || echo 7823)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP="${IP:-<server-ip>}"

printf '\n'
print_warnings

if [[ "$ACTION" == "update" ]]; then
  version="$(as_user git -C "$INSTALL_DIR" log -1 --format='%h · %cd' --date=short 2>/dev/null || true)"
  printf '  %bCloudbase is updated%b%s\n' "$BOLD" "$RESET" "${version:+ — $version}"
  [[ "$ROLE" == "node" ]] && note "Update the primary and the other nodes as well, so they run the same version"
elif [[ "$ROLE" == "primary" ]]; then
  printf '  %bCloudbase is running.%b\n\n' "$BOLD" "$RESET"
  printf '    Panel      %bhttp://%s:%s%b\n' "$BOLD" "$IP" "$PORT" "$RESET"
  if [[ -f "$STATE_DIR/password" ]]; then
    printf '    Username   admin\n'
    printf '    Password   %b%s%b\n' "$GREEN" "$(cat "$STATE_DIR/password")" "$RESET"
    printf '               %bStore it now — it isn'\''t shown again.%b\n' "$DIM" "$RESET"
  fi
  if $DISCONNECT_NODE; then
    printf '\n'
    note "Remove this server under Nodes on ${OLD_PRIMARY:-its old primary}; its instances there stay offline"
  fi
  printf '\n  Next steps\n'
  note "Make ports 80 and 443 reachable from the internet"
  note "Connect a domain to the panel under Settings → Domain & SSL for HTTPS"
  note "Close port $PORT once the panel has a domain"
  note "The Guides page in the panel covers the rest"
else
  printf '  %bThis server is a Cloudbase node.%b\n\n' "$BOLD" "$RESET"
  [[ -n "$MAIN_URL" ]] && note "It appears under Nodes on $MAIN_URL within a few seconds"
  note "Add instances to it from the panel; this server has no panel of its own"
  note "Check the connection with: cloudbase node-status"
  note "To make it a primary later: cloudbase disconnect"
fi
printf '\n'
note "Log: $LOG_FILE"
if [[ -f "$STATE_DIR/reboot" ]]; then
  printf '\n'
  warn "Reboot once to enable Docker's memory statistics:  sudo reboot"
fi
printf '\n'
