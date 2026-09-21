#!/bin/bash
#
# ServerKit bootstrap installer.
#
#   curl -fsSL https://serverkit.ai/install.sh | bash
#
# By default the script clones the repository and builds from source. Two
# environment switches change that:
#
#   INSTALL_FROM_RELEASE=1   fetch a pre-built tarball instead of compiling
#   BUILD_FROM_SOURCE=1      force a source build even when a release exists
#   SERVERKIT_SKIP_SSL=1     run on plain HTTP (no HTTPS / no certbot attempt)
#
# Running your own reverse proxy (Caddy / Traefik / nginx) in front of the panel:
#
#   SERVERKIT_EXTERNAL_PROXY=1   your proxy terminates TLS; ours must not
#                                redirect to HTTPS. Implies SERVERKIT_SKIP_SSL=1
#                                and configures the trusted-proxy client IP.
#   SERVERKIT_PUBLIC_URL=...     the public https:// URL browsers use. Required
#                                with EXTERNAL_PROXY or realtime updates break.
#
# Re-runnable installs:
#
#   SERVERKIT_CONFIG=/path/install.conf   KEY=VALUE defaults for any of the
#                                         variables above. Explicit environment
#                                         still wins over the file.
#
# The Flask backend runs straight on the host (it needs real system access);
# the React frontend is built to static files and served by the host nginx.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Declarative install config (SERVERKIT_CONFIG)
# ---------------------------------------------------------------------------
# Sourced before any default is applied, so a file entry behaves exactly like
# passing that variable on the command line. Runs this early — ahead of the
# terminal-styling helpers — so it can only use plain printf/exit.
load_install_config() {
    local file="${SERVERKIT_CONFIG:-}"
    [ -n "$file" ] || return 0
    if [ ! -f "$file" ]; then
        printf 'ERROR: SERVERKIT_CONFIG file not found: %s\n' "$file" >&2
        exit 1
    fi

    # The file supplies DEFAULTS. A variable already present in the environment
    # was passed explicitly for this run and must win, so apply keys one at a
    # time and skip the ones already set — sourcing the file wholesale would
    # invert that precedence. Parsing (rather than sourcing) also keeps a stray
    # command in the file from executing as root.
    local line key value
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"      # strip leading whitespace
        case "$line" in ''|'#'*) continue ;; esac
        case "$line" in 'export '*) line="${line#export }" ;; esac
        case "$line" in *=*) ;; *) continue ;; esac
        key="${line%%=*}"
        value="${line#*=}"
        # Names only; anything else is a typo, not a variable.
        case "$key" in ''|*[!A-Za-z0-9_]*) continue ;; esac
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        [ -z "${!key+set}" ] || continue
        export "$key=$value"
    done < "$file"
}
load_install_config

# ---------------------------------------------------------------------------
# Settings and environment contract
# ---------------------------------------------------------------------------
SERVERKIT_DIR="${SERVERKIT_DIR:-/opt/serverkit}"
INSTALL_DIR="$SERVERKIT_DIR"
BASE_NAME="$(basename "$INSTALL_DIR")"
BASE_DIR="$(dirname "$INSTALL_DIR")"
DIR_A="$BASE_DIR/${BASE_NAME}-a"
DIR_B="$BASE_DIR/${BASE_NAME}-b"
VENV_DIR="${SERVERKIT_VENV_DIR:-$INSTALL_DIR/venv}"
LOG_DIR="/var/log/serverkit"
DATA_DIR="/var/lib/serverkit"
BACKUP_DIR="/var/backups/serverkit"
CONFIG_DIR="/etc/serverkit"

PYTHON_MIN="3.11"
# 3.13 included since issue #99: Debian 13 (trixie) ships ONLY python3.13 — no
# 3.11, no 3.12 — so a 3.11-3.12 gate could never be satisfied from its repos
# and every install there fell through to the source build. Ubuntu 26.04 lands
# in the same place. Raising the ceiling needed SQLAlchemy >= 2.0.31 (see
# backend/requirements.txt) and dropping two stacked @classmethod/@staticmethod
# decorators that 3.13 no longer collapses.
# 3.14 included since the 2026-08 distro-compatibility baseline: Arch/Manjaro
# rolling ship ONLY python 3.14 from their repos (no versioned python3.1x
# packages exist to fall back to), so a ceiling of 3.13 made every Arch-family
# install fall through to the source build — the same trap trixie was in.
PYTHON_MAX="3.14"
PYTHON_BIN=""

GITHUB_REPO="${GITHUB_REPO:-jhd3197/ServerKit}"
INSTALL_FROM_RELEASE="${INSTALL_FROM_RELEASE:-0}"
BUILD_FROM_SOURCE="${BUILD_FROM_SOURCE:-0}"
SERVERKIT_VERSION="${SERVERKIT_VERSION:-}"
VERSION="${VERSION:-${SERVERKIT_VERSION:-1.4.11}}"
CHANNEL="${CHANNEL:-Stable}"

# Install profile: what we put on the box, not what the box is allowed to do.
#   minimal  — panel + nginx + SQLite. No Docker.
#   standard — + Docker and the compose plugin (the default).
#   full     — + recommended extensions and hardening.
# Everything a profile skips stays installable later from the panel. Set
# SERVERKIT_PROFILE to skip the interactive prompt. Thresholds below are
# mirrored by recommend_profile() in backend/app/services/install_profile_service.py
# — change one, change the other.
PROFILE="${SERVERKIT_PROFILE:-}"
RECOMMENDED_PROFILE="standard"
PROFILE_PROMPT_TIMEOUT="${SERVERKIT_PROFILE_TIMEOUT:-15}"

PANEL_DOMAIN="${PANEL_DOMAIN:-}"
PANEL_PORT="${PANEL_PORT:-80}"
SERVERKIT_SKIP_SSL="${SERVERKIT_SKIP_SSL:-0}"

# An external reverse proxy (Caddy, Traefik, nginx elsewhere) terminates TLS in
# front of the panel and speaks plain HTTP upstream. Our own nginx must then NOT
# redirect :80 to HTTPS — the proxy would follow that redirect straight back
# into itself, which is the "too many redirects" the browser reports. So this
# implies SKIP_SSL, and it also turns on the trusted-proxy client-IP path with
# two hops (their proxy appends the client, our nginx appends their proxy).
SERVERKIT_EXTERNAL_PROXY="${SERVERKIT_EXTERNAL_PROXY:-0}"
if [ "$SERVERKIT_EXTERNAL_PROXY" = "1" ]; then
    SERVERKIT_SKIP_SSL=1
fi
# The public https:// URL browsers use. Behind a proxy the panel cannot infer
# it, and the Socket.IO handshake rejects the browser's Origin without it.
PANEL_PUBLIC_URL="${SERVERKIT_PUBLIC_URL:-}"
PANEL_PUBLIC_URL="${PANEL_PUBLIC_URL%/}"
SERVERKIT_OFFLINE_TARBALL="${SERVERKIT_OFFLINE_TARBALL:-}"
SERVERKIT_MIRROR_URL="${SERVERKIT_MIRROR_URL:-}"

# Runtime state populated as we go (declared up-front for `set -u`).
OS_FAMILY="unknown"
ARCH=""
DL_ARCH=""
PKG_MGR=""
SAFE_MODE=false
SSL_MODE="insecure"
FIRST_SLOT="$DIR_A"

# Resolve where this script (and thus the bundled scripts/lib helpers) lives so
# shared libraries load whether we run from a clone, a release tree, or piped
# through `curl | bash`. The clone-relative path is tried first; once the source
# is on disk under $INSTALL_DIR that path works too.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
load_serverkit_lib() {
    local name="$1" d
    for d in "$SELF_DIR/scripts/lib" "$INSTALL_DIR/scripts/lib"; do
        if [ -n "$d" ] && [ -f "$d/$name" ]; then
            # shellcheck source=/dev/null
            source "$d/$name"
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# Terminal styling
#
# Truecolor is used when the stream is an interactive terminal that has not
# opted out. The ServerKit identity is a violet ramp (V1 brightest .. V5
# deepest); status colors sit alongside it. Everything degrades to plain text
# when colour is unavailable so piped logs stay readable.
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    ESC=$'\033'
    RST="${ESC}[0m"; BLD="${ESC}[1m"; DIMM="${ESC}[2m"
    paint() { printf '%s[38;2;%d;%d;%dm' "$ESC" "$1" "$2" "$3"; }
else
    RST=''; BLD=''; DIMM=''
    paint() { :; }
fi

V1="$(paint 196 181 253)"; V2="$(paint 167 139 250)"; V3="$(paint 139 92 246)"
V4="$(paint 124 58 237)";  V5="$(paint 109 40 217)"
PAPER="$(paint 237 233 254)"; ASH="$(paint 165 160 190)"; FOG="$(paint 113 108 140)"
HUE_OK="$(paint 52 211 153)"; HUE_WARN="$(paint 250 204 21)"
HUE_ERR="$(paint 248 113 113)"; HUE_LINK="$(paint 103 232 249)"

# Line-level reporting. Distinct verbs, distinct marks, single indent.
good()  { printf '  %s✔%s %s\n'  "$HUE_OK"   "$RST" "$1"; }
warn()  { printf '  %s▴%s %s\n'  "$HUE_WARN" "$RST" "$1"; }
halt()  { printf '  %s✘%s %s\n'  "$HUE_ERR"  "$RST" "$1" >&2; exit 1; }
step()  { printf '  %s❯%s %s\n'  "$HUE_LINK" "$RST" "$1"; }
faint() { printf '  %s%s%s\n'    "$FOG" "$1" "$RST"; }

# ---------------------------------------------------------------------------
# Phase headers
#
# Each major stage prints a numbered, time-stamped header followed by a violet
# rule. There is no global step total — the index plus elapsed clock is enough
# to read progress, and it never drifts out of sync with the actual path taken
# (source vs. release installs run different stages).
# ---------------------------------------------------------------------------
STARTED_AT=0
PHASE_N=0

clock() {
    [ "$STARTED_AT" -gt 0 ] || { printf ''; return; }
    local secs=$(( $(date +%s) - STARTED_AT ))
    printf '%dm %02ds' "$((secs / 60))" "$((secs % 60))"
}

phase() {
    PHASE_N=$((PHASE_N + 1))
    local t; t="$(clock)"
    printf '\n  %s%s%02d%s  %s%s%s  %s%s%s\n' \
        "$BLD" "$V3" "$PHASE_N" "$RST" "$BLD" "$1" "$RST" "$FOG" "$t" "$RST"
    printf '  %s%s%s\n\n' "$V4" "──────────────────────────────────────" "$RST"
}

# ---------------------------------------------------------------------------
# Masthead
# ---------------------------------------------------------------------------
masthead() {
    printf '\n'
    printf '  %s%s▖▌▌%s  %s%sServerKit%s  %sv%s%s  %s•%s %s%s%s\n' \
        "$BLD" "$V2" "$RST" "$BLD" "$PAPER" "$RST" "$FOG" "$VERSION" "$RST" \
        "$HUE_OK" "$RST" "$ASH" "$CHANNEL" "$RST"
    printf '  %s%s▌▖▌%s  %sSelf-hosted infrastructure, made simple.%s\n' \
        "$BLD" "$V3" "$RST" "$ASH" "$RST"
    printf '  %s%s▌▌▖%s  %sWeb apps · Databases · Docker · Email · DNS · Security%s\n' \
        "$BLD" "$V4" "$RST" "$FOG" "$RST"
    printf '  %s%s▘▘▘%s  %sPython + React, one command · serverkit.ai%s\n' \
        "$BLD" "$V5" "$RST" "$FOG" "$RST"
    printf '\n'
}

# ---------------------------------------------------------------------------
# Pre-flight: disk, memory, privileges
# ---------------------------------------------------------------------------
preflight() {
    step "Running pre-flight checks..."

    # Root first: every check and phase after this point may write to the
    # host (choose_pkg_manager drops an apt config file), so a non-root run
    # must die on THIS friendly message, not on the first raw
    # "Permission denied". (I11)
    if [ "$EUID" -ne 0 ]; then
        halt "Please run this installer as root (use sudo)."
    fi

    # Source builds need more headroom than unpacking a release.
    local need_kb=5242880
    [ "$INSTALL_FROM_RELEASE" = "1" ] && need_kb=2097152

    # Probe the filesystem that will actually hold the install — the target
    # tree rarely exists yet, so walk up to the nearest existing ancestor —
    # and use POSIX -Pk output so a long device name cannot wrap the line
    # and skew the awk parse. (I14)
    local probe="$INSTALL_DIR" parent
    while [ ! -d "$probe" ]; do
        parent="$(dirname "$probe")"
        [ "$parent" != "$probe" ] || break
        probe="$parent"
    done
    local free_kb
    free_kb=$(df -Pk "$probe" 2>/dev/null | awk 'NR==2 {print $4}') || free_kb=""
    if [ -n "$free_kb" ] && [ "$free_kb" -lt "$need_kb" ]; then
        halt "Need at least $((need_kb / 1024 / 1024))GB free on $probe; less is available."
    fi

    # `free` is missing from some LXC templates — skip the advisory memory
    # check rather than aborting on the bare assignment. (I14)
    local free_mem=""
    if command -v free &>/dev/null; then
        free_mem=$(free -m 2>/dev/null | awk '/^Mem:/ {print $7}') || free_mem=""
    else
        warn "'free' not found — skipping the memory check."
    fi
    if [ -n "$free_mem" ] && [ "$free_mem" -lt 256 ]; then
        warn "Under 256MB memory free — the install may run slowly."
    fi

    good "Pre-flight checks passed."
}

# ---------------------------------------------------------------------------
# Identify the OS family and CPU architecture
# ---------------------------------------------------------------------------
# Pure mapping from /etc/os-release ID (+ ID_LIKE fallback) to a ServerKit
# family. Kept as a standalone function so it is unit-testable without a real
# /etc/os-release. Echoes: debian|fedora|rhel|suse|arch|alpine|gentoo|unknown.
os_family_from() {
    local id="$1" id_like="$2"
    case "$id" in
        ubuntu|linuxmint|pop|raspbian|elementary|zorin|debian|devuan)
            printf 'debian\n'; return ;;
        fedora|nobara)
            printf 'fedora\n'; return ;;
        rocky|almalinux|rhel|centos|ol|oracle|eurolinux)
            printf 'rhel\n'; return ;;
        opensuse*|sles|sled|suse|sle-micro)
            printf 'suse\n'; return ;;
        arch|manjaro|endeavouros|cachyos)
            printf 'arch\n'; return ;;
        alpine)
            printf 'alpine\n'; return ;;
        gentoo|funtoo)
            printf 'gentoo\n'; return ;;
    esac
    # Unknown ID — map via ID_LIKE to the closest supported family. Check rhel
    # before fedora: RHEL clones advertise ID_LIKE="rhel centos fedora" and want
    # the RHEL Docker repo, whereas a pure Fedora spin only lists "fedora".
    case " $id_like " in
        *debian*|*ubuntu*) printf 'debian\n' ;;
        *rhel*|*centos*)   printf 'rhel\n' ;;
        *fedora*)          printf 'fedora\n' ;;
        *suse*)            printf 'suse\n' ;;
        *arch*)            printf 'arch\n' ;;
        *alpine*)          printf 'alpine\n' ;;
        *gentoo*)          printf 'gentoo\n' ;;
        *)                 printf 'unknown\n' ;;
    esac
}

