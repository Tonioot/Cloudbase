#!/usr/bin/env bash
set -euo pipefail

BOLD='\033[1m'
BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
RESET='\033[0m'

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$INSTALL_DIR/backend"
VENV_PATH="$BACKEND_DIR/venv"
# server.port from the user config (falls back to the repo default)
PORT="$(awk '/^server:/{s=1;next} /^[^[:space:]#]/{s=0} s && $1=="port:"{print $2; exit}' "$HOME/.cloudbase/config.yaml" 2>/dev/null || true)"
[[ "$PORT" =~ ^[0-9]+$ ]] || PORT=7823
CREDS="$HOME/.cloudbase/credentials"
APP_NAME="Cloudbase"
SERVICE_NAME="cloudbase"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CERTS_DIR="$HOME/.cloudbase/certs"
LOG_DIR="$HOME/.cloudbase/logs"
CLI_LOG_FILE="$LOG_DIR/cloudbase-cli.log"
AGENT_LOG_FILE="$LOG_DIR/node-agent.log"
COMMAND="${1:-start}"

mkdir -p "$CERTS_DIR" "$LOG_DIR"

if [[ $# -gt 0 ]]; then
    shift
fi

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

log_line() {
    local level="$1"
    local color="$2"
    local message="$3"
    printf '[%s] [%s] %s\n' "$(timestamp)" "$level" "$message" >> "$CLI_LOG_FILE"
    printf '%b[%s] [%s]%b %s\n' "$color" "$(timestamp)" "$level" "$RESET" "$message"
}

info()    { log_line "INFO" "$BLUE" "$1"; }
success() { log_line "OK"   "$GREEN" "$1"; }
warn()    { log_line "WARN" "$YELLOW" "$1"; }
err()     { log_line "ERR"  "$RED" "$1"; }

banner() {
    cat <<'EOF'
_________ .__                   .______.                         
\_   ___ \|  |   ____  __ __  __| _/\_ |__ _____    ______ ____  
/    \  \/|  |  /  _ \|  |  \/ __ |  | __ \\__  \  /  ___// __ \ 
\     \___|  |_(  <_> )  |  / /_/ |  | \_\ \/ __ \_\___ \\  ___/ 
 \______  /____/\____/|____/\____ |  |___  (____  /____  >\___  >
        \/                       \/      \/     \/     \/     \/ 
EOF
}

usage() {
    local _ip
    _ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    cat <<EOF
Usage: cloudbase <command> [options]

Cloudbase is managed from the web UI — apps, databases, domains, HTTPS, users:
  http://localhost:${PORT}${_ip:+
  http://${_ip}:${PORT}}
The CLI runs the server and handles updates, backups and recovery.

Service:
  start | stop | restart       Start, stop or restart Cloudbase
  status                       Service, version, apps and nodes at a glance
  logs [options]               Cloudbase's own log
                                 -f              keep following
                                 -n <lines>      lines to show (default 100)
                                 --app <name>    everything Cloudbase did for one app
                                 --errors        only warnings and errors
  enable | disable             Start Cloudbase on boot (systemd)
  doctor                       Check the installation and explain what's wrong

Install:
  update [--branch <name>]     Update to the latest version
  version                      Show the installed version
  uninstall [--keep-data]      Remove Cloudbase from this server

Recovery:
  password [user]              Set a new password (default user: admin)
  backup [file]                Back up Cloudbase's database, keys and settings
  restore <file> [--yes]       Restore a backup made with 'cloudbase backup'
  nginx permissions [user]     Let Cloudbase manage nginx and HTTPS certificates

Nodes:
  connect                      Make this server a node of a primary
                                 --main-url <url>  --invite-code <code>  [--node-name <name>]
  disconnect                   Leave the primary and make this server a primary itself
  node-status                  Show which primary this node is connected to
EOF
}

service_installed() {
    [[ -f "$SERVICE_FILE" ]]
}

require_systemctl() {
    if ! command -v systemctl >/dev/null 2>&1; then
        err "systemctl is not available on this machine"
        exit 1
    fi
}

require_nginx() {
    if ! command -v nginx >/dev/null 2>&1; then
        err "nginx is not installed"
        exit 1
    fi
}

service_run_user() {
    local run_user=""
    if service_installed && command -v systemctl >/dev/null 2>&1; then
        run_user="$(systemctl show -p User --value "$SERVICE_NAME" 2>/dev/null || true)"
    fi
    if [[ -n "$run_user" ]]; then
        printf '%s\n' "$run_user"
        return 0
    fi
    printf '%s\n' "${SUDO_USER:-$(id -un)}"
}

detect_pkg_mgr() {
    if command -v apt-get >/dev/null 2>&1; then
        echo "apt"
    elif command -v dnf >/dev/null 2>&1; then
        echo "dnf"
    elif command -v yum >/dev/null 2>&1; then
        echo "yum"
    elif command -v pacman >/dev/null 2>&1; then
        echo "pacman"
    elif command -v zypper >/dev/null 2>&1; then
        echo "zypper"
    else
        echo ""
    fi
}

# certbot gives apps free HTTPS certificates from Let's Encrypt
ensure_certbot() {
    command -v certbot >/dev/null 2>&1 && return 0
    info "Installing certbot (free HTTPS certificates)"
    case "$(detect_pkg_mgr)" in
        apt)    sudo apt-get install -y certbot ;;
        dnf)    sudo dnf install -y certbot ;;
        yum)    sudo yum install -y certbot ;;
        pacman) sudo pacman -S --noconfirm certbot ;;
        zypper) sudo zypper install -y python3-certbot ;;
        *)      warn "Install certbot manually to enable automatic HTTPS"; return 0 ;;
    esac
}

