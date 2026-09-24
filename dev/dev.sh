#!/usr/bin/env bash
# Unified ServerKit development launcher.
#
# Root wrappers:
#   ./dev.sh                 Linux/WSL/macOS entry point
#   .\dev.ps1                Windows entry point, delegates to this script via WSL
#
# Usage:
#   ./dev.sh                 Start backend + frontend on stable non-default ports
#   ./dev.sh backend         Start backend only
#   ./dev.sh frontend        Start frontend only, targeting BackendPort for API calls
#   ./dev.sh tunnel          Start backend + frontend + expose backend via ngrok
#   ./dev.sh validate        Run checks
#
# Options:
#   --backend-port,  -BackendPort   Pin/prefer a backend port
#   --frontend-port, -FrontendPort  Pin/prefer a frontend port
#   --no-auto-port,  -NoAutoPort    Fail when a preferred port is busy

set -euo pipefail

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$DEV_DIR/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
BACKEND_VENV_DIR="${SERVERKIT_BACKEND_VENV:-$BACKEND_DIR/.venv-wsl}"

MODE="start"
MODE_SET=0
BACKEND_PORT="${SERVERKIT_BACKEND_PORT:-47927}"
FRONTEND_PORT="${SERVERKIT_FRONTEND_PORT:-41921}"
FRONTEND_ONLY_DEFAULT_BACKEND_PORT="$BACKEND_PORT"
AUTO_PORT=1
KILL_PORTS="${SERVERKIT_KILL_PORTS:-1}"

CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
DIM='\033[2m'
NC='\033[0m'

usage() {
    cat <<'EOF'
ServerKit development launcher

Usage:
  ./dev.sh [mode] [options]
  .\dev.ps1 [mode] [options]

Modes:
  start       Start backend + frontend (default)
  backend     Start backend only
  frontend    Start frontend only, targeting BackendPort for API calls
  tunnel      Start backend + frontend + ngrok tunnel
  validate    Run validation checks

Options:
  --backend-port PORT,  -BackendPort PORT     Pin/prefer a backend port
  --frontend-port PORT, -FrontendPort PORT    Pin/prefer a frontend port
  --no-auto-port,       -NoAutoPort           Fail if a pinned/preferred port is busy
  -h, --help                                  Show this help

Environment:
  SERVERKIT_BACKEND_PORT     Optional pinned/preferred backend port
  SERVERKIT_FRONTEND_PORT    Optional pinned/preferred frontend port
  NGROK_DOMAIN               Optional reserved ngrok domain
  NGROK_AUTHTOKEN            Optional ngrok authtoken
  SERVERKIT_KILL_PORTS=0     Disable startup cleanup of configured dev ports
  SERVERKIT_DEV_DATABASE_URL Use another database (e.g. a throwaway sqlite
                             file for a panel that starts in first-run setup)

Defaults:
  ServerKit uses stable non-default local ports: backend 47927, frontend 41921.
  If either port is busy, the launcher moves to the next free nearby port unless
  --no-auto-port is provided.
EOF
}

header() {
    echo
    echo -e "${CYAN}=== $1 ===${NC}"
    echo
}

pass() {
    echo -e "  ${GREEN}PASS${NC} $1"
}

fail() {
    echo -e "  ${RED}FAIL${NC} $1"
}

die() {
    echo -e "${RED}Error:${NC} $*" >&2
    exit 1
}

python_command_works() {
    local candidate="$1"

    [ -n "$candidate" ] || return 1
    "$candidate" -c 'import sys' >/dev/null 2>&1
}