identify_system() {
    phase "Detecting System"

    local os_release="${SERVERKIT_OS_RELEASE:-/etc/os-release}"
    [ -f "$os_release" ] || halt "Cannot detect OS — $os_release is missing."
    # os-release defines its own VERSION ("24.04.1 LTS ..."), which would
    # clobber the installer's version string; ID/PRETTY_NAME are wanted
    # globals (provision_python keys off ID), so source in place and restore
    # just our VERSION afterwards. (I22)
    local sk_version="$VERSION"
    . "$os_release"
    VERSION="$sk_version"

    OS_FAMILY="$(os_family_from "${ID:-}" "${ID_LIKE:-}")"
    case "$OS_FAMILY" in
        unknown)
            warn "Untested OS '${ID:-unknown}' (ID_LIKE='${ID_LIKE:-}') — continuing anyway." ;;
        *)
            if [ "${ID:-}" = "$OS_FAMILY" ] || \
               { [ "$OS_FAMILY" = "debian" ] && [ "${ID:-}" = "ubuntu" ]; }; then
                good "Detected: ${PRETTY_NAME:-$ID} ($OS_FAMILY family)"
            else
                warn "Detected: ${PRETTY_NAME:-${ID:-unknown}} — treating as '$OS_FAMILY' family."
            fi
            ;;
    esac

    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)          DL_ARCH="amd64"; good "Architecture: x86_64" ;;
        aarch64|arm64)   DL_ARCH="arm64"; good "Architecture: ARM64" ;;
        *)               halt "Unsupported architecture: $ARCH" ;;
    esac
}

# ---------------------------------------------------------------------------
# Package manager abstraction (apt / dnf / yum / zypper / pacman / apk / emerge)
# ---------------------------------------------------------------------------
# Detection order mirrors scripts/lib/pkg.sh. These run during the early
# dependency phase — before the repo (and thus scripts/lib) is on disk in the
# `curl | bash` path — so the logic is inline here and kept in sync with the lib.
choose_pkg_manager() {
    if command -v apt-get &>/dev/null; then
        PKG_MGR="apt"
        # unattended-upgrades can hold the dpkg lock right after boot; tell
        # apt to wait for it rather than failing outright.
        mkdir -p /etc/apt/apt.conf.d
        cat > /etc/apt/apt.conf.d/99-serverkit-lock-wait.conf <<'APT_EOF'
DPkg::Lock::Timeout "300";
APT_EOF
    elif command -v dnf &>/dev/null; then
        PKG_MGR="dnf"
    elif command -v yum &>/dev/null; then
        PKG_MGR="yum"
    elif command -v zypper &>/dev/null; then
        PKG_MGR="zypper"
    elif command -v pacman &>/dev/null; then
        PKG_MGR="pacman"
    elif command -v apk &>/dev/null; then
        PKG_MGR="apk"
    elif command -v emerge &>/dev/null; then
        PKG_MGR="emerge"
    else
        halt "No supported package manager found (need apt, dnf, yum, zypper, pacman, apk, or emerge)."
    fi
}

refresh_pkg_index() {
    # Best-effort by design (mirrors scripts/lib/pkg.sh pkg_refresh): a failed
    # index refresh — flaky mirror, momentary lock — must never abort the
    # install, so every arm is guarded and the function always returns 0.
    # The --refresh flag is dnf-only; classic yum has no such flag, so its arm
    # is a plain makecache (same as pkg.sh).
    case "$PKG_MGR" in
        apt)    apt-get update -y >/dev/null 2>&1 || true ;;
        dnf)    dnf makecache --refresh >/dev/null 2>&1 || true ;;
        yum)    yum makecache >/dev/null 2>&1 || true ;;
        zypper) zypper --non-interactive refresh >/dev/null 2>&1 || true ;;
        pacman) pacman -Sy --noconfirm >/dev/null 2>&1 || true ;;
        apk)    apk update >/dev/null 2>&1 || true ;;
        # Portage keeps a full repo snapshot; --sync just refreshes it.
        emerge) emerge --sync >/dev/null 2>&1 || true ;;
    esac
    return 0
}

# Minimal images (Docker base layers, LXC templates, geerlingguy systemd
# containers) can lack curl AND ship with empty package indexes — every later
# phase (release fetch, NodeSource setup, git/venv installs) then fails in
# confusing, hard-to-read ways. Refresh the index once and bootstrap curl up
# front; both are best-effort, and later phases re-probe what they need.
ensure_bootstrap_tools() {
    refresh_pkg_index
    if ! command -v curl &>/dev/null; then
        step "Installing curl (needed for release downloads and repo setup)..."
        pkg_add curl ca-certificates
        command -v curl &>/dev/null || \
            warn "curl could not be installed — release downloads and NodeSource setup will fail; source-build paths remain."
    fi
}

# Warn-and-continue package install. Output is captured so a failure can be
# reported with the manager's last lines — and that capture carries an
# `|| rc=$?` guard because a bare `out=$(apt-get ...)` assignment is itself
# an abort point under `set -e`: the old body died on the assignment, before
# it could print anything at all. Always returns 0; callers that *require* a
# package must probe the resulting state (command -v, locate_python, ...)
# rather than this exit code.
pkg_add() {
    local out="" rc=0
    case "$PKG_MGR" in
        # -y is NOT enough on Debian/Ubuntu. It answers apt's own prompts but
        # not debconf's: tzdata, keyboard-configuration and friends still open
        # an interactive dialog and wait forever. Because this output is
        # captured, the installer then hangs in total silence — observed as a
        # 60-minute stall on ubuntu:22.04 with the prompt invisible. Worse
        # under `curl | bash`, where stdin is the script itself, so debconf can
        # read installer source as its answers. The Dpkg options cover the
        # other prompt class (modified conffiles), which DEBIAN_FRONTEND alone
        # does not suppress. Every other manager here was already guarded.
        apt)    out=$(DEBIAN_FRONTEND=noninteractive apt-get install -y \
                        -o Dpkg::Options::=--force-confold \
                        -o Dpkg::Options::=--force-confdef "$@" 2>&1) || rc=$? ;;
        dnf)    out=$(dnf install -y "$@" 2>&1) || rc=$? ;;
        yum)    out=$(yum install -y "$@" 2>&1) || rc=$? ;;
        zypper) out=$(zypper --non-interactive install "$@" 2>&1) || rc=$? ;;
        pacman) out=$(pacman -S --noconfirm "$@" 2>&1) || rc=$? ;;
        apk)    out=$(apk add "$@" 2>&1) || rc=$? ;;
        # --noreplace: already-installed slots are a success, not an error.
        # --ask=n: emerge never prompts by default; pin it anyway (every other
        # arm here carries an explicit non-interactive guard).
        emerge) out=$(emerge --noreplace --ask=n "$@" 2>&1) || rc=$? ;;
        *)      warn "No supported package manager — cannot install: $*"; return 0 ;;
    esac
    if [ "$rc" -ne 0 ]; then
        warn "Could not install: $* (exit $rc)"
        printf '%s\n' "$out" | tail -5 >&2 || true
    fi
    return 0
}

# ---------------------------------------------------------------------------
# RHEL-family (Rocky/Alma/RHEL/CentOS 9): upgrade openssh + openssl TOGETHER,
# up front, before any other dnf transaction runs.
#
# Rocky 9 images ship openssh linked against openssl-libs 3.0.x while the
# updates stream carries openssl-libs 3.5.x plus a matching openssh rebuild.
# Any later `dnf install` that pulls dependencies can transitively upgrade
# openssl-libs; if openssh is not upgraded in the SAME transaction, every new
# sshd fork dies with "OpenSSL version mismatch" — remote installs over SSH
# then cut themselves off mid-run. Upgrading both together lets openssh's
# scriptlet restart sshd against the matched libssl (KillMode=process keeps
# the live SSH session alive). Do NOT rewrite this with --exclude=openssl
# shapes; they fail on python3-libs symbol requirements. Best-effort: a box
# without the updates repo (or already current) is a clean no-op.
# ---------------------------------------------------------------------------
upgrade_rhel_crypto_stack() {
    [ "$OS_FAMILY" = "rhel" ] || return 0
    command -v dnf &>/dev/null || return 0
    step "Upgrading openssh/openssl together (avoids sshd 'OpenSSL version mismatch')..."
    dnf upgrade -y openssh openssh-server openssh-clients openssl openssl-libs openssl-devel 2>/dev/null || true
    return 0
}

# ---------------------------------------------------------------------------
# Memory tuning: low-RAM safe mode and a swap fallback
# ---------------------------------------------------------------------------
gauge_memory() {
    # `free` is missing from some LXC templates — degrade to "no safe mode"
    # instead of aborting on the bare assignment under pipefail. (I14)
    local total=""
    if command -v free &>/dev/null; then
        total=$(free -m 2>/dev/null | awk '/^Mem:/ {print $2}') || total=""
    fi
    if [ -z "$total" ]; then
        SAFE_MODE=false
        warn "Cannot read total memory ('free' missing) — skipping the low-RAM check."
        return 0
    fi
    if [ "$total" -le 700 ]; then
        SAFE_MODE=true
        warn "Low RAM (${total}MB) — enabling VPS safe mode."
    else
        SAFE_MODE=false
    fi
}

detect_container() {
    # Echo the container flavour, or nothing on bare metal / a full VM. This
    # matters more than core count: Docker frequently cannot run inside an
    # unprivileged LXC or OpenVZ guest, so such a box can look adequate on
    # paper and still be unable to host a single app.
    if [ -f /.dockerenv ]; then
        printf 'docker'
        return 0
    fi
    if [ -d /proc/vz ] && [ ! -d /proc/bc ]; then
        printf 'openvz'
        return 0
    fi
    if command -v systemd-detect-virt &>/dev/null; then
        local virt
        virt=$(systemd-detect-virt --container 2>/dev/null) || virt=""
        if [ -n "$virt" ] && [ "$virt" != "none" ]; then
            printf '%s' "$virt"
            return 0
        fi
    fi
    # /proc/1/environ carries container= for LXC and systemd-nspawn.
    if [ -r /proc/1/environ ]; then
        if tr '\0' '\n' < /proc/1/environ 2>/dev/null | grep -q '^container=lxc'; then
            printf 'lxc'
            return 0
        fi
    fi
    return 0
}

recommend_profile() {
    # Pick the profile that this hardware can actually carry. Deliberately
    # conservative — an operator can always override upward, but silently
    # defaulting a 700MB VPS to a Docker install produces a box that installs
    # cleanly and then OOMs on the first deploy.
    local ram_mb=0 cores=1 disk_gb="" container=""

    if command -v free &>/dev/null; then
        ram_mb=$(free -m 2>/dev/null | awk '/^Mem:/ {print $2}') || ram_mb=0
    fi
    [ -n "$ram_mb" ] || ram_mb=0

    if command -v nproc &>/dev/null; then
        cores=$(nproc 2>/dev/null) || cores=1
    fi
    [ -n "$cores" ] || cores=1

    # Free space on the volume that will hold images and backups.
    if command -v df &>/dev/null; then
        disk_gb=$(df -BG "$BASE_DIR" 2>/dev/null | awk 'NR==2 {gsub(/G/,"",$4); print $4}') || disk_gb=""
    fi

    container=$(detect_container)

    RECOMMENDED_PROFILE="standard"

    if [ "$container" = "lxc" ] || [ "$container" = "openvz" ]; then
        RECOMMENDED_PROFILE="minimal"
        return 0
    fi
    # 1.5GB, expressed in MB to stay in integer arithmetic.
    if [ "$ram_mb" -gt 0 ] && [ "$ram_mb" -lt 1536 ]; then
        RECOMMENDED_PROFILE="minimal"
        return 0
    fi
    if [ -n "$disk_gb" ] && [ "$disk_gb" -lt 5 ]; then
        RECOMMENDED_PROFILE="minimal"
        return 0
    fi
    if [ "$ram_mb" -ge 4096 ] && [ "$cores" -ge 4 ]; then
        if [ -z "$disk_gb" ] || [ "$disk_gb" -ge 20 ]; then
            RECOMMENDED_PROFILE="full"
        fi
    fi
    return 0
}

prompt_for_profile() {
    # Honour an explicit choice and never ask again.
    case "$PROFILE" in
        minimal|standard|full)
            good "Install profile: $PROFILE (from SERVERKIT_PROFILE)"
            return 0
            ;;
        "")
            ;;
        *)
            warn "Unknown SERVERKIT_PROFILE='$PROFILE' — ignoring it."
            PROFILE=""
            ;;
    esac

    recommend_profile

    # curl | bash has no interactive stdin. Take the recommendation rather than
    # blocking forever. SERVERKIT_FORCE_PROMPT=1 lets the unit tests drive this
    # from a pipe (same hook prompt_for_domain uses).
    if [ ! -t 0 ] && [ "${SERVERKIT_FORCE_PROMPT:-0}" != "1" ]; then
        PROFILE="$RECOMMENDED_PROFILE"
        good "Install profile: $PROFILE (auto-detected)"
        return 0
    fi

    printf '\n'
    printf '%sHow much should we install?%s\n' "$BLD" "$RST"
    printf '  %s1) minimal%s  Panel, nginx and SQLite. No Docker.\n' "$BLD" "$RST"
    printf '              Monitoring, domains, certificates, cron and DNS all work.\n'
    printf '  %s2) standard%s Adds Docker so this server can host apps.\n' "$BLD" "$RST"
    printf '  %s3) full%s     Adds recommended extensions and hardening.\n' "$BLD" "$RST"
    printf '\n'
    printf 'Detected hardware suggests: %s%s%s\n' "$BLD" "$RECOMMENDED_PROFILE" "$RST"
    printf 'Anything skipped can be installed later from the panel.\n'
    printf '%sTip:%s set SERVERKIT_PROFILE=minimal|standard|full to skip this prompt\n' "$BLD" "$RST"
    printf '> [%s] ' "$RECOMMENDED_PROFILE"

    local answer=""
    # A timeout keeps a half-attended install moving instead of parking on the
    # prompt; the recommendation is the fallback either way.
    read -r -t "$PROFILE_PROMPT_TIMEOUT" answer || answer=""
    answer=$(printf '%s' "$answer" | tr -d ' ' | tr '[:upper:]' '[:lower:]')

    case "$answer" in
        1|minimal)  PROFILE="minimal" ;;
        2|standard) PROFILE="standard" ;;
        3|full)     PROFILE="full" ;;
        "")         PROFILE="$RECOMMENDED_PROFILE" ;;
        *)
            warn "Unrecognised answer '$answer' — using $RECOMMENDED_PROFILE."
            PROFILE="$RECOMMENDED_PROFILE"
            ;;
    esac

    if [ "$PROFILE" != "minimal" ] && [ "$RECOMMENDED_PROFILE" = "minimal" ]; then
        warn "Overriding the Minimal recommendation on a constrained host."
        warn "Docker may fail to start, or deploys may be OOM-killed."
    fi

    good "Install profile: $PROFILE"
    return 0
}