install_missing_runtime_deps() {
    local pkg_mgr="$1"
    case "$pkg_mgr" in
        apt)
            sudo apt-get update
            sudo apt-get install -y lsof python3-venv python3-pip
            ;;
        dnf)
            sudo dnf install -y lsof python3 python3-pip
            ;;
        yum)
            sudo yum install -y lsof python3 python3-pip
            ;;
        pacman)
            sudo pacman -S --noconfirm lsof python python-pip
            ;;
        zypper)
            sudo zypper install -y lsof python3 python3-pip
            ;;
        *)
            err "Could not detect a supported package manager. Install python3, python3-venv, python3-pip and lsof manually."
            exit 1
            ;;
    esac
}

port_owner_pid() {
    local pid=""
    if command -v lsof >/dev/null 2>&1; then
        pid=$(lsof -ti tcp:"$PORT" 2>/dev/null | head -n 1 || true)
    elif command -v ss >/dev/null 2>&1; then
        pid=$(ss -lptn "sport = :$PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | head -n 1)
    fi
    echo "$pid"
}

write_service_file() {
    require_systemctl
    local run_user="${SUDO_USER:-$(id -un)}"
    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=${APP_NAME}
After=network.target docker.service

[Service]
Type=simple
User=${run_user}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/bin/bash ${INSTALL_DIR}/start.sh run
Restart=on-failure
RestartSec=5
KillMode=process
TimeoutStopSec=15
Delegate=yes

[Install]
WantedBy=multi-user.target
EOF
}

stop_foreground_instance() {
    local old_pid
    old_pid="$(port_owner_pid)"
    if [[ -n "$old_pid" ]]; then
        info "Stopping process on port $PORT (pid $old_pid)"
        kill -9 "$old_pid" 2>/dev/null || sudo kill -9 "$old_pid" || true
        success "Cloudbase API process stopped"
    else
        warn "No local Cloudbase process is listening on port $PORT"
    fi

    # Also kill any lingering node_agent.py processes (started alongside the API in node mode)
    local agent_pids
    agent_pids="$(pgrep -f "node_agent\.py" 2>/dev/null || true)"
    if [[ -n "$agent_pids" ]]; then
        info "Stopping node agent process(es): $agent_pids"
        echo "$agent_pids" | xargs kill -9 2>/dev/null || true
        success "Node agent stopped"
    fi
}

show_first_run_password() {
    local pw_file="$HOME/.cloudbase/.first-run-password"
    [[ -f "$pw_file" ]] || return 0
    local pass
    pass=$(cat "$pw_file")
    rm -f "$pw_file"
    printf '\n'
    printf '%b%s%b\n' "$YELLOW" "================================================================" "$RESET"
    printf '%b  FIRST RUN — Administrator password%b\n' "$BOLD" "$RESET"
    printf '%b  Username : admin%b\n' "$BOLD" "$RESET"
    printf '%b  Password : %b%s%b\n' "$BOLD" "$GREEN" "$pass" "$RESET"
    printf '%b  Login at : http://localhost:%s%b\n' "$BOLD" "$PORT" "$RESET"
    printf '%b  Change it via Settings in the UI after login.%b\n' "$BOLD" "$RESET"
    printf '%b%s%b\n' "$YELLOW" "================================================================" "$RESET"
    printf '\n'
}

SERVER_LOG_FILE="$LOG_DIR/server.log"
DB_FILE="$HOME/.cloudbase/cloudbase.db"

# Colour log levels when writing to a terminal.
colorize_log() {
    if [[ -t 1 ]]; then
        awk -v r="$RED" -v y="$YELLOW" -v d='\033[2m' -v z="$RESET" '
            /\[(ERROR|CRITICAL)\]/ { print r $0 z; fflush(); next }
            /\[WARNING\]/          { print y $0 z; fflush(); next }
            /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] / { print d substr($0, 1, 23) z substr($0, 24); fflush(); next }
            { print; fflush() }'
    else
        cat
    fi
}