find_python() {
    local name path

    for name in python3 python; do
        path="$(command -v "$name" 2>/dev/null || true)"
        [ -n "$path" ] || continue

        # WSL can inherit Windows PATH entries. Those shims look executable to
        # Bash but cannot run as Linux interpreters.
        case "$path" in
            /mnt/*) continue ;;
        esac

        if python_command_works "$path"; then
            echo "$path"
            return 0
        fi
    done

    for path in /usr/bin/python3 /usr/local/bin/python3; do
        if [ -x "$path" ] && python_command_works "$path"; then
            echo "$path"
            return 0
        fi
    done

    return 1
}

backend_python() {
    local path

    path="$(command -v python 2>/dev/null || true)"
    if python_command_works "$path"; then
        echo "$path"
        return 0
    fi

    require_python
    echo "$PYTHON_BIN"
}

PYTHON_BIN="$(find_python || true)"

require_python() {
    if [ -z "$PYTHON_BIN" ]; then
        die "python3 or python is required for local development."
    fi
}

validate_port() {
    local name="$1"
    local port="$2"

    if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        die "$name port must be between 1 and 65535. Got $port."
    fi
}

validate_optional_port() {
    local name="$1"
    local port="$2"

    if [ -n "$port" ]; then
        validate_port "$name" "$port"
    fi
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            start|backend|frontend|tunnel|validate)
                if [ "$MODE_SET" -eq 1 ]; then
                    die "Only one mode can be provided."
                fi
                MODE="$1"
                MODE_SET=1
                shift
                ;;
            --backend-port|-BackendPort)
                [ "$#" -ge 2 ] || die "$1 requires a port."
                BACKEND_PORT="$2"
                shift 2
                ;;
            --backend-port=*|-BackendPort=*)
                BACKEND_PORT="${1#*=}"
                shift
                ;;
            --frontend-port|-FrontendPort)
                [ "$#" -ge 2 ] || die "$1 requires a port."
                FRONTEND_PORT="$2"
                shift 2
                ;;
            --frontend-port=*|-FrontendPort=*)
                FRONTEND_PORT="${1#*=}"
                shift
                ;;
            --no-auto-port|-NoAutoPort)
                AUTO_PORT=0
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "Unknown argument: $1"
                ;;
        esac
    done

    validate_optional_port "Backend" "$BACKEND_PORT"
    validate_optional_port "Frontend" "$FRONTEND_PORT"
}

port_available() {
    local port="$1"
    require_python

    "$PYTHON_BIN" - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(("0.0.0.0", port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

is_reserved_port() {
    local candidate="$1"
    shift || true

    local reserved
    for reserved in "$@"; do
        if [ "$candidate" = "$reserved" ]; then
            return 0
        fi
    done
    return 1
}

listener_pids_for_port() {
    local port="$1"

    if command -v ss >/dev/null 2>&1; then
        ss -ltnp 2>/dev/null |
            awk -v suffix=":$port" '$4 ~ suffix "$" { print }' |
            grep -oE 'pid=[0-9]+' |
            cut -d= -f2 |
            sort -u
    elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | sort -u
    fi
}

stop_listeners_on_port() {
    local port="$1"
    local label="$2"

    [ "$KILL_PORTS" = "1" ] || return 0
    [ -n "$port" ] || return 0

    local pids=()
    local pid
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        [ "$pid" != "$$" ] || continue
        pids+=("$pid")
    done < <(listener_pids_for_port "$port")

    [ "${#pids[@]}" -gt 0 ] || return 0

    echo -e "${YELLOW}Stopping existing $label listener on port $port: ${pids[*]}${NC}"
    kill "${pids[@]}" 2>/dev/null || true
    sleep 1

    local still_running=()
    for pid in "${pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            still_running+=("$pid")
        fi
    done

    if [ "${#still_running[@]}" -gt 0 ]; then
        kill -9 "${still_running[@]}" 2>/dev/null || true
    fi
}

RESOLVED_PORT=""
RESOLVED_CHANGED=0

resolve_random_port() {
    local name="$1"
    shift
    local reserved_ports=("$@")
    require_python

    RESOLVED_PORT="$("$PYTHON_BIN" - "${reserved_ports[@]}" <<'PY'
import socket
import sys

reserved = {int(port) for port in sys.argv[1:]}

for _ in range(100):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()

    if port not in reserved:
        print(port)
        raise SystemExit(0)

raise SystemExit(1)
PY
)" || die "Could not find a random free $name port."

    if is_reserved_port "$RESOLVED_PORT" "${reserved_ports[@]}" || ! port_available "$RESOLVED_PORT"; then
        die "Could not reserve random $name port $RESOLVED_PORT."
    fi

    RESOLVED_CHANGED=0
}

resolve_dev_port() {
    local name="$1"
    local preferred="$2"
    shift 2
    local reserved_ports=("$@")

    if [ -z "$preferred" ]; then
        resolve_random_port "$name" "${reserved_ports[@]}"
        return
    fi

    if ! is_reserved_port "$preferred" "${reserved_ports[@]}" && port_available "$preferred"; then
        RESOLVED_PORT="$preferred"
        RESOLVED_CHANGED=0
        return
    fi

    if [ "$AUTO_PORT" -eq 0 ]; then
        if is_reserved_port "$preferred" "${reserved_ports[@]}"; then
            die "$name port $preferred is already reserved by another local service in this launch."
        fi
        die "$name port $preferred is already in use. Stop the other process or omit --no-auto-port."
    fi

    local upper_bound=$((preferred + 200))
    if [ "$upper_bound" -gt 65535 ]; then
        upper_bound=65535
    fi

    local candidate
    for ((candidate = preferred + 1; candidate <= upper_bound; candidate++)); do
        if is_reserved_port "$candidate" "${reserved_ports[@]}"; then
            continue
        fi
        if port_available "$candidate"; then
            RESOLVED_PORT="$candidate"
            RESOLVED_CHANGED=1
            return
        fi
    done

    die "Could not find a free $name port near $preferred."
}

trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

join_unique_csv() {
    local result=()
    local value clean existing

    for value in "$@"; do
        clean="$(trim "$value")"
        [ -n "$clean" ] || continue

        for existing in "${result[@]}"; do
            if [ "$existing" = "$clean" ]; then
                continue 2
            fi
        done
        result+=("$clean")
    done

    local IFS=,
    printf '%s' "${result[*]}"
}

BACKEND_ACTUAL_PORT=""
BACKEND_PORT_CHANGED=0
FRONTEND_ACTUAL_PORT=""
FRONTEND_PORT_CHANGED=0
BACKEND_URL=""
FRONTEND_URL=""
API_URL=""
DEV_CORS_ORIGINS=""

is_wsl() {
    [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/version 2>/dev/null
}

browser_backend_host() {
    if [ -n "${SERVERKIT_BROWSER_BACKEND_HOST:-}" ]; then
        printf '%s' "$SERVERKIT_BROWSER_BACKEND_HOST"
        return
    fi

    if is_wsl; then
        hostname -I 2>/dev/null | awk '{print $1}'
        return
    fi

    printf 'localhost'
}

# Interface Vite binds to. Under WSL the browser is on Windows, a different
# host: binding WSL's own loopback leaves the app reachable only if WSL2's
# localhost forwarding happens to be up, and when it silently stops the app is
# unreachable from Windows while the backend (already bound to 0.0.0.0) keeps
# answering — the confusing half-broken state this guards against. Bind all
# interfaces so the WSL IP always works. WSL networking is NAT'd behind the
# Windows host, so this does not put the dev server on the LAN.
frontend_bind_host() {
    if [ -n "${SERVERKIT_FRONTEND_BIND:-}" ]; then
        printf '%s' "$SERVERKIT_FRONTEND_BIND"
        return
    fi
    if is_wsl; then
        printf '0.0.0.0'
        return
    fi
    printf '127.0.0.1'
}

# Host to actually type into the browser for the frontend.
browser_frontend_host() {
    if is_wsl; then
        hostname -I 2>/dev/null | awk '{print $1}'
        return
    fi
    printf 'localhost'
}

new_dev_settings() {
    local backend_may_already_be_running="${1:-0}"
    local include_ngrok_origins="${2:-0}"

    if [ "$backend_may_already_be_running" -eq 1 ]; then
        BACKEND_ACTUAL_PORT="${BACKEND_PORT:-$FRONTEND_ONLY_DEFAULT_BACKEND_PORT}"
        BACKEND_PORT_CHANGED=0
    else
        resolve_dev_port "Backend" "$BACKEND_PORT"
        BACKEND_ACTUAL_PORT="$RESOLVED_PORT"
        BACKEND_PORT_CHANGED="$RESOLVED_CHANGED"
    fi

    resolve_dev_port "Frontend" "$FRONTEND_PORT" "$BACKEND_ACTUAL_PORT"
    FRONTEND_ACTUAL_PORT="$RESOLVED_PORT"
    FRONTEND_PORT_CHANGED="$RESOLVED_CHANGED"

    local backend_host
    backend_host="$(browser_backend_host)"
    [ -n "$backend_host" ] || backend_host="localhost"

    local frontend_host
    frontend_host="$(browser_frontend_host)"
    [ -n "$frontend_host" ] || frontend_host="localhost"

    BACKEND_URL="http://$backend_host:$BACKEND_ACTUAL_PORT"
    FRONTEND_URL="http://$frontend_host:$FRONTEND_ACTUAL_PORT"
    API_URL="$BACKEND_URL/api/v1"

    local origins=()
    if [ -n "${CORS_ORIGINS:-}" ]; then
        IFS=',' read -r -a origins <<< "$CORS_ORIGINS"
    fi

    origins+=(
        "$FRONTEND_URL"
        # Both loopback spellings stay allowed: localhost still works whenever
        # WSL2's forwarding is up, and is the only host off WSL.
        "http://localhost:$FRONTEND_ACTUAL_PORT"
        "http://127.0.0.1:$FRONTEND_ACTUAL_PORT"
        "$BACKEND_URL"
        "http://localhost:$BACKEND_ACTUAL_PORT"
        "http://127.0.0.1:$BACKEND_ACTUAL_PORT"
    )

    if [ "$include_ngrok_origins" -eq 1 ]; then
        origins+=(
            "https://*.ngrok-free.app"
            "https://*.ngrok.app"
            "https://*.ngrok.io"
        )
        if [ -n "${NGROK_DOMAIN:-}" ]; then
            origins+=("https://$NGROK_DOMAIN")
        fi
    fi

    DEV_CORS_ORIGINS="$(join_unique_csv "${origins[@]}")"
}

export_dev_env() {
    mkdir -p "$BACKEND_DIR/instance"

    export FLASK_ENV=development
    export PORT="$BACKEND_ACTUAL_PORT"
    export DATABASE_URL="${SERVERKIT_DEV_DATABASE_URL:-sqlite:///$BACKEND_DIR/instance/serverkit.db}"
    export CORS_ORIGINS="$DEV_CORS_ORIGINS"
    export VITE_API_URL="$API_URL"
}

write_frontend_env() {
    local env_file="$FRONTEND_DIR/.env.development.local"
    local tmp_file="$env_file.tmp.$$"

    mkdir -p "$FRONTEND_DIR"

    if [ -f "$env_file" ]; then
        awk '
            $0 == "# BEGIN SERVERKIT DEV" { skip = 1; next }
            $0 == "# END SERVERKIT DEV" { skip = 0; next }
            !skip { print }
        ' "$env_file" > "$tmp_file"
    else
        : > "$tmp_file"
    fi

    if [ -s "$tmp_file" ] && [ "$(tail -c 1 "$tmp_file" 2>/dev/null || true)" != "" ]; then
        printf '\n' >> "$tmp_file"
    fi

    cat >> "$tmp_file" <<EOF
# BEGIN SERVERKIT DEV
VITE_API_URL=$API_URL
# END SERVERKIT DEV
EOF

    mv "$tmp_file" "$env_file"
}

print_dev_summary() {
    local title="${1:-ServerKit Dev Server}"

    echo
    echo -e "${CYAN}${title}${NC}"
    # Under WSL the FRONTEND_URL host is the WSL interface IP (reachable from
    # Windows even when localhost forwarding dies), but localhost is the one
    # people expect — print both and let either work.
    if is_wsl && [[ "$FRONTEND_URL" != *"://localhost"* && "$FRONTEND_URL" != *"://127."* ]]; then
        echo "  Open app: http://localhost:$FRONTEND_ACTUAL_PORT"
        echo "  (fallback: $FRONTEND_URL)"
    else
        echo "  Open app: $FRONTEND_URL"
    fi
    echo "  Backend:  $BACKEND_URL"
    echo "  Health:   $API_URL/system/health"
    echo "  API env:  VITE_API_URL=$API_URL"

    if [ "$BACKEND_PORT_CHANGED" -eq 1 ]; then
        echo -e "  ${YELLOW}Note:${NC} backend port $BACKEND_PORT was busy; using $BACKEND_ACTUAL_PORT."
    fi
    if [ "$FRONTEND_PORT_CHANGED" -eq 1 ]; then
        echo -e "  ${YELLOW}Note:${NC} frontend port $FRONTEND_PORT was busy; using $FRONTEND_ACTUAL_PORT."
    fi
    echo
}

activate_backend_venv() {
    local venv_dir

    for venv_dir in "$BACKEND_VENV_DIR" "$BACKEND_DIR/venv" "$BACKEND_DIR/.venv"; do
        if [ -f "$venv_dir/bin/activate" ] && python_command_works "$venv_dir/bin/python"; then
            # shellcheck disable=SC1091
            source "$venv_dir/bin/activate"
            return 0
        fi
    done

    require_python

    echo -e "${YELLOW}Backend WSL virtualenv is missing or not runnable; creating $BACKEND_VENV_DIR${NC}"
    if ! "$PYTHON_BIN" -m venv "$BACKEND_VENV_DIR"; then
        die "Could not create backend venv. In WSL, install venv support with: sudo apt install python3-venv"
    fi

    if [ -f "$BACKEND_VENV_DIR/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$BACKEND_VENV_DIR/bin/activate"
    else
        die "Backend venv was created but activate script was not found."
    fi
}

# Hash of requirements.txt, or empty if it cannot be read. Uses the venv's own
# python so there is no dependency on sha256sum/shasum being installed.
backend_requirements_hash() {
    local py="$1"
    "$py" - "$BACKEND_DIR/requirements.txt" <<'PY' 2>/dev/null
import hashlib, sys
try:
    with open(sys.argv[1], 'rb') as fh:
        print(hashlib.sha256(fh.read()).hexdigest())
except OSError:
    pass
PY
}

ensure_backend_packages() {
    local py marker want have
    py="$(backend_python)"
    marker="$BACKEND_VENV_DIR/.requirements-sha256"

    # Two independent reasons to install. The import probe catches a venv that
    # is missing or broken; the hash catches the case it cannot see -- a NEW
    # requirement added since the last install, where all four probe imports
    # still succeed and the app dies on the fifth. That was a recurring
    # session-derailer: a dependency lands, dev.sh reports nothing, and the
    # backend boots straight into ModuleNotFoundError.
    if python_command_works "$py" && "$py" - <<'PY' >/dev/null 2>&1
import dotenv
import flask
import flask_socketio
import sqlalchemy
PY
    then
        want="$(backend_requirements_hash "$py")"
        have=""
        [ -f "$marker" ] && have="$(cat "$marker" 2>/dev/null)"
        # An unreadable requirements.txt yields an empty hash; treat that as
        # "cannot tell" and leave a working venv alone rather than reinstalling
        # on every start.
        if [ -z "$want" ] || [ "$want" = "$have" ]; then
            return 0
        fi
        echo -e "${YELLOW}requirements.txt changed since the last install; syncing...${NC}"
    fi

    echo -e "${YELLOW}Installing backend Python dependencies in WSL venv...${NC}"
    "$py" -m pip install --upgrade pip
    "$py" -m pip install -r "$BACKEND_DIR/requirements.txt"

    # Only stamp after a successful install, so a failed one retries next time.
    want="$(backend_requirements_hash "$py")"
    [ -n "$want" ] && printf '%s\n' "$want" > "$marker"
}

start_backend_process() {
    export_dev_env
    cd "$BACKEND_DIR"
    activate_backend_venv
    ensure_backend_packages
    "$(backend_python)" run.py
}

start_frontend_process() {
    export_dev_env
    write_frontend_env
    cd "$FRONTEND_DIR"
    npm run dev -- --host "$(frontend_bind_host)" --port "$FRONTEND_ACTUAL_PORT" --strictPort
}

start_backend() {
    stop_listeners_on_port "$BACKEND_PORT" "backend"
    new_dev_settings 0 0
    header "Starting Backend ($BACKEND_URL)"
    start_backend_process
}

start_frontend() {
    stop_listeners_on_port "$FRONTEND_PORT" "frontend"
    new_dev_settings 1 0
    header "Starting Frontend ($FRONTEND_URL)"
    echo "  API target: $API_URL"
    if [ "$FRONTEND_PORT_CHANGED" -eq 1 ]; then
        echo -e "  ${YELLOW}Note:${NC} frontend port $FRONTEND_PORT was busy; using $FRONTEND_ACTUAL_PORT."
    fi
    echo
    start_frontend_process
}

cleanup_pids() {
    local pid
    for pid in "$@"; do
        if [ -n "${pid:-}" ]; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    for pid in "$@"; do
        if [ -n "${pid:-}" ]; then
            wait "$pid" 2>/dev/null || true
        fi
    done
}

wait_for_first_exit() {
    if help wait 2>/dev/null | grep -q -- '-n'; then
        set +e
        wait -n "$@"
        local status=$?
        set -e
        return "$status"
    fi

    local pid running
    while true; do
        running="$(jobs -pr)"
        for pid in "$@"; do
            if ! printf '%s\n' "$running" | grep -qx "$pid"; then
                set +e
                wait "$pid" 2>/dev/null
                local status=$?
                set -e
                return "$status"
            fi
        done
        sleep 1
    done
}

start_both() {
    stop_listeners_on_port "$BACKEND_PORT" "backend"
    stop_listeners_on_port "$FRONTEND_PORT" "frontend"
    new_dev_settings 0 0
    print_dev_summary

    start_backend_process &
    local backend_pid=$!

    sleep 2

    start_frontend_process &
    local frontend_pid=$!

    trap 'echo; echo -e "${YELLOW}Stopping...${NC}"; cleanup_pids "$backend_pid" "$frontend_pid"; echo "Stopped."; exit 0' INT TERM

    echo -e "${DIM}Press Ctrl+C to stop...${NC}"
    set +e
    wait_for_first_exit "$backend_pid" "$frontend_pid"
    local status=$?
    set -e

    echo
    echo -e "${YELLOW}One dev process stopped; shutting down the rest.${NC}"
    cleanup_pids "$backend_pid" "$frontend_pid"
    return "$status"
}

start_tunnel() {
    if ! command -v ngrok >/dev/null 2>&1; then
        echo -e "${RED}Error:${NC} ngrok is not installed or not in PATH."
        echo
        echo "Install ngrok:"
        echo "  - https://ngrok.com/download"
        echo "  - Windows: choco install ngrok  (or scoop install ngrok)"
        echo "  - WSL/Linux: snap install ngrok  (or download the .tgz)"
        echo
        echo "Then authenticate once: ngrok config add-authtoken <YOUR_TOKEN>"
        exit 1
    fi

    stop_listeners_on_port "$BACKEND_PORT" "backend"
    stop_listeners_on_port "$FRONTEND_PORT" "frontend"
    new_dev_settings 0 1
    print_dev_summary "ServerKit Dev Server (with ngrok tunnel)"
    echo "  Tunnel:   exposing backend port $BACKEND_ACTUAL_PORT via ngrok"
    echo
    echo -e "${YELLOW}NOTE:${NC} Agents and remote callers should use the public ngrok URL"
    echo "      printed below as their --server / control plane URL."
    echo

    start_backend_process &
    local backend_pid=$!

    sleep 2

    start_frontend_process &
    local frontend_pid=$!

    sleep 1

    local ngrok_args=(http "$BACKEND_ACTUAL_PORT" --log=stdout)
    if [ -n "${NGROK_DOMAIN:-}" ]; then
        ngrok_args+=("--domain=$NGROK_DOMAIN")
    fi
    if [ -n "${NGROK_AUTHTOKEN:-}" ]; then
        ngrok_args+=("--authtoken=$NGROK_AUTHTOKEN")
    fi

    ngrok "${ngrok_args[@]}" &
    local ngrok_pid=$!

    (
        local url
        for _ in $(seq 1 30); do
            sleep 1
            if command -v curl >/dev/null 2>&1; then
                url="$(curl -fsS http://127.0.0.1:4040/api/tunnels 2>/dev/null \
                    | grep -oE '"public_url":"https://[^"]+' \
                    | head -n1 \
                    | sed 's/"public_url":"//')" || true
                if [ -n "$url" ]; then
                    echo
                    echo -e "${GREEN}=========================================================${NC}"
                    echo -e "${GREEN}  Public tunnel URL: ${NC}${CYAN}$url${NC}"
                    echo -e "${GREEN}  Use this as your agent --server / control plane URL.${NC}"
                    echo -e "${GREEN}=========================================================${NC}"
                    echo
                    break
                fi
            fi
        done
    ) &
    local poll_pid=$!

    trap 'echo; echo -e "${YELLOW}Stopping...${NC}"; cleanup_pids "$backend_pid" "$frontend_pid" "$ngrok_pid" "$poll_pid"; echo "Stopped."; exit 0' INT TERM

    echo -e "${DIM}Press Ctrl+C to stop...${NC}"
    set +e
    wait_for_first_exit "$backend_pid" "$frontend_pid" "$ngrok_pid"
    local status=$?
    set -e

    echo
    echo -e "${YELLOW}One tunnel process stopped; shutting down the rest.${NC}"
    cleanup_pids "$backend_pid" "$frontend_pid" "$ngrok_pid" "$poll_pid"
    return "$status"
}

run_validate() {
    header "ServerKit Validation Suite"
    local failed=0
    local passed=0

    echo -e "${YELLOW}Running ESLint...${NC}"
    if (cd "$FRONTEND_DIR" && npm run lint 2>&1); then
        pass "ESLint"
        passed=$((passed + 1))
    else
        echo -e "  ${YELLOW}WARN${NC} ESLint (has warnings/errors - run 'cd frontend && npm run lint' for details)"
        passed=$((passed + 1))
    fi

    echo -e "${YELLOW}Running Bandit...${NC}"
    if command -v bandit >/dev/null 2>&1; then
        if bandit -r "$BACKEND_DIR/app" --ini "$BACKEND_DIR/.bandit" --severity-level medium 2>&1; then
            pass "Bandit (security scan)"
            passed=$((passed + 1))
        else
            fail "Bandit (security scan)"
            failed=$((failed + 1))
        fi
    else
        fail "Bandit (not installed - pip install bandit)"
        failed=$((failed + 1))
    fi

    echo -e "${YELLOW}Running Pytest...${NC}"
    if (cd "$BACKEND_DIR" && activate_backend_venv && pytest --tb=short -q 2>&1); then
        pass "Pytest"
        passed=$((passed + 1))
    else
        fail "Pytest"
        failed=$((failed + 1))
    fi

    echo -e "${YELLOW}Running Frontend build...${NC}"
    if (cd "$FRONTEND_DIR" && npm run build 2>&1); then
        pass "Frontend build"
        passed=$((passed + 1))
    else
        fail "Frontend build"
        failed=$((failed + 1))
    fi

    header "Results"
    echo -e "  ${GREEN}Passed: $passed${NC}"
    if [ "$failed" -gt 0 ]; then
        echo -e "  ${RED}Failed: $failed${NC}"
        return 1
    fi

    echo -e "  ${GREEN}All checks passed!${NC}"
}

run_validate_watch() {
    run_validate || true

    if command -v inotifywait >/dev/null 2>&1; then
        echo -e "${DIM}Watching for changes... (Ctrl+C to stop)${NC}"
        while true; do
            inotifywait -r -q -e modify,create,delete \
                --include '\.(py|js|jsx|ts|tsx)$' \
                "$BACKEND_DIR/app" "$FRONTEND_DIR/src" 2>/dev/null || break
            echo -e "\n${YELLOW}Change detected, re-running...${NC}"
            sleep 1
            run_validate || true
        done
    else
        echo
        echo -e "${DIM}Install inotify-tools for file watching. Running one-shot validation only.${NC}"
    fi
}

parse_args "$@"

case "$MODE" in
    backend) start_backend ;;
    frontend) start_frontend ;;
    tunnel) start_tunnel ;;
    validate) run_validate_watch ;;
    start) start_both ;;
    *) die "Unknown mode: $MODE" ;;
esac