ensure_swap() {
    # No `free` (some LXC templates) → cannot gauge swap; skip quietly. (I14)
    if ! command -v free &>/dev/null; then
        warn "'free' not found — skipping the swap check."
        return 0
    fi
    # The swapfile path and /proc/swaps are overridable so the unit tests can
    # exercise this against fixtures (same pattern as SERVERKIT_NGINX_DIR).
    local swapfile="${SERVERKIT_SWAPFILE:-/swapfile}"
    local proc_swaps="${SERVERKIT_PROC_SWAPS:-/proc/swaps}"
    local swap
    swap=$(free -m 2>/dev/null | awk '/^Swap:/ {print $2}') || swap=""
    [ -n "$swap" ] || return 0
    if [ "$swap" -lt 512 ]; then
        step "Adding 1GB of swap..."
        # Every arm is guarded: fallocate is unsupported on some filesystems,
        # dd dies on a full disk, and swapon fails outright on btrfs/zfs and
        # in most containers. The old body claimed "Swap active." no matter
        # what and the Vite build then OOM'd — verify via swapon --show /
        # /proc/swaps before saying so, and degrade to a warning when no swap
        # could actually be brought up. (I19)
        if [ ! -f "$swapfile" ]; then
            if fallocate -l 1G "$swapfile" 2>/dev/null || \
               dd if=/dev/zero of="$swapfile" bs=1M count=1024 status=none 2>/dev/null; then
                chmod 600 "$swapfile" 2>/dev/null || true
                mkswap "$swapfile" >/dev/null 2>&1 || true
            else
                # A dd that died on a full disk leaves a partial file behind.
                rm -f "$swapfile" 2>/dev/null || true
            fi
        fi
        swapon "$swapfile" 2>/dev/null || true
        if swapon --show 2>/dev/null | grep -q "$swapfile" || \
           grep -qs "$swapfile" "$proc_swaps"; then
            good "Swap active."
        else
            warn "Could not activate swap (full disk, unsupported filesystem, or container limits)."
            warn "Continuing without it — a low-RAM box may need swap for the frontend build."
        fi
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Python 3.11–3.14 — detect a usable interpreter or build one
# ---------------------------------------------------------------------------
ver_in_range() {
    # true when $1 is >= PYTHON_MIN and <= PYTHON_MAX
    printf '%s\n%s' "$PYTHON_MIN" "$1" | sort -C -V && \
    printf '%s\n%s' "$1" "$PYTHON_MAX" | sort -C -V
}

# True when <python> can actually create virtual environments. Debian/Ubuntu
# minimal images split the venv module (ensurepip) into pythonX.Y-venv, so an
# otherwise-valid interpreter still dies much later at `python -m venv` with
# "ensurepip is not available" — probe up front instead. (I8)
# NB: `-m venv --help` alone is NOT enough — Debian's python answers --help
# happily without ensurepip installed, and only fails at creation time. The
# ensurepip import is the real check. (Found via the Test Sandbox full mode
# on the geerlingguy debian12 image.)
py_venv_ok() {
    "$1" -m venv --help >/dev/null 2>&1 && \
        "$1" -c 'import ensurepip' >/dev/null 2>&1
}

locate_python() {
    # Prefer an explicit minor version, newest first, then bare python3. This
    # mirrors scripts/update.sh so a box that has python3.11 (Debian 12) but a
    # too-new/too-old default python3 is still recognized. A candidate must
    # also be venv-capable (py_venv_ok); on Debian-family boxes the missing
    # pythonX.Y-venv package is installed on demand before the candidate is
    # given up on. Sets PYTHON_BIN and returns 0 on success; returns 1
    # (without aborting) when nothing fits.
    local c v
    for c in python3.14 python3.13 python3.12 python3.11 python3; do
        if command -v "$c" &>/dev/null; then
            v=$("$c" -c 'import sys;print(".".join(map(str,sys.version_info[:2])))' 2>/dev/null || true)
            if [ -n "$v" ] && ver_in_range "$v"; then
                if ! py_venv_ok "$c" && [ "$OS_FAMILY" = "debian" ]; then
                    step "Installing python${v}-venv (the venv module is missing)..."
                    pkg_add "python${v}-venv"
                    py_venv_ok "$c" || pkg_add python3-venv
                fi
                if ! py_venv_ok "$c"; then
                    warn "$c (Python $v) cannot create virtualenvs (no venv/ensurepip) — skipping it."
                    continue
                fi
                PYTHON_BIN="$c"
                good "Using $c (Python $v)"
                return 0
            fi
        fi
    done
    PYTHON_BIN=""
    return 1
}

build_python_from_source() {
    local ver="3.12.8" src="/tmp/Python-3.12.8"

    # Every step is checked and the real error is shown. The old version piped
    # each command to `tail -1` and ignored the status, so a failed configure
    # looked identical to a successful one and the install died much later with
    # "Could not install a supported Python" and no reason (issue #99).
    #
    # No --enable-optimizations: PGO triples-to-quadruples the build (20+ min on
    # a small VPS) to buy runtime speed a control panel never notices. This is a
    # last-resort path; finishing matters more than a faster interpreter.
    (
        set -e
        cd /tmp
        step "Downloading Python $ver source..."
        wget -q "https://www.python.org/ftp/python/$ver/Python-$ver.tgz"
        tar xzf "Python-$ver.tgz"
        cd "Python-$ver"
        step "Configuring Python $ver (this takes a few minutes)..."
        ./configure --prefix=/usr/local > /tmp/python-build.log 2>&1
        step "Compiling Python $ver..."
        make -j"$(nproc)" >> /tmp/python-build.log 2>&1
        make altinstall >> /tmp/python-build.log 2>&1
    )
    local rc=$?
    rm -rf "$src" "/tmp/Python-$ver.tgz"

    if [ "$rc" != "0" ]; then
        warn "Building Python $ver failed. Last 20 lines:"
        tail -20 /tmp/python-build.log 2>/dev/null | sed 's/^/    /' >&2
        warn "Full log: /tmp/python-build.log"
        return 1
    fi

    # A source build silently omits modules whose headers were missing at
    # configure time. No ssl means every HTTPS fetch dies; no sqlite3 means the
    # database never opens — both far from here and impossible to connect back.
    local built="/usr/local/bin/python${ver%.*}"
    if [ -x "$built" ]; then
        local missing=""
        for mod in ssl sqlite3 ctypes; do
            "$built" -c "import $mod" >/dev/null 2>&1 || missing="$missing $mod"
        done
        if [ -n "$missing" ]; then
            warn "The Python we built is missing stdlib modules:$missing"
            warn "Install the matching -dev packages and re-run the installer."
            return 1
        fi
    fi
    return 0
}

python_build_ready() {
    command -v make >/dev/null 2>&1 || return 1
    # Compile against THIS interpreter's headers, including pyconfig.h. Merely
    # finding a Python executable or a different version's -dev package is not
    # enough for pip's native dependencies (issue #148).
    "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import pathlib
import shlex
import subprocess
import sysconfig
import tempfile

with tempfile.TemporaryDirectory(prefix='serverkit-python-build-') as work:
    source = pathlib.Path(work) / 'probe.c'
    source.write_text('#include <Python.h>\nint main(void) { return 0; }\n')
    includes = {sysconfig.get_path('include'), sysconfig.get_path('platinclude')}
    command = shlex.split(sysconfig.get_config_var('CC') or 'cc')
    command += ['-I' + path for path in includes if path]
    command += ['-c', str(source), '-o', str(pathlib.Path(work) / 'probe.o')]
    subprocess.run(command, check=True)
PY
}

ensure_python_build_deps() {
    python_build_ready && return 0
    local v
    v=$("$PYTHON_BIN" -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')
    step "Installing build tools and headers for Python $v..."
    refresh_pkg_index
    case "$OS_FAMILY" in
        debian) pkg_add build-essential "python${v}-dev" ;;
        fedora|rhel) pkg_add gcc make "python${v}-devel" ;;
        suse) pkg_add gcc make "python${v//./}-devel" ;;
        arch) pkg_add base-devel python ;;
        alpine) pkg_add build-base python3-dev ;;
        gentoo) pkg_add sys-devel/gcc sys-devel/make "dev-lang/python:${v}" ;;
    esac
    # pkg_add deliberately warns and continues. Probe again so failure is
    # reported here, rather than much later as an opaque pip build failure.
    if ! python_build_ready; then
        halt "Native Python dependencies cannot be built with $PYTHON_BIN (Python $v).
       Install a C compiler, make, and the matching Python development headers, then re-run.
       On Debian/Ubuntu: apt-get install build-essential python${v}-dev"
        return 1
    fi
}

provision_python() {
    phase "Installing Python"

    # Existing interpreters may have venv support but no development headers.
    if locate_python; then
        ensure_python_build_deps
        return $?
    fi

    warn "No supported Python ($PYTHON_MIN–$PYTHON_MAX) found — installing one."

    # Prefer the distro's own package over a source compile. Debian 12 ships
    # python3.11; Ubuntu 24.04 ships python3.12 (older Ubuntu falls back to the
    # deadsnakes PPA); Fedora/RHEL provide 3.12 or 3.11 in their repos. A slow,
    # fragile source build is now strictly the last resort. pkg_add is
    # warn-and-continue (always returns 0), so each attempt is followed by a
    # state probe (command -v) to decide whether the next fallback is needed.
    if [ "$OS_FAMILY" = "debian" ]; then
        if [ "${ID:-}" = "ubuntu" ]; then
            refresh_pkg_index
            pkg_add python3 python3-venv python3-dev
            if locate_python; then
                ensure_python_build_deps
                return $?
            fi
            pkg_add python3.12 python3.12-venv python3.12-dev
            if ! command -v python3.12 &>/dev/null; then
                step "Adding deadsnakes PPA for Python 3.12..."
                pkg_add software-properties-common
                add-apt-repository -y ppa:deadsnakes/ppa || true
                refresh_pkg_index
                pkg_add python3.12 python3.12-venv python3.12-dev
            fi
        else
            # Debian (and Debian-like). The default `python3` tracks the
            # release — bookworm 3.11, trixie 3.13 — so ask for it FIRST rather
            # than naming a version. Trixie carries no python3.11 or python3.12
            # at all, so the old "install python3.11, else python3.12" pair
            # could only ever fail there (issue #99), and hardcoding 3.13
            # instead would just move the same trap to the next release.
            # The explicit fallbacks below cover releases whose default is out
            # of range. locate_python (not `command -v`) is the probe, because
            # a binary existing says nothing about it being in range or
            # venv-capable.
            refresh_pkg_index
            pkg_add python3 python3-venv python3-dev
            locate_python >/dev/null 2>&1 || \
                pkg_add python3.13 python3.13-venv python3.13-dev
            locate_python >/dev/null 2>&1 || \
                pkg_add python3.12 python3.12-venv python3.12-dev
            locate_python >/dev/null 2>&1 || \
                pkg_add python3.11 python3.11-venv python3.11-dev
        fi
    elif [ "$OS_FAMILY" = "fedora" ] || [ "$OS_FAMILY" = "rhel" ]; then
        pkg_add python3.12 python3.12-devel
        command -v python3.12 &>/dev/null || pkg_add python3.11 python3.11-devel
    elif [ "$OS_FAMILY" = "suse" ]; then
        pkg_add python311 python311-devel
        command -v python3.11 &>/dev/null || pkg_add python312 python312-devel
    elif [ "$OS_FAMILY" = "arch" ]; then
        # Rolling release — `python` tracks the newest upstream line the gate
        # covers (3.14 as of the 2026-08 baseline). Arch carries no versioned
        # python3.1x packages, so there is nothing to fall back to here; if the
        # rolling package ever outruns the gate, locate_python below rejects it
        # and the source-build path takes over.
        pkg_add python
    elif [ "$OS_FAMILY" = "alpine" ]; then
        pkg_add python3 python3-dev
    elif [ "$OS_FAMILY" = "gentoo" ]; then
        # Portage carries every supported line as a parallel slot.
        pkg_add dev-lang/python:3.13
        command -v python3.13 &>/dev/null || pkg_add dev-lang/python:3.12
    fi

    # Did a distro package give us something usable?
    if locate_python; then
        good "Python ready: $PYTHON_BIN"
        ensure_python_build_deps
        return $?
    fi

    # Last resort: compile from source.
    warn "Distro packages did not provide a supported Python — building from source."
    # A COMPILER, first. This list was only the -dev headers: nothing anywhere
    # in the installer ever installed gcc or make, so `./configure` could not
    # run on any clean VPS and this whole fallback was decorative (issue #99).
    if [ "$OS_FAMILY" = "debian" ]; then
        pkg_add build-essential wget zlib1g-dev libbz2-dev libreadline-dev \
            libsqlite3-dev libncurses5-dev libncursesw5-dev \
            xz-utils tk-dev liblzma-dev libffi-dev libssl-dev
    elif [ "$OS_FAMILY" = "suse" ]; then
        pkg_add gcc gcc-c++ make wget zlib-devel libbz2-devel readline-devel \
            sqlite3-devel ncurses-devel xz-devel tk-devel libffi-devel \
            libopenssl-devel
    elif [ "$OS_FAMILY" = "gentoo" ]; then
        pkg_add sys-devel/gcc sys-devel/make net-misc/wget sys-libs/zlib \
            dev-libs/bzip2 sys-libs/readline dev-db/sqlite sys-libs/ncurses \
            dev-libs/libffi dev-libs/openssl
    else
        pkg_add gcc gcc-c++ make wget zlib-devel bzip2-devel readline-devel \
            sqlite-devel ncurses-devel xz-devel tk-devel libffi-devel \
            openssl-devel
    fi

    if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1; then
        halt "No C compiler available and no packaged Python $PYTHON_MIN-$PYTHON_MAX on ${ID:-this distro}.
       Install one by hand, then re-run:  apt-get install build-essential   (or: dnf group install \"Development Tools\")"
    fi

    if ! build_python_from_source; then
        halt "Could not build Python from source — see the log above.
       Install Python $PYTHON_MIN-$PYTHON_MAX by hand and re-run the installer."
    fi
    PYTHON_BIN="python3.12"

    command -v "$PYTHON_BIN" &>/dev/null || \
        halt "Could not install a supported Python — install Python $PYTHON_MIN-$PYTHON_MAX by hand."
    good "Python installed ($PYTHON_BIN)."
    ensure_python_build_deps
}

# ---------------------------------------------------------------------------
# Docker engine + compose plugin
# ---------------------------------------------------------------------------
# Add Docker's upstream .repo file, handling both config-manager generations:
# classic dnf4 uses `--add-repo URL`, while dnf5 (Fedora 41+) removed that
# flag in favour of `addrepo --from-repofile=URL`. Warn-and-continue: when
# neither works the docker-ce install below fails too (also non-fatally). (I10)
docker_repo_add() {
    local url="$1"
    if dnf config-manager --add-repo "$url" >/dev/null 2>&1; then
        return 0
    fi
    if dnf config-manager addrepo --from-repofile="$url" >/dev/null 2>&1; then
        return 0
    fi
    warn "Could not add the Docker repository ($url)."
    return 0
}

provision_docker() {
    phase "Docker"

    # The Minimal profile deliberately ships without Docker. The panel detects
    # its absence at runtime and hides app hosting; adding Docker later from
    # Settings promotes the install to Standard.
    if [ "$PROFILE" = "minimal" ]; then
        good "Skipping Docker (minimal profile) — add it later from the panel."
        return
    fi

    if command -v docker &>/dev/null; then
        good "Docker already present: $(docker --version | head -1)"
        return
    fi

    step "Installing Docker..."
    case "$OS_FAMILY" in
        fedora)
            pkg_add dnf-plugins-core
            docker_repo_add https://download.docker.com/linux/fedora/docker-ce.repo
            pkg_add docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin
            ;;
        rhel)
            pkg_add dnf-plugins-core
            docker_repo_add https://download.docker.com/linux/rhel/docker-ce.repo
            pkg_add docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin
            ;;
        suse)
            pkg_add docker docker-compose
            ;;
        arch)
            pkg_add docker docker-compose
            ;;
        alpine)
            pkg_add docker docker-cli-compose
            ;;
        *)
            # Docker's convenience script (the default Debian/Ubuntu path).
            # Stage it to a temp file with retries instead of piping curl
            # straight into sh — a connection dropped mid-download would
            # otherwise execute half a script. Warn-and-continue on failure,
            # falling back to the distro package (docker.io on Debian). (I13)
            local dget drc=0
            dget="$(mktemp 2>/dev/null)" || dget="/tmp/serverkit-get-docker.sh"
            if curl -fsSL --retry 3 https://get.docker.com -o "$dget"; then
                sh "$dget" || drc=$?
            else
                drc=1
            fi
            rm -f "$dget" 2>/dev/null || true
            if [ "$drc" -ne 0 ]; then
                warn "The get.docker.com install script failed — trying the distro package instead."
                pkg_add docker.io docker-compose-v2
            fi
            ;;
    esac

    # Enable + start across init systems (systemd on most families, OpenRC on
    # Alpine). Guarded so a non-systemd box doesn't abort the install here.
    if command -v systemctl &>/dev/null && [ -d /run/systemd/system ]; then
        systemctl enable docker 2>/dev/null || true
        systemctl start docker 2>/dev/null || true
    elif command -v rc-update &>/dev/null; then
        rc-update add docker default 2>/dev/null || true
        rc-service docker start 2>/dev/null || true
    fi
    # Don't claim success the old unconditional way — probe. Docker stays
    # best-effort here (managed app deploys need it, the panel itself runs
    # without it), so a miss is a loud warning, not a halt. (I13)
    if command -v docker &>/dev/null; then
        good "Docker installed."
    else
        warn "Docker could not be installed — managed app deployments need it."
        warn "Install it manually (https://docs.docker.com/engine/install/) when you need containers."
    fi
}