# App id for an app name (case-insensitive; a unique partial match also works).
app_id_by_name() {
    python3 - "$DB_FILE" "$1" <<'PYEOF'
import sqlite3, sys
db, name = sys.argv[1], sys.argv[2]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = con.execute("SELECT id, name FROM applications ORDER BY name").fetchall()
hit = [r for r in rows if r[1].lower() == name.lower()] or [r for r in rows if name.lower() in r[1].lower()]
if len(hit) != 1:
    names = ", ".join(r[1] for r in (hit or rows)) or "(no apps)"
    print(("Several apps match: " if hit else "No app called that. Apps: ") + names, file=sys.stderr)
    sys.exit(1)
print(hit[0][0])
PYEOF
}

cmd_logs() {
    local follow=0 lines=100 app="" errors=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -f|--follow) follow=1 ;;
            -n|--lines)  lines="${2:-100}"; shift ;;
            --app)       app="${2:-}"; shift ;;
            --errors)    errors=1 ;;
            *) err "Unknown option for logs: $1"; exit 1 ;;
        esac
        shift
    done

    local file="$SERVER_LOG_FILE"
    if [[ -n "$app" ]]; then
        [[ -f "$DB_FILE" ]] || { err "No Cloudbase database found."; exit 1; }
        local app_id
        app_id="$(app_id_by_name "$app")" || exit 1
        file="$LOG_DIR/apps/$app_id.log"
        if [[ ! -f "$file" ]]; then
            warn "Nothing logged for this app yet."
            [[ $follow -eq 1 ]] || return 0
        fi
    elif [[ ! -s "$file" ]]; then
        # Installs before v2 logged everything to the node agent log.
        if [[ -s "$AGENT_LOG_FILE" ]]; then
            file="$AGENT_LOG_FILE"
        elif service_installed && command -v systemctl >/dev/null 2>&1; then
            local jflags=(-u "$SERVICE_NAME" -n "$lines" --no-pager)
            [[ $follow -eq 1 ]] && jflags+=(-f)
            sudo journalctl "${jflags[@]}"
            return
        else
            warn "No Cloudbase logs found yet"
            return
        fi
    fi

    local filter=(cat)
    [[ $errors -eq 1 ]] && filter=(grep --line-buffered -E '\[(WARNING|ERROR|CRITICAL)\]')
    if [[ $follow -eq 1 ]]; then
        tail -n "$lines" -F "$file" 2>/dev/null | "${filter[@]}" | colorize_log
    else
        tail -n "$lines" "$file" | "${filter[@]}" | colorize_log || true
    fi
}

nginx_permissions() {
    require_nginx
    local target_user="${1:-$(service_run_user)}"
    ensure_certbot
    info "Configuring Cloudbase nginx permissions for user '$target_user'"
    bash "$INSTALL_DIR/scripts/setup-nginx-permissions.sh" "$target_user"
    success "Cloudbase nginx permissions configured"
}

handle_nginx_command() {
    local subcommand="${1:-}"
    shift || true
    case "$subcommand" in
        permissions|perms|fix-perms)
            nginx_permissions "$@"
            ;;
        *)
            # The panel domain, app domains and HTTPS are managed by Cloudbase itself now.
            warn "Domains and HTTPS are set up in the web UI: Settings → Domain & SSL."
            info "The only nginx command left is: cloudbase nginx permissions"
            exit 1
            ;;
    esac
}

handle_cert_command() {
    warn "Certificates are handled in the web UI (Settings → Domain & SSL): Let's Encrypt is requested and renewed automatically, and you can upload your own there."
    exit 1
}

# ── Status ──────────────────────────────────────────────────────────────────
cmd_version() {
    local version sha branch
    version="$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo dev)"
    sha="$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || true)"
    branch="$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    printf 'Cloudbase v%s%s\n' "$version" "${sha:+ ($branch · $sha)}"
}

service_state() {
    if service_installed && command -v systemctl >/dev/null 2>&1; then
        systemctl is-active "$SERVICE_NAME" 2>/dev/null || true
    elif [[ -n "$(port_owner_pid)" ]]; then
        echo "active"
    else
        echo "inactive"
    fi
}