ensure_compose_plugin() {
    # No Docker on the minimal profile, so nothing to plug into.
    if [ "$PROFILE" = "minimal" ]; then
        return
    fi
    if docker compose version &>/dev/null; then
        good "Docker Compose plugin present."
        return
    fi
    step "Installing Docker Compose plugin..."
    # Fallback chain (I9): when Docker came from the distro repo (docker.io),
    # Docker's own repo was never configured, so 'docker-compose-plugin' does
    # not exist there — Ubuntu ships the same plugin as 'docker-compose-v2'.
    # Probe after each attempt; if neither lands, warn and continue (the box
    # may have compose v1, or the operator can add it later).
    pkg_add docker-compose-plugin
    if ! docker compose version &>/dev/null; then
        pkg_add docker-compose-v2
    fi
    if docker compose version &>/dev/null; then
        good "Docker Compose plugin installed."
    else
        warn "Could not install the Docker Compose plugin — 'docker compose' is unavailable."
        warn "Managed app deployments need it; install docker-compose-plugin or docker-compose-v2 manually."
    fi
}

# ---------------------------------------------------------------------------
# Node.js 20 (only needed for source builds — releases ship a built frontend)
# ---------------------------------------------------------------------------
# Node 18+ is the floor for the Vite frontend build. Distro nodejs on every
# currently-supported target (Ubuntu 24.04, Debian 12, Fedora 40+) already meets
# it, so we install the distro package first and only pipe NodeSource into bash
# when the distro can't deliver a new-enough Node + npm.
node_major()  { node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'; }
# Vite 8 / rolldown (the frontend bundler) require Node ^20.19.0 || >=22.12.0.
# On anything older, npm silently skips rolldown's native binary and
# `npm run build` dies with a cryptic MODULE_NOT_FOUND — so gate on the real floor.
node_ready()  {
    command -v node &>/dev/null && command -v npm &>/dev/null || return 1
    local v M m
    v="$(node --version 2>/dev/null | sed -E 's/^v//')"
    M="${v%%.*}"; m="${v#*.}"; m="${m%%.*}"
    case "$M" in ''|*[!0-9]*) return 1;; esac
    case "$m" in ''|*[!0-9]*) return 1;; esac
    if [ "$M" -eq 20 ] && [ "$m" -ge 19 ]; then return 0; fi
    if [ "$M" -eq 22 ] && [ "$m" -ge 12 ]; then return 0; fi
    if [ "$M" -ge 23 ]; then return 0; fi
    return 1
}

provision_node() {
    phase "Node.js"

    if [ "$INSTALL_FROM_RELEASE" = "1" ]; then
        good "Skipping Node.js — the release ships a pre-built frontend."
        return
    fi

    if node_ready; then
        good "Node.js already present: $(node --version)"
        return
    fi

    step "Installing Node.js (distro repo first)..."
    # Debian/Ubuntu split npm into its own package; Fedora bundles it.
    pkg_add nodejs npm 2>/dev/null || pkg_add nodejs 2>/dev/null || true
    command -v npm &>/dev/null || pkg_add npm 2>/dev/null || true

    if ! node_ready; then
        warn "Distro Node.js is missing or too old to build the frontend (need 20.19+ or 22.12+) — falling back to NodeSource 22 LTS."
        if [ "$OS_FAMILY" = "debian" ]; then
            curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1 || true
        else
            curl -fsSL https://rpm.nodesource.com/setup_22.x | bash - >/dev/null 2>&1 || true
        fi
        pkg_add nodejs || true
    fi

    node_ready || \
        halt "Node.js 20.19+ or 22.12+ (with npm) is required to build the frontend but could not be installed. Install Node 22 LTS and re-run."
    good "Node.js $(node --version) ready."
}

# ---------------------------------------------------------------------------
# Live-state carry: keep .env + backend/instance/ across a slot rewrite
# ---------------------------------------------------------------------------
# fetch_release and sync_source both rebuild $FIRST_SLOT with an rm -rf. On a
# re-run over an existing install that tree holds irreplaceable state: the
# generated .env (SECRET_KEY / JWT_SECRET_KEY / SERVERKIT_ENCRYPTION_KEY —
# losing that key orphans every encrypted secret) and backend/instance/ (the
# SQLite database). Copy them aside before the rewrite and restore them after,
# mirroring scripts/update.sh's deploy_source carry-forward. Both helpers are
# best-effort and always return 0 — preservation must never abort an install.
stash_live_state() {
    # Echoes a stash directory holding the live state, or nothing when there
    # is nothing to preserve (fresh install) or no stash could be created.
    local src="$1" stash
    if [ ! -f "$src/.env" ] && [ ! -d "$src/backend/instance" ]; then
        return 0
    fi
    stash="$(mktemp -d 2>/dev/null || true)"
    [ -n "$stash" ] || return 0
    cp -a "$src/.env" "$stash/.env" 2>/dev/null || true
    cp -a "$src/backend/instance" "$stash/instance" 2>/dev/null || true
    printf '%s' "$stash"
    return 0
}

restore_live_state() {
    local stash="$1" dest="$2"
    [ -n "$stash" ] || return 0
    if [ -f "$stash/.env" ]; then
        cp -a "$stash/.env" "$dest/.env" 2>/dev/null || true
    fi
    if [ -d "$stash/instance" ]; then
        mkdir -p "$dest/backend" 2>/dev/null || true
        rm -rf "$dest/backend/instance" 2>/dev/null || true
        cp -a "$stash/instance" "$dest/backend/instance" 2>/dev/null || true
    fi
    rm -rf "$stash" 2>/dev/null || true
    good "Preserved the existing .env and database across the rewrite."
    return 0
}

# ---------------------------------------------------------------------------
# Release tarball path
# ---------------------------------------------------------------------------
resolve_release_tag() {
    if [ -n "$SERVERKIT_VERSION" ]; then
        printf '%s' "$SERVERKIT_VERSION"
        return
    fi
    # Primary: the releases/latest redirect. Its Location header carries the
    # tag and — unlike api.github.com, which allows only 60 requests/hour per
    # IP and is routinely exhausted behind shared cloud NAT — it has no API
    # quota. The API lookup stays as the fallback. Both arms are guarded so
    # an unresolvable tag comes back as EMPTY output (the caller warns loudly)
    # instead of a set -e abort. (I15)
    local tag=""
    tag=$(curl -sfLI -o /dev/null -w '%{url_effective}' \
            "https://github.com/${GITHUB_REPO}/releases/latest" 2>/dev/null \
        | sed -n 's|.*/releases/tag/||p') || tag=""
    if [ -n "$tag" ]; then
        printf '%s' "$tag"
        return 0
    fi
    curl -sf "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" 2>/dev/null \
        | grep '"tag_name"' | head -1 | cut -d'"' -f4 || true
}

fetch_release() {
    phase "Downloading Pre-built Release"

    local tag tarball tmp_dir unpacked

    if [ -n "$SERVERKIT_OFFLINE_TARBALL" ]; then
        [ -f "$SERVERKIT_OFFLINE_TARBALL" ] || halt "Offline tarball not found: $SERVERKIT_OFFLINE_TARBALL"
        tag="offline"
        tarball="$SERVERKIT_OFFLINE_TARBALL"
        good "Using offline tarball: $tarball"
    else
        tag=$(resolve_release_tag) || tag=""
        if [ -z "$tag" ]; then
            # Loud on purpose: this used to be a silent downgrade that turned
            # a 2-minute release install into a full compile. (I15)
            warn "Could not resolve the latest release tag (GitHub unreachable or API rate-limited)."
            warn "FALLING BACK TO A FULL SOURCE BUILD — much slower, and it compiles the frontend here."
            warn "To install a release instead, pin one: SERVERKIT_VERSION=vX.Y.Z"
            INSTALL_FROM_RELEASE=0
            return 1
        fi
        good "Latest release: $tag"

        local base
        if [ -n "$SERVERKIT_MIRROR_URL" ]; then
            base="$SERVERKIT_MIRROR_URL"
        else
            base="https://github.com/${GITHUB_REPO}/releases/download/${tag}"
        fi

        tarball="/tmp/serverkit-${tag}-linux-${DL_ARCH}.tar.gz"
        step "Fetching release tarball (${DL_ARCH})..."
        if ! curl -sfL "${base}/serverkit-${tag}-linux-${DL_ARCH}.tar.gz" -o "$tarball"; then
            warn "No release tarball for this platform — falling back to source."
            INSTALL_FROM_RELEASE=0
            return 1
        fi

        step "Verifying checksum..."
        if curl -sfL "${base}/checksums.txt" -o "/tmp/serverkit-checksums-${tag}.txt"; then
            if ! (cd /tmp && sha256sum -c <(grep "serverkit-${tag}-linux-${DL_ARCH}.tar.gz" "serverkit-checksums-${tag}.txt") >/dev/null 2>&1); then
                halt "Checksum verification failed for release tarball."
            fi
            good "Checksum verified"
        else
            warn "Could not download checksums.txt — skipping verification"
        fi
    fi

    step "Unpacking release..."
    tmp_dir="$(mktemp -d)"
    # A failed unpack (corrupt download, disk full) must take the clean
    # source fallback, not "succeed" into a half-written tree. (I16)
    if ! tar xzf "$tarball" -C "$tmp_dir"; then
        warn "Could not unpack the release tarball (corrupt download or disk full) — falling back to source."
        rm -rf "$tmp_dir"
        INSTALL_FROM_RELEASE=0
        return 1
    fi

    unpacked="$tmp_dir/serverkit"
    [ ! -d "$unpacked" ] && unpacked="$tmp_dir/opt/serverkit"
    if [ ! -d "$unpacked" ]; then
        unpacked="$(find "$tmp_dir" -maxdepth 2 -type d -name serverkit | head -n1)"
    fi
    [ -d "$unpacked" ] || halt "Release tarball layout is unrecognized (expected serverkit/ or opt/serverkit/)."

    ensure_install_layout

    # A re-run over a live install must never destroy its secrets or database
    # (the old bare rm -rf below did exactly that). Stash them, rewrite,
    # restore. (I4)
    local live_dir stash
    live_dir="$(readlink -f "$INSTALL_DIR" 2>/dev/null || echo "$FIRST_SLOT")"
    [ -d "$live_dir" ] || live_dir="$FIRST_SLOT"
    stash="$(stash_live_state "$live_dir")"

    rm -rf "$FIRST_SLOT"
    # Disk full mid-copy: a half-written slot must not masquerade as a
    # success. Put the live state back and hand control to the caller's
    # source fallback. (I16)
    if ! cp -a "$unpacked" "$FIRST_SLOT"; then
        warn "Could not copy the release into $FIRST_SLOT (disk full?) — falling back to source."
        rm -rf "$tmp_dir"
        mkdir -p "$FIRST_SLOT" 2>/dev/null || true
        restore_live_state "$stash" "$FIRST_SLOT"
        INSTALL_FROM_RELEASE=0
        return 1
    fi
    rm -rf "$tmp_dir"
    restore_live_state "$stash" "$FIRST_SLOT"

    [ -L "$INSTALL_DIR" ] || ln -s "$FIRST_SLOT" "$INSTALL_DIR"

    chmod +x "$INSTALL_DIR/serverkit"
    chmod +x "$INSTALL_DIR/scripts/"*.sh 2>/dev/null || true
    good "Release unpacked into $FIRST_SLOT"
}

# ---------------------------------------------------------------------------
# Source path: fetch / refresh the repository
# ---------------------------------------------------------------------------
sync_source() {
    phase "Source Code"

    # Source installs clone with git, but minimal images don't guarantee it
    # and the preflight never checked — install it on demand, and halt only
    # if that fails too (a source install cannot proceed without git). (I7)
    if ! command -v git &>/dev/null; then
        step "Installing git..."
        # Cleaned-down images (Docker base layers, geerlingguy systemd
        # containers) ship with EMPTY apt indexes — without a refresh,
        # `apt-get install git` finds no candidate. refresh_pkg_index is
        # best-effort and a no-op-costly-but-harmless on populated images.
        refresh_pkg_index
        pkg_add git
        command -v git &>/dev/null || \
            halt "git is required for a source install but could not be installed."
    fi

    ensure_install_layout

    if [ -d "$FIRST_SLOT/.git" ]; then
        step "Refreshing the existing checkout..."
        cd "$FIRST_SLOT"
        if ! git pull --ff-only; then
            warn "Fast-forward failed (local edits?) — hard-resetting to origin/main."
            git fetch origin
            git reset --hard origin/main
        fi
    else
        step "Cloning ServerKit..."
        # Same live-state carry as fetch_release: the non-git tree replaced
        # here may still be a live install (e.g. one installed from a release
        # tarball) holding the generated .env and the database. (I4)
        local stash
        stash="$(stash_live_state "$FIRST_SLOT")"
        rm -rf "$FIRST_SLOT"
        git clone --depth 1 "https://github.com/${GITHUB_REPO}.git" "$FIRST_SLOT"
        restore_live_state "$stash" "$FIRST_SLOT"
        cd "$FIRST_SLOT"
    fi
    [ -L "$INSTALL_DIR" ] || ln -sfn "$FIRST_SLOT" "$INSTALL_DIR"
    good "Repository ready."
}

# ---------------------------------------------------------------------------
# On-disk layout (blue/green slots + symlink)
# ---------------------------------------------------------------------------
ensure_install_layout() {
    # Migrate legacy real-directory installs into the blue/green layout.
    if [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ]; then
        step "Migrating to blue/green install layout..."
        # A stale slot left by an aborted earlier run would make `mv` NEST
        # the live tree inside it ($FIRST_SLOT/serverkit). The live install
        # is $INSTALL_DIR (a real dir here, so nothing links to the slot) —
        # clear the stale slot before moving. (I18)
        if [ -e "$FIRST_SLOT" ]; then
            warn "Removing a stale $FIRST_SLOT left by an earlier run."
            rm -rf "$FIRST_SLOT"
        fi
        mv "$INSTALL_DIR" "$FIRST_SLOT"
        ln -s "$FIRST_SLOT" "$INSTALL_DIR"
        good "Active install is now $INSTALL_DIR → $FIRST_SLOT"
    fi

    mkdir -p "$DIR_A" "$DIR_B"
    [ -L "$INSTALL_DIR" ] || ln -s "$FIRST_SLOT" "$INSTALL_DIR"
}

make_directories() {
    phase "Creating Directories"

    ensure_install_layout

    local d
    for d in "$LOG_DIR" "$DATA_DIR" "$BACKUP_DIR" "$CONFIG_DIR" "$INSTALL_DIR/backend/instance"; do
        mkdir -p -m 0750 "$d"
    done
    for d in "$INSTALL_DIR/nginx/ssl" /etc/serverkit/templates /var/serverkit/apps \
             /var/www/acme /etc/nginx/serverkit-locations; do
        mkdir -p -m 0755 "$d"
    done

    good "Directories created."
}