cmd_status() {
    local state
    state="$(service_state)"
    printf '%b%s%b\n' "$BOLD" "$(cmd_version)" "$RESET"
    if [[ "$state" == "active" ]]; then
        printf '  Service   %brunning%b on port %s\n' "$GREEN" "$RESET" "$PORT"
    else
        printf '  Service   %b%s%b — start it with: cloudbase start\n' "$RED" "$state" "$RESET"
    fi
    if is_node; then
        printf '  Role      node (see: cloudbase node-status)\n'
        return
    fi
    printf '  Role      primary\n\n'
    if [[ ! -f "$DB_FILE" ]]; then
        warn "No Cloudbase database found yet."
        return
    fi
    python3 - "$DB_FILE" <<'PYEOF'
import sqlite3, sys, datetime

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
tty = sys.stdout.isatty()
C = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "d": "\033[2m", "x": "\033[0m"} if tty else {k: "" for k in "gryxd"}

def table(title, cols, rows, status_col):
    print(f"{C['d']}{title}{C['x']}")
    if not rows:
        print("  (none)\n")
        return
    widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        cells = []
        for i, (v, w) in enumerate(zip(r, widths)):
            pad = v.ljust(w)
            if i == status_col:
                col = C["g"] if v in ("running", "online") else C["r"] if v in ("error", "offline", "stopped") else C["y"]
                pad = col + pad + C["x"]
            cells.append(pad)
        print("  " + "  ".join(cells))
    print()

def ago(ts):
    if not ts:
        return "—"
    try:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        secs = int((now - datetime.datetime.fromisoformat(str(ts))).total_seconds())
    except ValueError:
        return "—"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit} ago"
    return "just now"

apps = con.execute("""
    SELECT a.name, a.status, a.domain, a.nginx_enabled, a.autoscale_enabled,
           a.autoscale_min_replicas AS amin, a.autoscale_max_replicas AS amax,
           (SELECT COUNT(*) FROM application_replicas r WHERE r.app_id = a.id AND r.status = 'running') AS up,
           (SELECT COUNT(*) FROM application_replicas r WHERE r.app_id = a.id) AS total
    FROM applications a ORDER BY a.name
""").fetchall()
table("APPS", ["NAME", "STATUS", "INSTANCES", "AUTOSCALE", "DOMAIN"], [
    (a["name"], a["status"] or "unknown", f"{a['up']}/{a['total']}",
     f"{a['amin'] or 1}–{a['amax'] or 4}" if a["autoscale_enabled"] else "off",
     a["domain"] if a["nginx_enabled"] and a["domain"] else "—")
    for a in apps
], 1)

nodes = con.execute("""
    SELECT n.name, n.status, n.is_local, n.last_seen, n.node_cpu_percent AS cpu,
           (SELECT COUNT(*) FROM application_replicas r WHERE r.node_id = n.id AND r.status = 'running') AS up
    FROM nodes n ORDER BY n.is_local DESC, n.name
""").fetchall()
table("NODES", ["NAME", "STATUS", "INSTANCES", "CPU", "LAST SEEN"], [
    (n["name"], n["status"] or "unknown", str(n["up"]),
     f"{n['cpu']:.0f}%" if n["cpu"] is not None else "—",
     "this server" if n["is_local"] else ago(n["last_seen"]))
    for n in nodes
], 1)
PYEOF
}

# ── Password ────────────────────────────────────────────────────────────────
cmd_password() {
    local username="${1:-admin}"
    info "Setting a new password for '$username'"
    local new_pass confirm
    read -r -s -p "New password: " new_pass
    printf '\n'
    if [[ ${#new_pass} -lt 8 ]]; then
        err "Password must be at least 8 characters"
        exit 1
    fi
    read -r -s -p "Confirm password: " confirm
    printf '\n'
    if [[ "$new_pass" != "$confirm" ]]; then
        err "Passwords do not match"
        exit 1
    fi
    cd "$BACKEND_DIR"
    # Password and user name go in through the environment, never into the code.
    CB_NEW_PASS="$new_pass" CB_USER="$username" "$VENV_PATH/bin/python3" - <<'PYEOF'
import asyncio, os, sys
sys.path.insert(0, '.')
import auth
from database import AsyncSessionLocal
from models import User
from sqlalchemy import select

async def main():
    username = os.environ["CB_USER"]
    hashed = auth.hash_password(os.environ["CB_NEW_PASS"])
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            names = (await session.execute(select(User.username))).scalars().all()
            print(f"No user '{username}'. Users: {', '.join(names) or '(none)'}", file=sys.stderr)
            sys.exit(1)
        user.password_hash = hashed
        await session.commit()
    if username == "admin":
        # Keep the first-run credentials file in sync (only used to seed a fresh DB)
        auth.save_hashed_password(hashed)

asyncio.run(main())
PYEOF
    success "Password updated for '$username'"
}

# ── Backup / restore ────────────────────────────────────────────────────────
# Everything needed to bring this server back, including the keys that decrypt
# app env vars and saved tokens. App sources are re-cloned and images rebuilt,
# so they're not included; managed databases have their own backups.
BACKUP_FILES=(credentials secret_key env_key agent_token github_tokens.json config.yaml)
BACKUP_DIRS=(certs db-tls)

cmd_backup() {
    local out="${1:-cloudbase-backup-$(date +%Y%m%d-%H%M%S).tar.gz}"
    local data_dir="$HOME/.cloudbase"
    [[ -f "$data_dir/cloudbase.db" ]] || { err "No Cloudbase database found in $data_dir"; exit 1; }

    CB_TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "${CB_TMP_DIR:-}"' EXIT
    local tmp_dir="$CB_TMP_DIR"
    mkdir -p "$tmp_dir/cloudbase"

    # Consistent copy even while Cloudbase is writing to it
    python3 - "$data_dir/cloudbase.db" "$tmp_dir/cloudbase/cloudbase.db" <<'PYEOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PYEOF

    local f d
    for f in "${BACKUP_FILES[@]}"; do
        [[ -f "$data_dir/$f" ]] && cp -p "$data_dir/$f" "$tmp_dir/cloudbase/"
    done
    for d in "${BACKUP_DIRS[@]}"; do
        [[ -d "$data_dir/$d" ]] && cp -rp "$data_dir/$d" "$tmp_dir/cloudbase/"
    done
    # A node's identity towards its primary
    [[ -f "$BACKEND_DIR/agent_state.json" ]] && cp -p "$BACKEND_DIR/agent_state.json" "$tmp_dir/cloudbase/"

    printf 'version=%s\ncreated=%s\nhost=%s\n' \
        "$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo dev)" "$(date -Iseconds)" "$(hostname)" \
        > "$tmp_dir/cloudbase/MANIFEST"

    (umask 077 && tar -czf "$out" -C "$tmp_dir" cloudbase)
    success "Backup saved to $out"
    warn "It contains the keys to all app secrets — keep it somewhere safe."
    info "Managed databases aren't in here; they have their own backups (Databases page)."
}

cmd_restore() {
    local src="" yes=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -y|--yes) yes=1 ;;
            *) src="$1" ;;
        esac
        shift
    done
    if [[ -z "$src" || ! -f "$src" ]]; then
        err "Usage: cloudbase restore <backup.tar.gz> [--yes]"
        exit 1
    fi

    local data_dir="$HOME/.cloudbase"
    CB_TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "${CB_TMP_DIR:-}"' EXIT
    local tmp_dir="$CB_TMP_DIR"
    tar -xzf "$src" -C "$tmp_dir"
    local src_dir="$tmp_dir/cloudbase"
    if [[ ! -f "$src_dir/cloudbase.db" ]]; then
        err "Not a Cloudbase backup: cloudbase/cloudbase.db is missing"
        exit 1
    fi

    if [[ -f "$src_dir/MANIFEST" ]]; then
        info "Backup: $(tr '\n' ' ' < "$src_dir/MANIFEST")"
    else
        warn "Backup from before v2 (no MANIFEST)."
    fi
    [[ -f "$src_dir/env_key" ]] || warn "This backup has no env_key: app environment variables will not decrypt."

    if [[ $yes -ne 1 ]]; then
        if [[ ! -t 0 ]]; then
            err "This replaces Cloudbase's current data. Re-run with --yes to confirm."
            exit 1
        fi
        local answer
        read -r -p "This replaces Cloudbase's current data on this server. Continue? [y/N] " answer
        [[ "$answer" =~ ^[Yy]$ ]] || { info "Restore cancelled"; exit 0; }
    fi

    local was_running=0
    if [[ "$(service_state)" == "active" ]]; then
        was_running=1
        info "Stopping Cloudbase"
        if service_installed && command -v systemctl >/dev/null 2>&1; then
            sudo systemctl stop "$SERVICE_NAME"
        else
            stop_foreground_instance
        fi
    fi

    # Keep what we replace, so a wrong restore can be undone by hand.
    local keep="$data_dir/pre-restore-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$keep"
    local f d
    for f in cloudbase.db "${BACKUP_FILES[@]}"; do
        [[ -f "$data_dir/$f" ]] && mv "$data_dir/$f" "$keep/"
    done
    for d in "${BACKUP_DIRS[@]}"; do
        [[ -d "$data_dir/$d" ]] && mv "$data_dir/$d" "$keep/"
    done
    [[ -f "$BACKEND_DIR/agent_state.json" ]] && mv "$BACKEND_DIR/agent_state.json" "$keep/"

    mkdir -p "$data_dir"
    cp -p "$src_dir/cloudbase.db" "$data_dir/"
    for f in "${BACKUP_FILES[@]}"; do
        [[ -f "$src_dir/$f" ]] && cp -p "$src_dir/$f" "$data_dir/" && chmod 600 "$data_dir/$f"
    done
    for d in "${BACKUP_DIRS[@]}"; do
        [[ -d "$src_dir/$d" ]] && cp -rp "$src_dir/$d" "$data_dir/"
    done
    [[ -f "$src_dir/agent_state.json" ]] && cp -p "$src_dir/agent_state.json" "$BACKEND_DIR/"
    chmod 600 "$data_dir/cloudbase.db"

    success "Restored. The previous data is kept in $keep"
    if [[ $was_running -eq 1 ]]; then
        info "Starting Cloudbase"
        if service_installed && command -v systemctl >/dev/null 2>&1; then
            sudo systemctl start "$SERVICE_NAME"
        else
            info "Start it again with: cloudbase start"
        fi
    fi
    info "Apps are rebuilt from their repositories when they next start or deploy."
}

# ── Doctor ──────────────────────────────────────────────────────────────────
cmd_doctor() {
    local problems=0
    ok()   { printf '  %b✓%b %s\n' "$GREEN" "$RESET" "$1"; }
    hint() { if [[ -n "${1:-}" ]]; then printf '      → %s\n' "$1"; fi; }
    bad()  { printf '  %b✗%b %s\n' "$RED" "$RESET" "$1"; hint "${2:-}"; problems=$((problems + 1)); }
    note() { printf '  %b!%b %s\n' "$YELLOW" "$RESET" "$1"; hint "${2:-}"; }

    printf '%b%s%b\n\n' "$BOLD" "$(cmd_version)" "$RESET"

    # Install
    if [[ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
        note "Files in $INSTALL_DIR were changed by hand" "'cloudbase update' will overwrite them"
    fi

    # Service
    if service_installed; then
        systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null && ok "Starts on boot" \
            || note "Doesn't start on boot" "cloudbase enable"
    else
        note "No systemd service installed" "cloudbase enable"
    fi
    if [[ "$(service_state)" == "active" ]]; then
        ok "Service is running"
    else
        bad "Service is not running" "cloudbase start, then cloudbase logs --errors"
    fi
    if python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$PORT/api/health', timeout=5)" 2>/dev/null; then
        ok "API answers on port $PORT"
    else
        bad "API doesn't answer on port $PORT" "cloudbase logs --errors"
    fi

    # Docker
    if ! command -v docker >/dev/null 2>&1; then
        bad "Docker is not installed" "cloudbase update installs it"
    elif docker info >/dev/null 2>&1; then
        ok "Docker is reachable"
    else
        bad "Docker isn't reachable for user $(id -un)" "sudo usermod -aG docker $(id -un), then log in again"
    fi

    if ! is_node; then
        # nginx + HTTPS
        # nginx lives in /usr/sbin, which isn't on a normal user's PATH
        local nginx_bin
        nginx_bin="$(PATH="$PATH:/usr/sbin:/sbin" command -v nginx || true)"
        if [[ -z "$nginx_bin" ]]; then
            bad "nginx is not installed" "cloudbase update installs it"
        elif sudo -n "$nginx_bin" -t >/dev/null 2>&1; then
            ok "nginx config is valid and Cloudbase may manage it"
        elif sudo -n true 2>/dev/null; then
            bad "nginx config has errors" "sudo nginx -t"
        else
            bad "Cloudbase may not manage nginx" "cloudbase nginx permissions"
        fi
        command -v certbot >/dev/null 2>&1 && ok "certbot is installed (automatic HTTPS)" \
            || bad "certbot is missing — no automatic HTTPS" "cloudbase update, then cloudbase nginx permissions"
        [[ -x /usr/local/lib/cloudbase/issue-cert ]] || bad "Certificate helper is missing" "cloudbase nginx permissions"
    else
        local main_url
        main_url="$(python3 -c "import json; print(json.load(open('$BACKEND_DIR/agent_state.json')).get('main_url',''))" 2>/dev/null || true)"
        if [[ -n "$main_url" ]] && python3 -c "import urllib.request; urllib.request.urlopen('${main_url%/}/api/health', timeout=8)" 2>/dev/null; then
            ok "Primary $main_url is reachable"
        else
            bad "Primary ${main_url:-?} is not reachable" "check the network / firewall, then cloudbase node-status"
        fi
    fi

    # Disk
    local use
    use="$(df -P "$HOME" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')"
    if [[ -n "$use" ]]; then
        if (( use >= 90 )); then
            bad "Disk is ${use}% full" "docker system prune removes unused images"
        elif (( use >= 80 )); then
            note "Disk is ${use}% full"
        else
            ok "Disk ${use}% used"
        fi
    fi

    # Recent errors
    if [[ -s "$SERVER_LOG_FILE" ]]; then
        local today yesterday n
        today="$(date +%Y-%m-%d)"; yesterday="$(date -d yesterday +%Y-%m-%d 2>/dev/null || echo "$today")"
        n="$(grep -cE "^($today|$yesterday) .*\[(ERROR|CRITICAL)\]" "$SERVER_LOG_FILE" 2>/dev/null || true)"
        if [[ "${n:-0}" -gt 0 ]]; then
            note "$n error(s) logged since yesterday" "cloudbase logs --errors"
        else
            ok "No errors logged since yesterday"
        fi
    fi

    printf '\n'
    if [[ $problems -eq 0 ]]; then
        success "No problems found"
    else
        warn "$problems problem(s) found"
        exit 1
    fi
}

# update, connect, disconnect and uninstall are done by the installer, so they
# look and behave the same as installing.
run_installer() {
    exec sudo bash "$INSTALL_DIR/install.sh" "$@"
}

is_node() {
    [[ -f "$BACKEND_DIR/agent_state.json" ]]
}


run_runtime() {
    info "Starting Cloudbase runtime"

    if ! command -v python3 >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1 || ! command -v lsof >/dev/null 2>&1; then
        warn "Missing runtime dependencies detected. Installing them now."
        install_missing_runtime_deps "$(detect_pkg_mgr)"
    fi

    mkdir -p "$(dirname "$CREDS")"

    local old_pid
    old_pid="$(port_owner_pid)"
    if [[ -n "$old_pid" ]]; then
        warn "Cleaning up old process on port $PORT (pid $old_pid)"
        kill -9 "$old_pid" 2>/dev/null || sudo kill -9 "$old_pid" || true
    fi

    cd "$BACKEND_DIR"

    if [[ ! -d "$VENV_PATH" || ! -f "$VENV_PATH/bin/pip" ]]; then
        warn "Virtual environment missing or broken. Rebuilding it."
        rm -rf "$VENV_PATH"
        python3 -m venv venv
        success "Virtual environment rebuilt"
    fi

    info "Syncing Python dependencies"
    "$VENV_PATH/bin/pip" install --upgrade pip
    "$VENV_PATH/bin/pip" install -r requirements.txt

    if [[ ! -f "$CREDS" ]]; then
        info "First run detected. Generating administrator password"
        local pass
        pass=$("$VENV_PATH/bin/python3" -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits + '!@#%^&*') for _ in range(20)))")

        "$VENV_PATH/bin/python3" - <<PYEOF
import sys
sys.path.insert(0, '.')
import auth
auth.save_hashed_password(auth.hash_password("$pass"))
PYEOF

        printf '%s' "$pass" > "$HOME/.cloudbase/.first-run-password"
    fi

    success "Launching Cloudbase on port $PORT"
    exec "$VENV_PATH/bin/uvicorn" main:app --host 0.0.0.0 --port "$PORT" --timeout-graceful-shutdown 8
}


run_node_runtime() {
    info "Running as a node (agent inside the API, listening on 127.0.0.1 only)"

    if ! command -v python3 >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1 || ! command -v lsof >/dev/null 2>&1; then
        warn "Missing runtime dependencies detected. Installing them now."
        install_missing_runtime_deps "$(detect_pkg_mgr)"
    fi

    mkdir -p "$(dirname "$CREDS")"
    cd "$BACKEND_DIR"

    if [[ ! -d "$VENV_PATH" || ! -f "$VENV_PATH/bin/pip" ]]; then
        warn "Virtual environment missing or broken. Rebuilding it."
        rm -rf "$VENV_PATH"
        python3 -m venv venv
        success "Virtual environment rebuilt"
    fi

    info "Syncing Python dependencies"
    "$VENV_PATH/bin/pip" install --upgrade pip
    "$VENV_PATH/bin/pip" install -r requirements.txt

    local old_pid
    old_pid="$(port_owner_pid)"
    if [[ -n "$old_pid" ]]; then
        warn "Cleaning up old process on port $PORT (pid $old_pid)"
        kill -9 "$old_pid" 2>/dev/null || sudo kill -9 "$old_pid" || true
    fi

    success "Launching Cloudbase node on port $PORT"
    exec "$VENV_PATH/bin/uvicorn" main:app --host 127.0.0.1 --port "$PORT" --timeout-graceful-shutdown 8
}

case "$COMMAND" in start|run|help|-h|--help)
    printf '\n%b' "$BOLD"
    banner
    printf '%b\n' "$RESET"
    ;;