# ---------------------------------------------------------------------------
# Python virtual environment + dependencies
# ---------------------------------------------------------------------------
build_virtualenv() {
    phase "Python Environment"

    # If the release shipped a pre-built venv at the expected path, use it —
    # but only if its interpreter actually runs on this host. The release
    # tarball is built on Ubuntu 24.04, so its venv python hard-requires
    # GLIBC_2.38 and dies on Ubuntu 22.04 / Debian 11 / older. In that case
    # discard it and fall through to a locally-built venv.
    if [ "$INSTALL_FROM_RELEASE" = "1" ] && [ -f "$FIRST_SLOT/venv/bin/activate" ] && [ -x "$FIRST_SLOT/venv/bin/python" ]; then
        if ! "$FIRST_SLOT/venv/bin/python" -c '' >/dev/null 2>&1; then
            warn "Release venv's python does not run on this host (too-new glibc build) — building a fresh venv instead."
            rm -rf "$FIRST_SLOT/venv"
        else
        step "Using pre-built virtual environment from release..."
        # In the default layout $INSTALL_DIR is a symlink to $FIRST_SLOT, so
        # $VENV_DIR *is* $FIRST_SLOT/venv — the rm -rf below would delete the
        # very venv it is about to "copy", and the cp would then fail. When
        # source and destination resolve to the same directory the venv is
        # already in place; verify and keep it. (I3)
        local src_venv dst_venv
        src_venv="$(readlink -f "$FIRST_SLOT/venv" 2>/dev/null || echo "$FIRST_SLOT/venv")"
        dst_venv="$(readlink -f "$VENV_DIR" 2>/dev/null || echo "$VENV_DIR")"
        if [ "$src_venv" = "$dst_venv" ]; then
            good "Virtual environment already in place from the release."
            return
        fi
        rm -rf "$VENV_DIR"
        cp -a "$FIRST_SLOT/venv" "$VENV_DIR"
        good "Virtual environment installed from release."
        return
        fi
    fi

    step "Creating the virtual environment..."
    $PYTHON_BIN -m venv "$VENV_DIR"
    if [ ! -f "$VENV_DIR/bin/activate" ]; then
        halt "Virtual environment creation failed: $VENV_DIR/bin/activate not found."
    fi
    source "$VENV_DIR/bin/activate"

    step "Installing Python dependencies..."
    pip install --upgrade pip --quiet
    if [ "$SAFE_MODE" = true ]; then
        pip install --no-cache-dir -r "$INSTALL_DIR/backend/requirements.txt" --quiet
    else
        pip install -r "$INSTALL_DIR/backend/requirements.txt" --quiet
    fi
    pip install gunicorn gevent gevent-websocket --quiet

    good "Python environment ready."
}

# ---------------------------------------------------------------------------
# Application configuration and generated secrets
# ---------------------------------------------------------------------------
# Where gunicorn listens. Loopback by default — host nginx fronts the panel on
# :80/:443, so the raw gunicorn port must not be world-reachable. Operators who
# front it differently override with SERVERKIT_BIND_HOST=0.0.0.0.
#
# One definition, two consumers: it renders the systemd unit AND decides whether
# X-Forwarded-For is trustworthy. Those answers must never disagree.
backend_bind_host() {
    printf '%s' "${SERVERKIT_BIND_HOST:-127.0.0.1}"
}

# True when the only route to Flask is through our own nginx.
#
# This is the whole security condition behind TRUST_PROXY_HEADERS. ProxyFix
# takes X-Forwarded-For at face value, which is safe exactly when a proxy we
# control is guaranteed to have written it — i.e. when nothing can open a socket
# to gunicorn directly. Loopback binding is what guarantees that; the installer
# firewall also never opens the backend port (SSH + 80/443 + PANEL_PORT only).
backend_is_loopback_only() {
    case "$(backend_bind_host)" in
        127.0.0.1|localhost|::1|'[::1]') return 0 ;;
        *) return 1 ;;
    esac
}

# Set KEY=value in an env file, replacing any existing definition (commented or
# not). Rewrites the file instead of `sed -i s|...|`: values here are URLs, and
# an unescaped '/', '&' or '|' in a sed replacement is syntax, not data.
# Fill a gap without ever overriding a choice. An uncommented definition is the
# operator's — someone may have turned trust off deliberately, and a re-run
# silently turning it back on would be the worst kind of surprise.
_env_default_key() {
    local file="$1" key="$2" value="$3"
    grep -qE "^[[:space:]]*${key}=" "$file" && return 0
    _env_set_key "$file" "$key" "$value"
}

_env_set_key() {
    local file="$1" key="$2" value="$3" tmp
    # Beside the target, not in /tmp: this file carries SECRET_KEY and the
    # encryption key, and $INSTALL_DIR is root-owned while /tmp is world-listable.
    tmp="$(mktemp "${file}.XXXXXX")"
    grep -v -E "^[[:space:]]*#?[[:space:]]*${key}=" "$file" > "$tmp" || true
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    # cat rather than mv so the original mode (0600) and owner survive.
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

write_config() {
    phase "Configuration"

    if [ -f "$INSTALL_DIR/.env" ]; then
        warn ".env already exists — leaving the current configuration in place."
        # ...except the SSL mode, which reflects THIS run's outcome (a re-run
        # may have gained or lost HTTPS). Refresh just that key so the
        # panel's HSTS gate matches /etc/serverkit/ssl-mode instead of a
        # stale value from the previous install. (I24)
        if grep -q '^SERVERKIT_SSL_MODE=' "$INSTALL_DIR/.env"; then
            sed -i "s|^SERVERKIT_SSL_MODE=.*|SERVERKIT_SSL_MODE=$SSL_MODE|" "$INSTALL_DIR/.env"
        else
            printf 'SERVERKIT_SSL_MODE=%s\n' "$SSL_MODE" >> "$INSTALL_DIR/.env"
        fi
        # Same reasoning as the SSL mode above: a re-run may have chosen a
        # different profile, and the panel reads this to decide whether to
        # offer app hosting.
        if grep -q '^SERVERKIT_PROFILE=' "$INSTALL_DIR/.env"; then
            sed -i "s|^SERVERKIT_PROFILE=.*|SERVERKIT_PROFILE=$PROFILE|" "$INSTALL_DIR/.env"
        else
            printf 'SERVERKIT_PROFILE=%s\n' "$PROFILE" >> "$INSTALL_DIR/.env"
        fi
        # Converting an existing install to sit behind an external proxy is a
        # re-run with SERVERKIT_EXTERNAL_PROXY=1, and it lands here — the .env
        # already exists. Applying the switch only on a first install would make
        # the documented conversion path silently do nothing. Only ever ADD the
        # trust settings; never remove them on an ordinary re-run, which would
        # quietly break client IPs for someone who set them by hand.
        if [ "$SERVERKIT_EXTERNAL_PROXY" = "1" ]; then
            _env_set_key "$INSTALL_DIR/.env" TRUST_PROXY_HEADERS true
            _env_set_key "$INSTALL_DIR/.env" TRUSTED_PROXY_HOPS 2
            if [ -n "$PANEL_PUBLIC_URL" ]; then
                _env_set_key "$INSTALL_DIR/.env" SERVERKIT_PUBLIC_URL "$PANEL_PUBLIC_URL"
            fi
            good "External-proxy settings applied to the existing .env."
        elif backend_is_loopback_only; then
            # Installs made before the trusted-proxy default existed have no
            # TRUST_PROXY_HEADERS line at all, so they key rate limits, login
            # lockout and audit logs on nginx's own address. Backfill it on a
            # re-run — but only where the line is absent, never over an explicit
            # setting.
            if ! grep -qE '^[[:space:]]*TRUST_PROXY_HEADERS=' "$INSTALL_DIR/.env"; then
                _env_default_key "$INSTALL_DIR/.env" TRUST_PROXY_HEADERS true
                _env_default_key "$INSTALL_DIR/.env" TRUSTED_PROXY_HOPS 1
                good "Enabled trusted-proxy client IPs (behind this install's own nginx)."
            fi
        fi
        return
    fi

    step "Generating configuration..."
    local secret_key jwt_secret encryption_key
    secret_key=$(openssl rand -hex 32)
    jwt_secret=$(openssl rand -hex 32)
    encryption_key=$("${PYTHON_BIN:-python3}" -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')

    # Widen CORS / advertise a public URL when a panel domain is known.
    local public_url="" cors_origins="http://localhost,https://localhost"
    local url_scheme="http"
    if [ -n "$PANEL_DOMAIN" ]; then
        if [ "$SSL_MODE" = "secure" ]; then
            url_scheme="https"
            cors_origins="http://localhost,https://localhost,https://$PANEL_DOMAIN"
        else
            url_scheme="http"
            cors_origins="http://localhost,https://localhost,http://$PANEL_DOMAIN"
        fi
        public_url="${url_scheme}://$PANEL_DOMAIN"
    fi

    # Behind someone else's proxy the browser's origin is the proxy's https URL,
    # which no local signal reveals — SSL_MODE is 'insecure' here precisely
    # because *we* do not terminate TLS. So the operator-supplied value wins,
    # and it must be live (not commented out) or every websocket handshake 400s.
    local proxy_block=""
    if [ -n "$PANEL_PUBLIC_URL" ]; then
        public_url="$PANEL_PUBLIC_URL"
        case ",$cors_origins," in
            *",$public_url,"*) ;;
            *) cors_origins="$cors_origins,$public_url" ;;
        esac
    fi
    # Derived-from-the-domain URLs stay commented (they are a guess, and a wrong
    # SERVERKIT_PUBLIC_URL breaks websockets rather than merely not helping).
    # An operator-supplied one is written live.
    local public_url_line=""
    if [ -n "$PANEL_PUBLIC_URL" ]; then
        public_url_line="SERVERKIT_PUBLIC_URL=$public_url"
    elif [ -n "$public_url" ]; then
        public_url_line="# SERVERKIT_PUBLIC_URL=$public_url"
    fi

    if [ "$SERVERKIT_EXTERNAL_PROXY" = "1" ]; then
        # Two hops: their proxy appends the real client, our nginx appends their
        # proxy. ProxyFix counts from the right, so 1 would log their proxy's IP
        # for every request and 3 would let a client forge its own.
        proxy_block=$(cat <<'PROXYEOF'

# Trusted reverse proxy — set because this install sits behind an external
# TLS-terminating proxy (SERVERKIT_EXTERNAL_PROXY=1). Derives the real client
# IP from X-Forwarded-For for rate limits, login lockout and audit logs.
# TRUSTED_PROXY_HOPS = proxies in front of Flask: external proxy + our nginx.
TRUST_PROXY_HEADERS=true
TRUSTED_PROXY_HOPS=2
PROXYEOF
)
    elif backend_is_loopback_only; then
        proxy_block=$(cat <<'PROXYEOF'

# Trusted reverse proxy — every host install serves the panel through its own
# nginx, and gunicorn binds loopback (see the systemd unit), so X-Forwarded-For
# is written by that nginx and nothing can reach Flask directly to forge it.
# Without this the panel keys per-IP rate limits, the login brute-force throttle
# and every audit-log entry on nginx's own address instead of the real client.
# One hop: our nginx is the only proxy in front. Raise it if you add another
# (e.g. Cloudflare on top ⇒ 2).
TRUST_PROXY_HEADERS=true
TRUSTED_PROXY_HOPS=1
PROXYEOF
)
    else
        # The raw gunicorn port is exposed, so a client can connect without
        # passing through our nginx and X-Forwarded-For becomes attacker-chosen.
        # Trusting it here would let anyone forge the IP that rate limits, login
        # lockout and audit logs key on — strictly worse than logging one wrong
        # address. Left off, and said out loud rather than silently.
        proxy_block=$(cat <<'PROXYEOF'

# Trusted reverse proxy — deliberately OFF. SERVERKIT_BIND_HOST exposes the
# backend port directly, so X-Forwarded-For is client-controlled and cannot be
# trusted. Client IPs will show as the proxy/socket peer. If you do front this
# with a proxy AND block direct access to the backend port, set:
#   TRUST_PROXY_HEADERS=true
#   TRUSTED_PROXY_HOPS=<number of proxies in front of Flask>
TRUST_PROXY_HEADERS=false
PROXYEOF
)
        warn "SERVERKIT_BIND_HOST=${SERVERKIT_BIND_HOST:-} exposes the backend port directly."
        warn "Leaving TRUST_PROXY_HEADERS off: X-Forwarded-For would be client-forgeable."
    fi

    cat > "$INSTALL_DIR/.env" <<EOF
# ServerKit Configuration
# Generated on $(date)

# Security Keys (auto-generated, keep secret!)
SECRET_KEY=$secret_key
JWT_SECRET_KEY=$jwt_secret
SERVERKIT_ENCRYPTION_KEY=$encryption_key

# Database (SQLite by default)
DATABASE_URL=sqlite:///$INSTALL_DIR/backend/instance/serverkit.db

# CORS Origins (comma-separated, add your domain)
CORS_ORIGINS=$cors_origins

# Public URL for agents, browsers and install commands. Behind a reverse proxy
# this must be the URL users actually browse to, or the websocket handshake is
# rejected on origin and the UI stops live-updating.
$public_url_line

# SSL mode (secure|insecure) — gates the panel's HSTS header so HTTPS stays
# optional. Mirrors /etc/serverkit/ssl-mode; set to 'secure' only when this
# server terminates real end-to-end HTTPS.
SERVERKIT_SSL_MODE=$SSL_MODE
$proxy_block

# Install profile (minimal|standard|full) — what this install provisioned, not
# a licence tier. The panel reads it to decide whether to offer app hosting;
# anything skipped can still be installed later from Settings.
SERVERKIT_PROFILE=$PROFILE

# Ports
PORT=80
SSL_PORT=443

# Environment
FLASK_ENV=production
EOF
    chmod 600 "$INSTALL_DIR/.env"
    good "Configuration generated."

    # When a domain is configured, bounce bare-IP visitors to it.
    if [ -n "$PANEL_DOMAIN" ]; then
        mkdir -p /etc/nginx/serverkit-conf.d
        cat > /etc/nginx/serverkit-conf.d/canonical-domain.conf <<EOF
# Auto-generated by ServerKit install.sh
# Redirect direct IP access to the canonical panel domain.
#
# NOT \`listen 80 default_server\` with \`server_name _\`. Both serverkit.conf and
# serverkit-insecure.conf already declare a default server on :80 and then
# include this directory, and a second default server for the same address:port
# is a FATAL nginx error — "a duplicate default server for 0.0.0.0:80" — so
# nginx refused to start on any install that set PANEL_DOMAIN.
#
# A regex server_name is the right tool anyway: nginx tries exact names, then
# wildcards, then regexes, and only then the default server, so this block wins
# for a bare-IP Host and every other Host still falls through to the panel.
# That also removes the \`if\`, which nginx documents as best avoided.
server {
    listen 80;
    server_name ~^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\$;
    return 301 ${url_scheme}://$PANEL_DOMAIN\$request_uri;
}
EOF
        good "Canonical-domain redirect configured for $PANEL_DOMAIN"
    fi
}

# ---------------------------------------------------------------------------
# Frontend build (skipped on release installs)
# ---------------------------------------------------------------------------
# Give each install a unique favicon tile color so a favicon-hash comparison
# across ServerKit installs differs — a light-touch, Cloudflare-lava-lamp-style
# deterrent, NOT a security control.
#
# "Car-paint" model: pick a curated base HUE (tasteful families, no muddy
# yellows), then jitter the hue a few degrees and randomize the shade
# (saturation + lightness, kept rich and mid-dark — never light/washed-out,
# since a white glyph sits on top). So two installs that land on the same
# family still get visibly different paint, and the overall space is large
# enough that an identical color across installs is rare. The favicon is a
# plain-text SVG, so this is a pure string replace (fill → an hsl() value): no
# image tooling, and if it can't run the shipped default color just stays.
randomize_favicon() {
    local fav="$INSTALL_DIR/frontend/dist/favicon.svg"
    [ -f "$fav" ] || return 0
    # Assign the color ONCE and persist it in $CONFIG_DIR (survives updates), so
    # `serverkit update` re-applies the SAME color instead of repainting the box
    # on every update — a car keeps its paint. Existing installs get a color on
    # their first update. See scripts/update.sh for the matching re-apply.
    local color_file="$CONFIG_DIR/favicon-color"
    local hsl=""
    [ -s "$color_file" ] && hsl="$(cat "$color_file" 2>/dev/null)"
    if [ -z "$hsl" ]; then
        local hues=(210 225 235 250 265 285 320 345 12 25 160 175 190)
        local base="${hues[$RANDOM % ${#hues[@]}]}"
        local jitter=$(( RANDOM % 21 - 10 ))          # -10..+10 degrees
        local h=$(( (base + jitter + 360) % 360 ))
        local s=$(( 48 + RANDOM % 28 ))               # 48-75% saturation
        local l=$(( 32 + RANDOM % 15 ))               # 32-46% lightness (dark enough for the glyph)
        hsl="hsl(${h}, ${s}%, ${l}%)"
        mkdir -p "$CONFIG_DIR" 2>/dev/null || true
        printf '%s\n' "$hsl" > "$color_file" 2>/dev/null || true
        good "Favicon tint assigned: ${hsl}"
    fi
    sed -i -E "s|(<rect width=\"32\" height=\"32\" rx=\"7\" fill=\")[^\"]+(\")|\1${hsl}\2|" "$fav" 2>/dev/null || true
    # Serve the same tinted mark at /favicon.ico so a blind favicon-hash fetch
    # there varies per install too (SVG bytes under an .ico name — browsers use
    # the linked SVG; this only affects what a scanner hashes at that URL).
    cp "$fav" "$INSTALL_DIR/frontend/dist/favicon.ico" 2>/dev/null || true
}

build_frontend() {
    phase "Frontend Build"

    if [ "$INSTALL_FROM_RELEASE" = "1" ]; then
        good "Using the pre-built frontend from the release."
        randomize_favicon
        return
    fi

    step "Installing npm packages..."
    cd "$INSTALL_DIR/frontend"
    npm ci --prefer-offline 2>&1 | tail -3

    step "Compiling the frontend bundle..."
    NODE_OPTIONS="--max-old-space-size=1024" npm run build 2>&1 | tail -5
    good "Frontend built."
    randomize_favicon
}

# ---------------------------------------------------------------------------
# SSH port discovery — the safety interlock in front of default-deny.
#
# Turning on a default-deny firewall without allowing the port sshd actually
# answers on locks the operator out of their own server, over the very SSH
# session running this installer. So: never guess. Ask, in descending order of
# authority, and treat "no answer" as a hard stop rather than "probably 22".
#
#   1. `sshd -T` — the EFFECTIVE config. It resolves Include directives, so it
#      sees a port declared in /etc/ssh/sshd_config.d/*.conf that a naive grep
#      of sshd_config alone would miss (Ubuntu 22.04+ ships exactly that shape).
#   2. sshd_config + its drop-ins, parsed here. Covers boxes where `sshd -T`
#      refuses to run (missing host keys in a fresh image, non-root, ...).
#   3. Whatever sshd is listening on right now, per `ss`. Catches a running
#      daemon whose config on disk has drifted from what it was started with.
#
# A config file that names no Port at all means sshd's compiled-in default, so
# that case — and ONLY that case — resolves to 22. If not one of the three
# sources produced anything, the function returns non-zero and the caller must
# not enable a firewall: an exposed box is recoverable, a locked-out one
# usually is not.
#
# Echoes one port per line. SSHD_CONFIG_ROOT overrides /etc/ssh for tests.
# ---------------------------------------------------------------------------
detect_ssh_ports() {
    local etc="${SSHD_CONFIG_ROOT:-/etc/ssh}"
    local main="$etc/sshd_config"
    local raw="" ports="" p have_source=0

    if command -v sshd >/dev/null 2>&1; then
        raw="$(sshd -T 2>/dev/null | awk 'tolower($1) == "port" { print $2 }')" || raw=""
        [ -n "$raw" ] && have_source=1
    fi

    if [ -z "$raw" ] && [ -f "$main" ]; then
        have_source=1
        # Strip comments first so a commented-out "#Port 22" never counts.
        #
        # The `|| true` around cat is load-bearing, not defensive noise: most
        # boxes have no sshd_config.d/*.conf, the unmatched glob makes cat exit
        # non-zero, and under the installer's `set -o pipefail` that failure
        # would wipe a perfectly good answer — falling through to the "22"
        # default below on a box whose sshd is on 2222, and locking the
        # operator out. Exactly the failure this function exists to prevent.
        raw="$( { cat "$main" "$etc"/sshd_config.d/*.conf 2>/dev/null || true; } \
                | sed -e 's/#.*//' \
                | awk 'tolower($1) == "port" { print $2 }' )" || raw=""
    fi

    if [ -z "$raw" ] && command -v ss >/dev/null 2>&1; then
        raw="$(ss -H -tlnp 2>/dev/null | awk '/"sshd"/ { n = split($4, a, ":"); print a[n] }')" || raw=""
        [ -n "$raw" ] && have_source=1
    fi

    # A config that exists but names no Port means the built-in default.
    if [ -z "$raw" ] && [ "$have_source" = "1" ]; then
        raw="22"
    fi

    # Keep only plausible port numbers, de-duplicated, order preserved.
    for p in $raw; do
        case "$p" in
            ''|*[!0-9]*) continue ;;
        esac
        [ "$p" -ge 1 ] && [ "$p" -le 65535 ] || continue
        case " $ports " in
            *" $p "*) continue ;;
        esac
        ports="$ports $p"
    done
    ports="${ports# }"

    [ -n "$ports" ] || return 1
    for p in $ports; do printf '%s\n' "$p"; done
}

# Run a firewall command, or print it under FW_DRY_RUN. Same contract as
# scripts/lib/firewall.sh's _fw_run, kept local so the bootstrap below never
# depends on a private of that lib.
fw_bootstrap_run() {
    if [ "${FW_DRY_RUN:-0}" = "1" ]; then
        printf '  [firewall] would run: %s\n' "$*"
        return 0
    fi
    # Best-effort, like the lib's _fw_run callers: a rule that fails to apply
    # must never abort a `set -e` install. bootstrap_firewall's closing
    # `ufw status` check is what actually decides whether this worked.
    "$@" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Firewall bootstrap: put a default-deny firewall on a box that has none.
#
# The installer used to open 80/443 on whatever firewall it found and shrug
# ("assuming ports 80/443 are already open") when there wasn't one — which is
# the state of a stock Ubuntu/Debian VPS. That box then runs ServerKit with
# every port a deploy maps onto the host (8001-8999, plus database containers)
# facing the internet.
#
# Refuses to act, in this order, when:
#   * SERVERKIT_SKIP_FIREWALL=1        — operator said no; never argue.
#   * OS_FAMILY is not debian          — UFW is Debian/Ubuntu. RHEL-family boxes
#                                        already boot with firewalld ACTIVE, so
#                                        configure_firewall's open step covers
#                                        them. A firewalld default-deny bootstrap
#                                        for the boxes where it is *inactive* is
#                                        a deliberate follow-up: zones/services
#                                        are different enough that copying this
#                                        logic across would be wrong.
#   * a firewall is already active     — it is the operator's. Opening ports on
#                                        it is fine (next phase); rewriting its
#                                        default policy is not.
#   * inside a container               — the host owns netfilter; ufw either
#                                        fails outright (no NET_ADMIN) or, worse,
#                                        half-applies.
#   * we bootstrapped on a previous run and it is off now — the operator turned
#                                        it off on purpose. A re-run must not
#                                        undo that.
#   * the SSH port is unknown          — see detect_ssh_ports. Hard stop.
# ---------------------------------------------------------------------------
FIREWALL_BOOTSTRAP_KEY="firewall_bootstrapped"

bootstrap_firewall() {
    if [ "${SERVERKIT_SKIP_FIREWALL:-0}" = "1" ]; then
        good "Skipping firewall setup (SERVERKIT_SKIP_FIREWALL=1)."
        return 0
    fi
    if [ "${OS_FAMILY:-}" != "debian" ]; then
        return 0
    fi
    # "Is this box already protected", NOT "does a firewall binary exist".
    #
    # This used to ask firewall_detect() != none, which was wrong in the way
    # that matters: firewall_detect returns `nftables` when the nft binary is
    # merely present and can list an empty ruleset, so on a stock Ubuntu cloud
    # image — precisely the box this function exists for — the bootstrap
    # returned here silently and never installed anything. Caught on a real
    # 24.04 VM: the install logged "Opening HTTP/HTTPS (80, 443) via nftables"
    # while every line below went unprinted.
    if firewall_inbound_default_deny; then
        good "Inbound traffic is already filtered by default — leaving the firewall alone."
        return 0
    fi

    local ctr
    ctr="$(detect_container)"
    if [ -n "$ctr" ]; then
        warn "Running inside $ctr — leaving the firewall to the host."
        return 0
    fi

    if command -v state_get >/dev/null 2>&1 \
       && [ "$(state_get "$FIREWALL_BOOTSTRAP_KEY" 2>/dev/null || true)" = "1" ]; then
        good "A previous install set up the firewall and it is off now — leaving it alone."
        return 0
    fi

    # ---- the interlock. Nothing below runs without a known SSH port. -------
    local ssh_ports
    if ! ssh_ports="$(detect_ssh_ports)"; then
        warn "Could not determine which port sshd listens on — NOT enabling a firewall."
        warn "Refusing to risk locking you out. Declare 'Port <n>' in /etc/ssh/sshd_config and re-run,"
        warn "or set up UFW yourself: ufw allow <ssh>/tcp && ufw allow 80,443/tcp && ufw enable"
        return 0
    fi

    if ! command -v ufw >/dev/null 2>&1; then
        step "Installing UFW..."
        pkg_add ufw
    fi
    if ! command -v ufw >/dev/null 2>&1; then
        warn "UFW is unavailable — this box is left without a firewall."
        return 0
    fi

    # Order IS the safety property: every allow rule lands BEFORE the
    # default-deny policy, and the policy before the firewall is switched on.
    # `ufw allow` is idempotent ("Skipping adding existing rule"), so a re-run
    # never duplicates anything.
    local port
    for port in $ssh_ports; do
        step "Allowing SSH on ${port}/tcp..."
        fw_bootstrap_run ufw allow "${port}/tcp"
    done
    fw_bootstrap_run ufw allow 80/tcp
    fw_bootstrap_run ufw allow 443/tcp
    # A panel published on a non-standard port needs that port too, or the
    # operator loses the UI the instant default-deny lands.
    case "${PANEL_PORT:-80}" in
        80|443) : ;;
        *) fw_bootstrap_run ufw allow "${PANEL_PORT}/tcp" ;;
    esac

    fw_bootstrap_run ufw default deny incoming
    fw_bootstrap_run ufw default allow outgoing
    fw_bootstrap_run ufw --force enable

    if [ "${FW_DRY_RUN:-0}" = "1" ]; then
        good "Firewall bootstrap (dry run) complete."
        return 0
    fi

    if ufw status 2>/dev/null | grep -q 'Status: active'; then
        good "UFW enabled: default-deny inbound; SSH ($(printf '%s' "$ssh_ports" | tr '\n' ' ')), 80 and 443 allowed."
        if command -v state_set >/dev/null 2>&1; then
            state_set "$FIREWALL_BOOTSTRAP_KEY" 1 || true
        fi
    else
        warn "UFW did not come up active — this box is still unfirewalled."
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Firewall: open 80/443 so the panel is reachable from the outside, and record
# exactly what we opened in /etc/serverkit/install-state.json so uninstall can
# undo only those rules. Fresh RHEL-family boxes run firewalld by default, which
# is the #1 reason an otherwise-good install looks "broken" (port 80 blocked).
# ---------------------------------------------------------------------------
configure_firewall() {
    phase "Firewall"

    if ! load_serverkit_lib firewall.sh; then
        warn "Firewall helper not found — open ports 80/443 manually if needed."
        return 0
    fi
    load_serverkit_lib state.sh || true

    # Stock Debian/Ubuntu ships no firewall at all; put one there before the
    # "no active firewall detected" branch below just shrugs at it.
    bootstrap_firewall

    local backend
    backend="$(firewall_detect)"
    if [ "$backend" = "none" ]; then
        warn "No active firewall detected — assuming ports 80/443 are already open."
        return 0
    fi

    step "Opening HTTP/HTTPS (80, 443) via $backend..."
    firewall_open "$backend" 80/tcp 443/tcp
    if [ "${FW_DRY_RUN:-0}" != "1" ] && command -v state_set >/dev/null 2>&1; then
        # Best-effort, exactly like update.sh's ensure_firewall: failing to
        # RECORD the opened ports must not abort an install whose firewall
        # work already succeeded. (I21)
        state_set firewall_backend "$backend" || true
        state_append firewall_ports 80/tcp || true
        state_append firewall_ports 443/tcp || true
    fi
    good "Firewall configured ($backend): 80/tcp and 443/tcp open."
}

# ---------------------------------------------------------------------------
# Init-system probe + tiny service verbs (mirrors scripts/lib/init.sh)
# ---------------------------------------------------------------------------
# install.sh runs pre-clone in the `curl | bash` path, so it cannot source
# scripts/lib/init.sh — the detection is inline here and kept in sync with the
# lib: systemd when /run/systemd/system exists, then OpenRC/SysV fallbacks.
# INIT_OVERRIDE (same contract as init.sh) forces the answer for unit tests.
# Best-effort: on a box none of these can drive (containers, WSL) the verbs
# warn and return 0 — a missing init system must never abort the install. (I17)
svc_has_systemd() {
    if [ -n "${INIT_OVERRIDE:-}" ]; then
        [ "$INIT_OVERRIDE" = "systemd" ]
        return
    fi
    [ -d /run/systemd/system ] && command -v systemctl &>/dev/null
}

svc_enable() {
    local svc="$1"
    if svc_has_systemd; then
        systemctl enable "$svc" 2>/dev/null || warn "Could not enable $svc at boot."
    elif command -v rc-update &>/dev/null; then
        rc-update add "$svc" default 2>/dev/null || warn "Could not enable $svc at boot."
    elif command -v chkconfig &>/dev/null; then
        chkconfig "$svc" on 2>/dev/null || warn "Could not enable $svc at boot."
    elif command -v update-rc.d &>/dev/null; then
        update-rc.d "$svc" enable 2>/dev/null || warn "Could not enable $svc at boot."
    else
        warn "No init system found to enable $svc — enable it at boot manually."
    fi
    return 0
}

svc_start() {
    local svc="$1"
    if svc_has_systemd; then
        systemctl start "$svc" 2>/dev/null || warn "Could not start $svc."
    elif command -v rc-service &>/dev/null; then
        rc-service "$svc" start 2>/dev/null || warn "Could not start $svc."
    elif command -v service &>/dev/null; then
        service "$svc" start 2>/dev/null || warn "Could not start $svc."
    else
        warn "No init system found to start $svc — start it manually."
    fi
    return 0
}