esac

case "$COMMAND" in
    help|-h|--help)
        usage
        exit 0
        ;;
    enable)
        write_service_file
        sudo systemctl daemon-reload
        sudo systemctl enable --now "$SERVICE_NAME"
        success "Cloudbase now starts automatically on boot"
        ;;
    disable)
        require_systemctl
        if service_installed; then
            sudo systemctl disable --now "$SERVICE_NAME"
            success "Cloudbase boot autostart disabled"
        else
            warn "No Cloudbase systemd service is installed"
        fi
        ;;
    status)
        cmd_status
        ;;
    version|--version|-v)
        cmd_version
        ;;
    doctor)
        cmd_doctor
        ;;
    stop)
        if service_installed && command -v systemctl >/dev/null 2>&1; then
            sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
            success "Cloudbase service stopped"
        fi
        stop_foreground_instance
        ;;
    restart)
        if service_installed && command -v systemctl >/dev/null 2>&1; then
            sudo systemctl restart "$SERVICE_NAME"
            success "Cloudbase service restarted"
        else
            stop_foreground_instance
            if is_node; then run_node_runtime; else run_runtime; fi
        fi
        ;;
    logs)
        cmd_logs "$@"
        ;;
    password)
        cmd_password "$@"
        ;;
    backup|export)
        cmd_backup "$@"
        ;;
    restore|import)
        cmd_restore "$@"
        ;;
    update)
        run_installer --update "$@"
        ;;
    nginx)
        handle_nginx_command "$@"
        ;;
    cert|certs)
        handle_cert_command "$@"
        ;;
    connect)
        run_installer --node "$@"
        ;;
    disconnect)
        if ! is_node; then
            warn "This server isn't connected to a primary."
            exit 0
        fi
        # The installer disconnects it and sets it up as a primary.
        run_installer --primary "$@"
        ;;
    node-status)
        state_file="$BACKEND_DIR/agent_state.json"
        if [[ ! -f "$state_file" ]]; then
            warn "This server isn't a node — it's a primary, or not connected yet"
        else
            node_id=$(python3 -c "import json; d=json.load(open('$state_file')); print(d.get('node_id','?'))" 2>/dev/null)
            node_name=$(python3 -c "import json; d=json.load(open('$state_file')); print(d.get('node_name','?'))" 2>/dev/null)
            main_url=$(python3 -c "import json; d=json.load(open('$state_file')); print(d.get('main_url','?'))" 2>/dev/null)
            info "Registered as: $node_name (id=$node_id)"
            info "Primary:       $main_url"
            # The agent runs inside the Cloudbase service, not as its own process
            if [[ -n "$(port_owner_pid)" ]]; then
                success "Cloudbase (with agent): running on port $PORT"
            else
                warn "Cloudbase (with agent): not running — start it with 'cloudbase start'"
            fi
            if [[ -f "$AGENT_LOG_FILE" ]]; then
                last_conn=$(grep -E "WebSocket connected|websocket loop error" "$AGENT_LOG_FILE" | tail -n 1 || true)
                [[ -n "$last_conn" ]] && info "Last connection event: $last_conn"
            fi
        fi
        ;;
    start)
        show_first_run_password
        if service_installed && command -v systemctl >/dev/null 2>&1; then
            sudo systemctl start "$SERVICE_NAME"
            success "Cloudbase service started"
        else
            if is_node; then run_node_runtime; else run_runtime; fi
        fi
        ;;
    run)
        if is_node; then run_node_runtime; else run_runtime; fi
        ;;
    uninstall)
        run_installer --uninstall "$@"
        ;;
    apps|nodes)
        # Folded into status
        cmd_status
        ;;
    *)
        err "Unknown command: $COMMAND"
        usage
        exit 1
        ;;
esac