# Write the SSH brute-force jail as a jail.d drop-in.
#
# sshd is the one service every box exposes and it is under a password spray
# within minutes of getting a public IP, so this is the jail that matters on a
# fresh VPS. Two things make it correct rather than decorative:
#
#   * it watches the SAME port(s) detect_ssh_ports found for the firewall — a
#     jail pinned to :22 on a box whose sshd moved to :2222 bans nobody;
#   * it declares `backend = systemd` when there is no syslog file to tail
#     (Ubuntu 24.04+ and RHEL are journald-only). Without that the jail refuses
#     to start with "Have not found any log file for sshd jail" and fail2ban
#     looks installed while protecting nothing.
#
# The filename deliberately does NOT start with "serverkit-": that prefix is
# Fail2banJailService's ownership namespace for per-site jails, and this file is
# the installer's. It sorts after Debian's defaults-debian.conf, so jail.d
# ordering makes ours the winner. Regenerated on every run (idempotent).
write_sshd_jail() {
    local jail_dir ports backend_line=""
    jail_dir="${FAIL2BAN_JAIL_DIR:-/etc/fail2ban/jail.d}"

    if ! ports="$(detect_ssh_ports)"; then
        warn "Could not determine the SSH port — leaving fail2ban's sshd jail at its defaults."
        return 0
    fi
    ports="$(printf '%s' "$ports" | tr '\n' ',' | sed 's/,$//')"

    if [ ! -f /var/log/auth.log ] && [ ! -f /var/log/secure ] && svc_has_systemd; then
        backend_line="backend  = systemd"
    fi

    mkdir -p "$jail_dir" 2>/dev/null || true
    if ! cat > "$jail_dir/sshd-serverkit.conf" <<F2B_EOF
# ServerKit-managed fail2ban jail: SSH brute force.
# Generated by install.sh - regenerated on every install, do not edit by hand.
[sshd]
enabled  = true
port     = ${ports}
${backend_line}
maxretry = 5
findtime = 600
bantime  = 3600
F2B_EOF
    then
        warn "Could not write the fail2ban sshd jail to $jail_dir."
        return 0
    fi
    good "fail2ban sshd jail watching port(s): ${ports}."
}

provision_hardening() {
    # What "standard" and "full" each put on the box.
    #
    # The panel ships two features that quietly do nothing unless a daemon it
    # never installs is present: fail2ban_jail_service manages jails, and
    # configure_nginx only warns "Install certbot and run..." when certbot is
    # missing.
    #
    # fail2ban is standard-and-up: the small VPS is EXACTLY the box that gets
    # SSH-brute-forced, and leaving it to "full" meant the typical install got
    # neither a firewall nor a jail. certbot stays a "full" extra — HTTPS is
    # optional by decree, so it is a choice, not a baseline.
    #
    # Every step is warn-and-continue (pkg_add's contract) — a hardening extra
    # that fails to install must never abort an otherwise good install.
    case "$PROFILE" in
        minimal) return 0 ;;
    esac

    phase "Hardening"

    if command -v fail2ban-server &>/dev/null; then
        good "fail2ban already present."
    else
        step "Installing fail2ban..."
        pkg_add fail2ban
    fi
    if command -v fail2ban-server &>/dev/null; then
        write_sshd_jail
        svc_enable fail2ban
        svc_start fail2ban
        good "fail2ban enabled — manage jails from Security in the panel."
    else
        warn "fail2ban unavailable; the panel's jail management will stay inert."
    fi

    # Everything below is what makes "full" more than "standard".
    [ "$PROFILE" = "full" ] || return 0

    # Certbot only matters when this install is actually attempting HTTPS.
    if [ "$SERVERKIT_SKIP_SSL" = "1" ]; then
        good "Skipping certbot (SERVERKIT_SKIP_SSL=1)."
    elif command -v certbot &>/dev/null; then
        good "certbot already present."
    else
        step "Installing certbot..."
        case "$OS_FAMILY" in
            debian|ubuntu) pkg_add certbot python3-certbot-nginx ;;
            fedora|rhel)   pkg_add certbot python3-certbot-nginx ;;
            suse)          pkg_add certbot python3-certbot-nginx ;;
            arch)          pkg_add certbot certbot-nginx ;;
            alpine)        pkg_add certbot certbot-nginx ;;
            *)             pkg_add certbot ;;
        esac
        if command -v certbot &>/dev/null; then
            good "certbot installed."
        else
            warn "certbot unavailable — HTTPS stays best-effort on this host."
        fi
    fi

    return 0
}

# ---------------------------------------------------------------------------
# nginx site + reverse-proxy wiring
# ---------------------------------------------------------------------------
configure_nginx() {
    phase "Nginx"

    if command -v nginx &>/dev/null; then
        good "Nginx already installed."
    else
        step "Installing Nginx..."
        pkg_add nginx
        svc_enable nginx
        good "Nginx installed."
    fi

    systemctl stop nginx 2>/dev/null || true
    rm -f /etc/nginx/sites-enabled/default

    mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/serverkit-conf.d /var/www/certbot

    # Fedora/RHEL nginx.conf doesn't include sites-enabled by default.
    if ! grep -q "sites-enabled" /etc/nginx/nginx.conf; then
        sed -i '/http {/a \    include /etc/nginx/sites-enabled/*;' /etc/nginx/nginx.conf
    fi

    # Server-wide TLS floor: harden ssl_protocols/ssl_ciphers in the main
    # nginx.conf so even the default server and vhosts ServerKit did not
    # generate cannot negotiate TLS 1.0/1.1 or weak ciphers.
    harden_global_tls

    # Drop any foreign catch-all vhost that would shadow the panel.
    for conf in /etc/nginx/sites-enabled/*; do
        [ -f "$conf" ] || continue
        local name; name=$(basename "$conf")
        case "$name" in serverkit-*|serverkit.conf) continue ;; esac
        if grep -Eq 'server_name[[:space:]]+_;' "$conf" 2>/dev/null; then
            warn "Removing conflicting catch-all vhost: $name"
            rm -f "/etc/nginx/sites-enabled/$name" "/etc/nginx/sites-available/$name"
        fi
    done

    cp "$INSTALL_DIR/nginx/sites-available/serverkit.conf" /etc/nginx/sites-available/
    cp "$INSTALL_DIR/nginx/sites-available/serverkit-insecure.conf" /etc/nginx/sites-available/
    cp "$INSTALL_DIR/nginx/sites-available/example.conf.template" /etc/nginx/sites-available/

    # The panel frontend is served as static files by host nginx (no container).
    # The shipped config roots at the default /opt/serverkit; point it at the real
    # install dir when SERVERKIT_DIR was customised.
    apply_frontend_root /etc/nginx/sites-available/serverkit.conf \
                        /etc/nginx/sites-available/serverkit-insecure.conf

    # Decide whether we can use HTTPS. SSL is best-effort: if certbot fails or
    # no domain is given, we fall back to plain HTTP rather than forcing the
    # user to fix DNS/certs before they can use ServerKit.
    choose_ssl_mode
    install_nginx_config_for_mode

    # SELinux: let nginx make upstream connections to the app containers AND read
    # the SPA bundle it now serves from $INSTALL_DIR (default /opt, which is not
    # httpd_sys_content_t by default — without this nginx 403s every panel asset).
    if { [ "$OS_FAMILY" = "fedora" ] || [ "$OS_FAMILY" = "rhel" ]; } && command -v setsebool &>/dev/null; then
        setsebool -P httpd_can_network_connect 1 2>/dev/null || true
    fi
    selinux_label_frontend_dist

    good "Nginx configured ($SSL_MODE)."
}

# ---------------------------------------------------------------------------
# Choose secure or insecure mode. Never block the install because of SSL.
# ---------------------------------------------------------------------------
choose_ssl_mode() {
    if [ "$SERVERKIT_SKIP_SSL" = "1" ]; then
        SSL_MODE="insecure"
        warn "SERVERKIT_SKIP_SSL=1 — using plain HTTP."
        return
    fi

    if [ -z "$PANEL_DOMAIN" ]; then
        SSL_MODE="insecure"
        warn "No panel domain provided — using plain HTTP (access by IP)."
        warn "Set PANEL_DOMAIN and re-run the installer to try HTTPS automatically."
        return
    fi

    if ! command -v certbot &>/dev/null; then
        SSL_MODE="insecure"
        warn "Certbot not installed — cannot request SSL automatically."
        warn "Continuing with plain HTTP. Install certbot and run: certbot --nginx -d $PANEL_DOMAIN"
        return
    fi

    # Temporarily use the insecure config so certbot's webroot challenge can
    # answer on port 80.
    ln -sf /etc/nginx/sites-available/serverkit-insecure.conf /etc/nginx/sites-enabled/serverkit.conf
    step "Attempting Let's Encrypt certificate for $PANEL_DOMAIN..."
    if systemctl start nginx 2>/dev/null && \
       certbot certonly --webroot -w /var/www/certbot -d "$PANEL_DOMAIN" \
           --non-interactive --agree-tos --register-unsafely-without-email 2>/dev/null; then
        SSL_MODE="secure"
        good "Let's Encrypt certificate issued for $PANEL_DOMAIN."
    else
        SSL_MODE="insecure"
        warn "Could not obtain SSL certificate for $PANEL_DOMAIN."
        warn "Common causes: DNS not pointing to this server, port 80 not reachable, or rate limits."
        warn "Continuing with plain HTTP. Retry later with: certbot --nginx -d $PANEL_DOMAIN"
    fi
    systemctl stop nginx 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Install the nginx config matching the chosen SSL mode.
# ---------------------------------------------------------------------------
install_nginx_config_for_mode() {
    # Dirs are overridable so the unit tests can exercise this against
    # fixtures (same pattern as harden_global_tls / update.sh's NGINX_DIR).
    local nginx_dir="${SERVERKIT_NGINX_DIR:-/etc/nginx}"
    local config_dir="${SERVERKIT_CONFIG_DIR:-/etc/serverkit}"
    if [ "$SSL_MODE" = "secure" ]; then
        sed -i "s|/etc/letsencrypt/live/YOUR_DOMAIN/|/etc/letsencrypt/live/$PANEL_DOMAIN/|g" \
            "$nginx_dir/sites-available/serverkit.conf"
        ln -sf "$nginx_dir/sites-available/serverkit.conf" "$nginx_dir/sites-enabled/serverkit.conf"
    else
        ln -sf "$nginx_dir/sites-available/serverkit-insecure.conf" "$nginx_dir/sites-enabled/serverkit.conf"
    fi
    mkdir -p "$config_dir"
    printf '%s\n' "$SSL_MODE" > "$config_dir/ssl-mode"
    # Persist the domain so update.sh can re-apply the cert path on upgrades
    # without depending on the (commented-out) .env public URL. An empty
    # domain (the default no-domain install) is simply not persisted — and as
    # the LAST statement of the function this must be an `if`, not a
    # `[ -n ] && ...` list: the list form returned 1 on an empty domain and
    # set -e killed every no-domain install right here. (I1)
    if [ -n "$PANEL_DOMAIN" ]; then
        printf '%s\n' "$PANEL_DOMAIN" > "$config_dir/panel-domain"
    fi
}

# Point the static-frontend `root` at the real install dir. The shipped nginx
# config defaults to /opt/serverkit; only a customised SERVERKIT_DIR needs the
# rewrite. update.sh's refresh_config carries the same logic across upgrades.
apply_frontend_root() {
    [ "$INSTALL_DIR" = "/opt/serverkit" ] && return 0
    local f
    for f in "$@"; do
        # `if`, not a `[ -f ] && ...` list: with the list form a missing conf
        # as the LAST file made the loop (and thus the function) return 1,
        # and set -e killed the custom-SERVERKIT_DIR install right here —
        # the same species as I1/I2.
        if [ -f "$f" ]; then
            sed -i "s|/opt/serverkit/frontend/dist|$INSTALL_DIR/frontend/dist|g" "$f"
        fi
    done
    return 0
}

# Label the SPA bundle so SELinux-enforcing hosts (Fedora/RHEL) let nginx read
# it. /opt is not httpd_sys_content_t by default, so without this every panel
# asset 403s. Label the *real* slot path (blue/green resolves through a symlink).
# Best-effort: a permissive/disabled box, or one without the tools, just no-ops.
selinux_label_frontend_dist() {
    command -v selinuxenabled &>/dev/null && selinuxenabled 2>/dev/null || return 0
    local real dist
    real="$(readlink -f "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR")"
    dist="${real}/frontend/dist"
    [ -d "$dist" ] || return 0
    if command -v semanage &>/dev/null && command -v restorecon &>/dev/null; then
        semanage fcontext -a -t httpd_sys_content_t "${dist}(/.*)?" 2>/dev/null \
            || semanage fcontext -m -t httpd_sys_content_t "${dist}(/.*)?" 2>/dev/null || true
        restorecon -R "$dist" 2>/dev/null || true
    elif command -v chcon &>/dev/null; then
        chcon -R -t httpd_sys_content_t "$dist" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# Server-wide TLS floor — force every HTTPS listener onto TLS 1.2/1.3 with
# AEAD-only ciphers, so even the default server and non-ServerKit vhosts can't
# negotiate weak TLS.
#
# Preferred: drop a self-contained /etc/nginx/conf.d/serverkit-tls.conf snippet
# — ServerKit-owned, trivially reversible on uninstall, and never touches the
# distro's nginx.conf. BUT a snippet is `include`d into the same http{} context,
# so if nginx.conf ALREADY declares ssl_protocols (Debian/Ubuntu do) a second
# declaration is a "duplicate ssl_protocols" error. In that case we fall back to
# rewriting the existing directives in place. Idempotent either way.
# ---------------------------------------------------------------------------
harden_global_tls() {
    local nginx_dir="${SERVERKIT_NGINX_DIR:-/etc/nginx}"
    local conf="$nginx_dir/nginx.conf"
    [ -f "$conf" ] || return 0
    local ciphers='ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384'

    # Does the main config already declare these in http{}? (Debian/Ubuntu yes,
    # RHEL/Fedora/SUSE typically no.)
    local has_proto=0 has_ciphers=0
    grep -qE '^[[:space:]]*ssl_protocols[[:space:]]' "$conf" && has_proto=1
    grep -qE '^[[:space:]]*ssl_ciphers[[:space:]]'   "$conf" && has_ciphers=1

    # The conf.d snippet is only safe when neither directive already exists AND
    # nginx.conf includes conf.d/*.conf inside http{} (the default on every
    # supported family).
    if [ "$has_proto" = "0" ] && [ "$has_ciphers" = "0" ] && \
       grep -qE 'include[[:space:]]+/etc/nginx/conf\.d/\*\.conf' "$conf"; then
        mkdir -p "$nginx_dir/conf.d"
        cat > "$nginx_dir/conf.d/serverkit-tls.conf" <<EOF
# Auto-generated by ServerKit — server-wide TLS floor. Safe to remove.
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers ${ciphers};
EOF
        return 0
    fi

    # Fall back to editing nginx.conf in place (a snippet would duplicate the
    # existing directive). Remove any stale snippet we may have dropped before.
    rm -f "$nginx_dir/conf.d/serverkit-tls.conf" 2>/dev/null || true
    if [ "$has_proto" = "1" ]; then
        sed -i -E 's|^([[:space:]]*)ssl_protocols[[:space:]].*|\1ssl_protocols TLSv1.2 TLSv1.3;|' "$conf"
    else
        sed -i '/http {/a \    ssl_protocols TLSv1.2 TLSv1.3;' "$conf"
    fi
    if [ "$has_ciphers" = "1" ]; then
        sed -i -E "s|^([[:space:]]*)ssl_ciphers[[:space:]].*|\1ssl_ciphers ${ciphers};|" "$conf"
    else
        sed -i "/http {/a \\    ssl_ciphers ${ciphers};" "$conf"
    fi
}

# ---------------------------------------------------------------------------
# systemd unit + CLI symlink
# ---------------------------------------------------------------------------
# Render the systemd unit from the template to <out>, substituting the resolved
# install/venv/log paths so a custom SERVERKIT_DIR is honored (the old hardcoded
# unit baked in /opt/serverkit). The unit references the /opt/serverkit symlink,
# so blue/green switches need no re-render. Returns 1 if no template/unit exists.
render_service_unit() {
    local out="$1"
    local template="$INSTALL_DIR/templates/serverkit-backend.service.in"
    local bind_host
    bind_host="$(backend_bind_host)"
    if [ -f "$template" ]; then
        sed -e "s|@SERVERKIT_DIR@|$INSTALL_DIR|g" \
            -e "s|@SERVERKIT_VENV_DIR@|$VENV_DIR|g" \
            -e "s|@PORT@|5000|g" \
            -e "s|@BIND_HOST@|$bind_host|g" \
            -e "s|@USER@|root|g" \
            -e "s|@LOG_DIR@|$LOG_DIR|g" \
            "$template" > "$out"
        return 0
    elif [ -f "$INSTALL_DIR/serverkit-backend.service" ]; then
        cp "$INSTALL_DIR/serverkit-backend.service" "$out"
        return 0
    fi
    return 1
}

install_service() {
    phase "Systemd Service"

    render_service_unit /etc/systemd/system/serverkit.service || \
        halt "No systemd unit template found at $INSTALL_DIR/templates/serverkit-backend.service.in"
    chmod 644 /etc/systemd/system/serverkit.service

    chmod +x "$INSTALL_DIR/serverkit"
    ln -sf "$INSTALL_DIR/serverkit" /usr/local/bin/serverkit

    # Bash tab-completion (best-effort — not every distro ships bash-completion).
    if [ -d /etc/bash_completion.d ]; then
        bash "$INSTALL_DIR/serverkit" completion > /etc/bash_completion.d/serverkit 2>/dev/null \
            || rm -f /etc/bash_completion.d/serverkit
    fi

    # Without systemd (LXC/WSL/containers) we can't enable the unit — say so
    # clearly instead of failing cryptically (Goal G5). The probe is inline
    # (svc_has_systemd) so it holds even when scripts/lib never made it to
    # disk, where the old env.sh-based check silently skipped itself. (I17)
    if ! svc_has_systemd; then
        warn "systemd not detected — installed the unit but cannot enable/start it here."
        warn "Start the backend with a supervisor, or run it in the foreground; see docs/ARCHITECTURE.md."
        return 0
    fi

    systemctl daemon-reload
    systemctl enable serverkit
    good "Systemd service installed."
}

# ---------------------------------------------------------------------------
# App templates
# ---------------------------------------------------------------------------
sync_templates() {
    phase "App Templates"

    if [ -d "$INSTALL_DIR/backend/templates" ]; then
        cp -r "$INSTALL_DIR/backend/templates/"*.yaml /etc/serverkit/templates/ 2>/dev/null || true
        cp -r "$INSTALL_DIR/backend/templates/"*.yml  /etc/serverkit/templates/ 2>/dev/null || true
        good "Installed $(ls /etc/serverkit/templates/*.yaml 2>/dev/null | wc -l) app templates."
    else
        warn "No app templates found."
    fi
}

# ---------------------------------------------------------------------------
# Bring services up
# ---------------------------------------------------------------------------
launch_services() {
    phase "Starting Services"

    step "Starting the backend..."
    svc_start serverkit

    # The frontend is static files served by host nginx (built into
    # frontend/dist by build_frontend) — no container to build or start.
    step "Starting nginx..."
    svc_start nginx
    good "Services started."
}

# ---------------------------------------------------------------------------
# Health probe + automatic rollback
# ---------------------------------------------------------------------------
revert_install() {
    warn "Health check failed — rolling back..."

    systemctl stop serverkit 2>/dev/null || true
    systemctl stop nginx 2>/dev/null || true

    local active
    active="$(readlink -f "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR")"
    if [ -d "$active.backup" ]; then
        rm -rf "$active"
        cp -a "$active.backup" "$active"
        warn "Restored the previous installation from $active.backup"
    fi

    systemctl daemon-reload
    systemctl start serverkit 2>/dev/null || true
    systemctl start nginx 2>/dev/null || true
    halt "Install rolled back. Inspect logs: journalctl -u serverkit -n 50"
}

await_health() {
    phase "Health Check"

    step "Waiting for the backend to answer..."
    local waited=0
    while [ "$waited" -lt 30 ]; do
        if curl -sf --max-time 5 http://127.0.0.1:5000/api/v1/system/health >/dev/null 2>&1; then
            good "Backend healthy."
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done

    warn "Backend did not respond on port 5000."
    return 1
}

# ---------------------------------------------------------------------------
# Safety copy of an existing install before we touch it
# ---------------------------------------------------------------------------
snapshot_existing() {
    local active
    active="$(readlink -f "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR")"
    [ -d "$active" ] || return 0
    if [ ! -d "$active.backup" ]; then
        step "Backing up the existing installation..."
        cp -a "$active" "$active.backup"
        good "Backup saved to $active.backup"
        return 0
    fi
    # A .backup left by an earlier run is what revert_install restores — a
    # stale one would silently roll the box back to a months-old tree.
    # Refresh it: build the new copy first and swap only on success, so a
    # failed copy never costs us the backup we already have. (I20)
    step "Refreshing the existing installation backup..."
    rm -rf "$active.backup.new" 2>/dev/null || true
    if cp -a "$active" "$active.backup.new" 2>/dev/null; then
        rm -rf "$active.backup"
        mv "$active.backup.new" "$active.backup"
        good "Backup refreshed at $active.backup"
    else
        rm -rf "$active.backup.new" 2>/dev/null || true
        warn "Could not refresh $active.backup — keeping the previous backup."
    fi
}

# ---------------------------------------------------------------------------
# Closing summary
# ---------------------------------------------------------------------------
print_outro() {
    local ip
    ip=$(curl -sf --max-time 5 https://api.ipify.org 2>/dev/null || \
         hostname -I 2>/dev/null | awk '{print $1}' || echo "YOUR_SERVER_IP")

    printf '\n'
    printf '  %s%s✔  ServerKit installed successfully%s   %s%s%s\n' \
        "$BLD" "$HUE_OK" "$RST" "$FOG" "$(clock)" "$RST"
    printf '  %s%s%s\n\n' "$V4" "──────────────────────────────────────" "$RST"

    if [ -n "$PANEL_DOMAIN" ]; then
        if [ "$SSL_MODE" = "secure" ]; then
            printf '  %sPanel URL%s      https://%s\n' "$BLD" "$RST" "$PANEL_DOMAIN"
        else
            printf '  %sPanel URL%s      http://%s\n' "$BLD" "$RST" "$PANEL_DOMAIN"
        fi
    else
        printf '  %sPanel URL%s      http://%s\n' "$BLD" "$RST" "$ip"
    fi

    printf '  %sProfile%s        %s\n' "$BLD" "$RST" "$PROFILE"
    if [ "$PROFILE" = "minimal" ]; then
        printf '                 Docker was not installed, so app hosting is off.\n'
        printf '                 Add it any time from Settings — nothing is locked.\n'
    fi

    if [ "$SSL_MODE" != "secure" ]; then
        printf '\n  %sWARNING%s        Running without HTTPS. Passwords and tokens will be\n' "$BLD" "$RST"
        printf '                 transmitted unencrypted. Set PANEL_DOMAIN and run\n'
        printf '                 certbot, or set SERVERKIT_SKIP_SSL=1 to suppress this warning.\n'
    fi

    printf '\n  %sFirst step%s     create an admin user\n\n' "$BLD" "$RST"

    printf '  %sCLI%s            serverkit status\n' "$BLD" "$RST"
    printf '                 serverkit create-admin\n'
    printf '                 serverkit --help\n\n'

    printf '  %sService%s        systemctl status serverkit\n' "$BLD" "$RST"
    printf '                 journalctl -u serverkit -f\n'
    printf '                 systemctl restart serverkit\n\n'

    printf '  %sPaths%s          install : %s\n' "$BLD" "$RST" "$INSTALL_DIR"
    printf '                 config  : %s/.env\n' "$INSTALL_DIR"
    printf '                 backups : %s\n\n' "$BACKUP_DIR"

    printf '  %sUpdate%s         sudo serverkit update\n\n' "$HUE_WARN" "$RST"
}

# ---------------------------------------------------------------------------
# Anonymous install ping (best-effort)
# ---------------------------------------------------------------------------
ping_telemetry() {
    # Guarded: a missing VERSION file fails the pipeline under pipefail, and
    # this runs AFTER a successful install — it must never abort it. (I23)
    local v
    v=$(cat "$INSTALL_DIR/VERSION" 2>/dev/null | tr -d '\n\r ') || v=""
    curl -s "https://serverkit.ai/track/install?v=${v}" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Ask for a panel domain when running interactively
# ---------------------------------------------------------------------------
prompt_for_domain() {
    # Nothing to ask when a domain is already set, or when there is no
    # interactive stdin to ask on (curl | bash). SERVERKIT_FORCE_PROMPT=1
    # lets the unit tests drive the prompt from a pipe.
    [ -z "$PANEL_DOMAIN" ] || return 0
    if [ ! -t 0 ] && [ "${SERVERKIT_FORCE_PROMPT:-0}" != "1" ]; then
        return 0
    fi
    printf '\n'
    printf '%sEnter your panel domain (e.g. panel.example.com)%s\n' "$BLD" "$RST"
    printf 'Leave blank to access via IP (no SSL attempt)\n'
    printf '%sTip:%s set PANEL_DOMAIN=... in the environment to skip this prompt\n' "$BLD" "$RST"
    printf '      set SERVERKIT_SKIP_SSL=1 to disable HTTPS entirely\n'
    printf '> '
    read -r PANEL_DOMAIN || true
    PANEL_DOMAIN=$(printf '%s' "$PANEL_DOMAIN" | tr -d ' ')
    # A blank answer (plain Enter) is the documented default — and as the
    # LAST statement of the function this must be an `if`, not a
    # `[ -n ] && ...` list: the list form returned 1 on a blank answer and
    # set -e killed every interactive no-domain install right here. (I2)
    if [ -n "$PANEL_DOMAIN" ]; then
        PANEL_PORT="80"
    fi
}

prompt_for_public_url() {
    # Only meaningful when someone else terminates TLS in front of us.
    [ "$SERVERKIT_EXTERNAL_PROXY" = "1" ] || return 0
    [ -z "$PANEL_PUBLIC_URL" ] || return 0

    # Derive rather than ask when the domain already answers the question.
    if [ -n "$PANEL_DOMAIN" ]; then
        PANEL_PUBLIC_URL="https://$PANEL_DOMAIN"
        good "Public URL: $PANEL_PUBLIC_URL (from PANEL_DOMAIN)"
        return 0
    fi

    if [ ! -t 0 ] && [ "${SERVERKIT_FORCE_PROMPT:-0}" != "1" ]; then
        warn "SERVERKIT_EXTERNAL_PROXY=1 without SERVERKIT_PUBLIC_URL."
        warn "Realtime updates will be rejected until you set it in $INSTALL_DIR/.env."
        return 0
    fi

    printf '\n'
    printf '%sPublic URL of the panel, as your reverse proxy publishes it%s\n' "$BLD" "$RST"
    printf 'Example: https://serverkit.example.com\n'
    printf 'Required: the browser sends this as its websocket Origin, and the\n'
    printf 'panel rejects the handshake when it does not match.\n'
    printf '%sTip:%s set SERVERKIT_PUBLIC_URL=... to skip this prompt\n' "$BLD" "$RST"
    printf '> '
    read -r PANEL_PUBLIC_URL || true
    PANEL_PUBLIC_URL=$(printf '%s' "$PANEL_PUBLIC_URL" | tr -d ' ')
    PANEL_PUBLIC_URL="${PANEL_PUBLIC_URL%/}"
    if [ -z "$PANEL_PUBLIC_URL" ]; then
        warn "No public URL given — realtime updates will be rejected until"
        warn "SERVERKIT_PUBLIC_URL is set in $INSTALL_DIR/.env."
    fi
}

# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
# Decide whether a fresh install should default to a release tarball. An
# EXISTING install must never flip to release mode by default: fetch_release
# rewrites the whole slot. The old check tested $INSTALL_DIR/backend/src — a
# path that never existed (the tree is backend/app) — so every re-run over a
# live install picked release mode and rm -rf'd it, .env and database
# included. Probe the real tree and the generated .env instead. (I4)
should_default_to_release() {
    [ "$BUILD_FROM_SOURCE" != "1" ] || return 1
    [ -z "${SERVERKIT_VERSION:-}" ] || return 1
    [ ! -d "$INSTALL_DIR/backend/app" ] || return 1
    [ ! -f "$INSTALL_DIR/.env" ] || return 1
    return 0
}

main() {
    STARTED_AT=$(date +%s)
    masthead

    # Default to a release install unless an install is already present or
    # the caller forced a source build / pinned a version.
    if should_default_to_release; then
        INSTALL_FROM_RELEASE=1
    fi

    identify_system
    # preflight's root check must run before choose_pkg_manager, which
    # writes the apt lock-wait drop-in to /etc. (I11)
    preflight
    choose_pkg_manager
    ensure_bootstrap_tools
    gauge_memory
    ensure_swap
    # Ask what to install before anything is provisioned — the answer decides
    # whether provision_docker runs at all.
    prompt_for_profile
    prompt_for_domain
    prompt_for_public_url
    snapshot_existing

    # RHEL 9 family: keep sshd alive across the dnf work below (see the
    # function's comment for the full openssl/openssh mismatch story).
    upgrade_rhel_crypto_stack

    provision_docker
    ensure_compose_plugin
    provision_node

    if [ "$INSTALL_FROM_RELEASE" = "1" ]; then
        fetch_release || warn "Falling back to a source build."
    fi

    # When the release fetch failed, provision_node above already skipped
    # Node.js ("the release ships a pre-built frontend") — but the source
    # build below compiles the frontend and needs npm. provision_node is
    # idempotent (node_ready early-return), so just run it again. (Found via
    # Test Sandbox full mode on a GitHub-API-rate-limited host.)
    if [ "$INSTALL_FROM_RELEASE" != "1" ]; then
        provision_node
    fi

    if [ "$INSTALL_FROM_RELEASE" != "1" ]; then
        provision_python
        sync_source
        make_directories
        build_virtualenv
        build_frontend
    else
        make_directories
        # Release tarballs now ship a pre-built, relocatable venv. If one is
        # present, build_virtualenv will use it; otherwise it falls back to a
        # local build.
        provision_python
        build_virtualenv
    fi

    sync_templates
    # Before configure_nginx: on the full profile this is what puts certbot on
    # the box, and configure_nginx's HTTPS attempt needs it already there.
    provision_hardening
    configure_nginx
    configure_firewall
    write_config
    install_service

    # Tolerate failures through start + health so rollback can run.
    set +e
    launch_services
    await_health || revert_install
    set -e

    ping_telemetry
    print_outro
}

# Sourcing this file (e.g. from scripts/test/test_install.sh) defines every
# function above for unit testing without running an install. Only a direct
# execution falls through to the run below.
#
# Piped execution (`curl -fsSL … | bash`, the documented one-liner) leaves
# BASH_SOURCE unset entirely, so a bare ${BASH_SOURCE[0]} is an unbound
# variable under `set -u` and kills the install before main() — test the
# array is non-empty FIRST. Same guard as scripts/install.sh.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ "${BASH_SOURCE[0]}" != "$0" ]; then
    return 0
fi

main "$@"
