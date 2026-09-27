#!/bin/bash
#
# ServerKit updater — atomic blue/green, pre-flight checked, offline-capable.
#
# Updates default to a PRE-BUILT release tarball — the frontend is built once in
# CI and shipped, so the server needs no Node/npm and no on-box SPA build (that's
# what makes updates work uniformly on old distros, ARM/Pi, and tiny boxes). Opt
# into an on-box source rebuild with --source / --branch / BUILD_FROM_SOURCE=1
# (that path needs a compatible Node — see install.sh's node floor).
#
# Usage:
#   bash /opt/serverkit/scripts/update.sh                 # pre-built release (default, Node-free)
#   bash /opt/serverkit/scripts/update.sh --dry-run
#   bash /opt/serverkit/scripts/update.sh --source        # rebuild from main on this box (needs Node)
#   bash /opt/serverkit/scripts/update.sh --branch dev    # rebuild from a branch on this box (needs Node)
#   bash /opt/serverkit/scripts/update.sh --release [v1.7.0]
#   SERVERKIT_OFFLINE_TARBALL=/tmp/serverkit-v1.7.0-linux-amd64.tar.gz bash /opt/serverkit/scripts/update.sh
#
# -E (errtrace) makes the ERR trap fire for failures *inside* functions/subshells
# too — without it a silent death deep in a helper would never be reported.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Configuration + argument parsing
# ---------------------------------------------------------------------------
DRY_RUN=0
FORCE_UPDATE=0
TARGET_BRANCH=""
# Default to a pre-built release update (Node-free). --source / --branch /
# BUILD_FROM_SOURCE=1 opt into an on-box source rebuild instead.
USE_RELEASE="${INSTALL_FROM_RELEASE:-1}"
[ "${BUILD_FROM_SOURCE:-0}" = "1" ] && USE_RELEASE=0
RELEASE_VERSION="${SERVERKIT_VERSION:-}"

# Captured before parsing so the self-update re-exec can forward them verbatim.
ORIG_ARGS=("$@")

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run|-n)
            DRY_RUN=1
            shift
            ;;
        --force|-f)
            FORCE_UPDATE=1
            shift
            ;;
        --source|--main)
            # Rebuild from the main branch on this box (needs a compatible Node).
            USE_RELEASE=0
            TARGET_BRANCH=""
            shift
            ;;
        --branch|-b)
            # A branch update is an on-box source rebuild — override the release default.
            TARGET_BRANCH="$2"
            USE_RELEASE=0
            shift 2
            ;;
        --release|-r)
            USE_RELEASE=1
            if [[ -n "${2:-}" ]] && [[ ! "$2" =~ ^- ]]; then
                RELEASE_VERSION="$2"
                shift 2
            else
                shift
            fi
            ;;
        --help|-h)
            cat <<'EOF'
Usage: update.sh [OPTIONS]

Options:
  --dry-run, -n           Show what would happen without making changes
  --force, -f             Skip version comparison and update anyway
  --release [version], -r Update from a pre-built release tarball (DEFAULT; Node-free)
  --source, --main        Rebuild from the main branch on this box (needs Node)
  --branch <name>, -b     Rebuild from a git branch on this box (needs Node)
  --help, -h              Show this help message

Environment:
  SERVERKIT_DIR           Active install directory (default: /opt/serverkit)
  SERVERKIT_VENV_DIR      Python venv path (default: $SERVERKIT_DIR/venv)
  SERVERKIT_OFFLINE_TARBALL  Local release tarball to use instead of downloading
  SERVERKIT_MIRROR_URL    Base URL for release tarballs/checksums
  GITHUB_REPO             GitHub org/repo for source and releases
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SERVERKIT_DIR="${SERVERKIT_DIR:-/opt/serverkit}"
INSTALL_DIR="$SERVERKIT_DIR"
BASE_NAME="$(basename "$INSTALL_DIR")"
BASE_DIR="$(dirname "$INSTALL_DIR")"
DIR_A="$BASE_DIR/${BASE_NAME}-a"
DIR_B="$BASE_DIR/${BASE_NAME}-b"
VENV_DIR="${SERVERKIT_VENV_DIR:-$INSTALL_DIR/venv}"
BACKUP_DIR="${SERVERKIT_BACKUP_DIR:-/var/backups/serverkit}"
LOG_DIR="/var/log/serverkit"
CONFIG_DIR="${SERVERKIT_CONFIG_DIR:-/etc/serverkit}"
LOCK_FILE="${SERVERKIT_LOCK_FILE:-/var/lock/serverkit-update.lock}"

# System integration dirs — overridable so the config-refresh logic can be
# exercised against fixtures in tests instead of the host's real /etc.
NGINX_DIR="${SERVERKIT_NGINX_DIR:-/etc/nginx}"
LETSENCRYPT_DIR="${SERVERKIT_LETSENCRYPT_DIR:-/etc/letsencrypt}"
SYSTEMD_DIR="${SERVERKIT_SYSTEMD_DIR:-/etc/systemd/system}"
# Per-app nginx location snippets — one proxy_pass to each managed app's
# container. The updater probes these to prove apps stayed up across the switch.
APP_LOCATIONS_DIR="${SERVERKIT_APP_LOCATIONS_DIR:-/etc/nginx/serverkit-locations}"

GITHUB_REPO="${GITHUB_REPO:-jhd3197/ServerKit}"
SERVERKIT_OFFLINE_TARBALL="${SERVERKIT_OFFLINE_TARBALL:-}"
SERVERKIT_MIRROR_URL="${SERVERKIT_MIRROR_URL:-}"
BACKEND_SERVICE="serverkit"

# ---------------------------------------------------------------------------
# Terminal styling
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    ESC=$'\033'
    RST="${ESC}[0m"; BLD="${ESC}[1m"
    paint() { printf '%s[38;2;%d;%d;%dm' "$ESC" "$1" "$2" "$3"; }
else
    RST=''; BLD=''
    paint() { :; }
fi

V3="$(paint 139 92 246)"; V4="$(paint 124 58 237)"
PAPER="$(paint 237 233 254)"; FOG="$(paint 113 108 140)"
HUE_OK="$(paint 52 211 153)"; HUE_WARN="$(paint 250 204 21)"
HUE_ERR="$(paint 248 113 113)"; HUE_LINK="$(paint 103 232 249)"

good()  { printf '  %s✔%s %s\n' "$HUE_OK"   "$RST" "$1"; }
warn()  { printf '  %s▴%s %s\n' "$HUE_WARN" "$RST" "$1"; }
# Every hard failure points at the full log so a stuck box is debuggable from a
# single line (UPDATE_LOG is empty until init_logging runs, hence the guard).
halt()  {
    printf '  %s✘%s %s\n' "$HUE_ERR"  "$RST" "$1" >&2
    [ -n "${UPDATE_LOG:-}" ] && printf '  %sfull log: %s%s\n' "$FOG" "$UPDATE_LOG" "$RST" >&2
    exit 1
}
step()  { printf '  %s❯%s %s\n' "$HUE_LINK" "$RST" "$1"; }
info()  { printf '  %s•%s %s\n' "$FOG"      "$RST" "$1"; }

STARTED_AT=0
PHASE_N=0
clock() {
    [ "$STARTED_AT" -gt 0 ] || { printf ''; return; }
    local secs=$(( $(date +%s) - STARTED_AT ))
    printf '%dm %02ds' "$((secs / 60))" "$((secs % 60))"
}
LAST_PHASE="startup"
phase() {
    PHASE_N=$((PHASE_N + 1))
    LAST_PHASE="$1"
    printf '\n  %s%s%02d%s  %s%s%s  %s%s%s\n' \
        "$BLD" "$V3" "$PHASE_N" "$RST" "$BLD" "$1" "$RST" "$FOG" "$(clock)" "$RST"
    printf '  %s%s%s\n\n' "$V4" "──────────────────────────────────────" "$RST"
}

# ---------------------------------------------------------------------------
# L5 — Loud failures + always-on logging
# ---------------------------------------------------------------------------
# `set -euo pipefail` makes any unguarded command abort the script. Without this
# layer that abort is *silent* — you just land back at the prompt with no idea
# which phase, line, or command died (exactly the failure mode that made this
# updater so hard to debug). init_logging mirrors everything to a timestamped
# log; the ERR trap turns every abort into a labelled, actionable message.
UPDATE_LOG=""
init_logging() {
    [ -n "${SERVERKIT_NO_LOG:-}" ] && return 0
    mkdir -p "$LOG_DIR" 2>/dev/null || true
    if [ -d "$LOG_DIR" ] && [ -w "$LOG_DIR" ]; then
        UPDATE_LOG="$LOG_DIR/update-$(date +%Y%m%d-%H%M%S).log"
        # Keep output on the terminal *and* append it to the log. Colors were
        # already resolved above from the real TTY, so they survive the pipe.
        exec > >(tee -a "$UPDATE_LOG") 2>&1
        info "Logging to $UPDATE_LOG"
    fi
}

report_failure() {
    local rc=$1 line=$2 cmd=$3
    printf '\n  %s✘  Update aborted%s during %s%s%s\n' \
        "$HUE_ERR" "$RST" "$BLD" "$LAST_PHASE" "$RST" >&2
    printf '     %sexit %s · line %s · %s%s\n' "$FOG" "$rc" "$line" "$cmd" "$RST" >&2
    # `if`, not a trailing `&&` list: with UPDATE_LOG empty (fresh box before
    # init_logging, SERVERKIT_NO_LOG=1) the list form returns 1 from the
    # reporter itself — the same species as the July 2 outage.
    if [ -n "$UPDATE_LOG" ]; then
        printf '     %sfull log: %s%s\n' "$FOG" "$UPDATE_LOG" "$RST" >&2
    fi
}

# ---------------------------------------------------------------------------
# Self-updating bootstrap + run lock
# ---------------------------------------------------------------------------
# The updater that runs is whatever is installed on the box — so a *stale*
# update.sh (one predating a bug fix or a new deployment shape) fails in ways
# the current code already handles, and it can't fix itself. This is the exact
# trap that left a box stuck: an old updater died before it could install the
# fixed one. Before doing any work, fetch the newest update.sh for the target
# ref and re-exec into it. From this version on, "just run serverkit update"
# is reliable no matter how old the box is.
#
# CHANNEL DECISION (2026-07, scripts-reliability round 2): stable installs keep
# fetching this file from *main* — no release buffer. The flip side is that any
# updater bug merged to main is live on every box instantly, so merges touching
# scripts/** are gated by the real install+update e2e job in scripts-ci.yml
# (update-e2e). Fetching from the latest release tag was considered and
# rejected: a broken *released* updater would need a whole new release to fix,
# while main + a CI gate keeps the one-push-fixes-the-fleet property that
# resolved the 2026-07-02 incident.
SELF_PATH="${BASH_SOURCE[0]}"
maybe_reexec_latest_updater() {
    [ -n "${SERVERKIT_UPDATER_REEXECED:-}" ] && return 0    # already the latest
    [ "${SERVERKIT_NO_SELF_UPDATE:-0}" = "1" ] && return 0  # opt-out / tests
    [ "$DRY_RUN" = "1" ] && return 0
    [ -n "$SERVERKIT_OFFLINE_TARBALL" ] && return 0         # offline: nothing to fetch
    command -v curl &>/dev/null || return 0

    local ref="main"
    [ -n "$TARGET_BRANCH" ] && ref="$TARGET_BRANCH"
    [ "$USE_RELEASE" = "1" ] && [ -n "$RELEASE_VERSION" ] && ref="$RELEASE_VERSION"

    local url tmp
    url="https://raw.githubusercontent.com/${GITHUB_REPO}/${ref}/scripts/update.sh"
    tmp="$(mktemp)"
    if ! curl -fsSL --max-time 20 "$url" -o "$tmp" 2>/dev/null || [ ! -s "$tmp" ]; then
        rm -f "$tmp"; return 0                               # fetch failed → use local
    fi
    # Swap only if it genuinely differs AND is valid bash (never re-exec into a
    # truncated/corrupt download).
    if ! cmp -s "$tmp" "$SELF_PATH" && bash -n "$tmp" 2>/dev/null; then
        info "Refreshing the updater itself from ${ref}..."
        chmod +x "$tmp" 2>/dev/null || true
        # ${arr[@]+...}: expanding an EMPTY array as "${arr[@]}" is an
        # unbound-variable error under set -u on bash < 4.4 (older distros).
        SERVERKIT_UPDATER_REEXECED=1 exec bash "$tmp" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
    fi
    rm -f "$tmp"
}

# Stop two concurrent updates clobbering each other (e.g. both cloning into the
# same blue/green slot, or racing docker compose). The lock auto-releases when
# the script exits, since the fd closes.
acquire_update_lock() {
    [ "$DRY_RUN" = "1" ] && return 0
    command -v flock &>/dev/null || return 0
    mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
    # Brace-group so a failed open (unwritable path) is caught here instead of
    # the redirection error aborting the whole script — then just skip locking.
    { exec 9>"$LOCK_FILE"; } 2>/dev/null || return 0
    if ! flock -n 9; then
        halt "Another 'serverkit update' is already running (lock: $LOCK_FILE)."
    fi
}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
run_or_dry() {
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would run: $*"
    else
        "$@"
    fi
}

# Like run_or_dry, but runs the command inside <dir>. Under --dry-run it reports
# the working directory it *would* run in instead of silently dropping the cd —
# a chained `run_or_dry cd X && run_or_dry docker compose ...` used to print the
# compose command with no hint of where it would execute.
run_in_dir() {
    local dir="$1"; shift
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] (cwd: $dir) would run: $*"
    else
        ( cd "$dir" && "$@" )
    fi
}

# ---------------------------------------------------------------------------
# Version comparison — skip the whole update when already current.
# ---------------------------------------------------------------------------
local_version() {
    tr -d '\n\r ' < "$INSTALL_DIR/VERSION" 2>/dev/null || true
}

# True when two version strings match ignoring a leading "v" (1.7.1 == v1.7.1).
versions_equal() {
    [ "${1#v}" = "${2#v}" ]
}

# Resolve the release tag we would update to (pinned, or the latest on GitHub).
remote_release_tag() {
    if [ -n "$RELEASE_VERSION" ]; then
        printf '%s' "$RELEASE_VERSION"
        return 0
    fi
    curl -sf "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" 2>/dev/null \
        | grep '"tag_name"' | head -1 | cut -d'"' -f4 || true
}

# Return 0 when the install is already at the target version/ref (the caller
# should then skip). Best-effort: --force always proceeds; offline always
# proceeds (can't compare); an indeterminate comparison proceeds with the
# update rather than wrongly blocking it.
is_already_current() {
    [ "$FORCE_UPDATE" = "1" ] && return 1
    [ -n "$SERVERKIT_OFFLINE_TARBALL" ] && return 1

    if [ "$USE_RELEASE" = "1" ]; then
        local target local_v
        target="$(remote_release_tag)"
        [ -n "$target" ] || return 1
        local_v="$(local_version)"
        [ -n "$local_v" ] || return 1
        versions_equal "$local_v" "$target" && return 0
        return 1
    fi

    # Branch / main mode: compare the local checkout's HEAD against the remote
    # branch HEAD. ls-remote needs no fetch and works on shallow clones.
    local branch local_head remote_head
    branch="${TARGET_BRANCH:-main}"
    git -C "$INSTALL_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 1
    local_head="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || true)"
    remote_head="$(git -C "$INSTALL_DIR" ls-remote "https://github.com/${GITHUB_REPO}.git" \
        "refs/heads/$branch" 2>/dev/null | awk '{print $1}' | head -1)"
    [ -n "$local_head" ] && [ -n "$remote_head" ] && [ "$local_head" = "$remote_head" ] && return 0
    return 1
}

# If already current, announce it and exit cleanly (unless --force).
version_gate() {
    if is_already_current; then
        local v; v="$(local_version)"
        good "Already up to date (version ${v:-unknown}). Use --force to update anyway."
        exit 0
    fi
}

wait_for_service() {
    local unit="$1" target_state="$2" timeout="${3:-30}"
    local waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if systemctl is-active --quiet "$unit" 2>/dev/null; then
            [ "$target_state" = "active" ] && return 0
        else
            [ "$target_state" = "inactive" ] && return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

# Apply the (possibly refreshed) nginx config WITHOUT dropping traffic.
#
# Host nginx is the front door for every managed app — it reverse-proxies their
# containers via /etc/nginx/serverkit-locations/*.conf. Stopping it (the old
# behaviour) blacked out every hosted site for the whole switch window, so a
# panel update became an outage for unrelated apps. `nginx -s reload` keeps the
# listening sockets and in-flight connections alive while swapping config, so
# apps never blink. The updater only ever changes the *config* (refresh_config),
# never anything nginx serves from the blue/green slot, so a reload is all that
# is ever needed. If nginx happens to be down, start it instead.
reload_nginx_graceful() {
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would reload nginx (graceful, zero-downtime)"
        return 0
    fi
    if systemctl is-active --quiet nginx 2>/dev/null; then
        # Validate before reloading: a bad config would otherwise leave nginx
        # running the OLD config (reload is rejected) — non-fatal, but warn loudly.
        if nginx -t >/dev/null 2>&1; then
            systemctl reload nginx 2>/dev/null \
                || run_or_dry systemctl reload nginx \
                || warn "nginx reload failed — apps keep serving the previous config"
        else
            warn "nginx config test failed — skipping reload (apps keep serving the previous config)"
        fi
    else
        warn "nginx was not running — starting it"
        # Never propagate a failed start: nginx being down is a pre-existing
        # condition, and aborting here (post-switch) would fire a rollback that
        # cannot fix nginx either — warn and let the operator handle it.
        run_or_dry systemctl start nginx \
            || warn "nginx failed to start — check 'systemctl status nginx'"
        wait_for_service nginx active 15 || warn "nginx did not report active within 15 seconds"
    fi
}

# Label a slot's SPA bundle for SELinux-enforcing hosts so nginx can read the
# static panel assets it now serves (see install.sh selinux_label_frontend_dist).
# Best-effort and a no-op on permissive/disabled boxes or where the tools are
# absent. Takes the slot's real path (blue/green resolves through a symlink).
selinux_label_dist() {
    local dir="$1"
    [ "$DRY_RUN" = "1" ] && return 0
    command -v selinuxenabled &>/dev/null && selinuxenabled 2>/dev/null || return 0
    local dist="${dir}/frontend/dist"
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
# Managed-app uptime verification
# ---------------------------------------------------------------------------
# The whole point of the reload-not-stop discipline is that hosted apps never go
# down when the panel updates. These helpers turn that promise into something the
# updater actually checks: snapshot which app upstreams are reachable before the
# switch, re-probe after the reload, and shout if any app that WAS serving has
# stopped answering.

# Discover the upstreams (host:port) host nginx reverse-proxies for managed apps
# by scanning the per-app location snippets. One unique "host:port" per line.
discover_app_upstreams() {
    [ -d "$APP_LOCATIONS_DIR" ] || return 0
    grep -rhoE 'proxy_pass[[:space:]]+https?://127\.0\.0\.1:[0-9]+' "$APP_LOCATIONS_DIR" 2>/dev/null \
        | grep -oE '127\.0\.0\.1:[0-9]+' | sort -u || true
}

# Probe each upstream on stdin; emit "host:port up" / "host:port down". "up" means
# the app container answered at all (ANY HTTP status — a 4xx/5xx app is still
# reachable; a refused/timed-out connection is not), which is exactly what nginx
# needs to keep fronting it.
probe_app_upstreams() {
    local up
    while IFS= read -r up; do
        [ -n "$up" ] || continue
        if curl -s -o /dev/null --max-time 4 "http://$up/" 2>/dev/null; then
            printf '%s up\n' "$up"
        else
            printf '%s down\n' "$up"
        fi
    done
}

# A reachability snapshot for every fronted app: "host:port state" lines.
snapshot_app_reachability() {
    [ "$DRY_RUN" = "1" ] && return 0
    discover_app_upstreams | probe_app_upstreams
}

# Compare a pre-update snapshot with a post-update one. Warn for any app that was
# reachable before and is not now (the update disrupted a workload). Non-zero if
# any regression is found; an app that was already down before the update is not
# counted against us.
report_app_uptime_regressions() {
    local before="$1" after="$2" regressed=0 total=0 line up state
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        up="${line% *}"; state="${line##* }"
        total=$((total + 1))
        [ "$state" = "up" ] || continue
        if printf '%s\n' "$after" | grep -qx "$up down"; then
            warn "App upstream $up was reachable before the update but is DOWN now"
            regressed=$((regressed + 1))
        fi
    done <<EOF
$before
EOF
    if [ "$total" -eq 0 ]; then
        info "No managed apps fronted by nginx — nothing to verify"
    elif [ "$regressed" -eq 0 ]; then
        good "All $total managed app(s) stayed reachable across the update"
    else
        warn "$regressed of $total managed app(s) went down across the update"
    fi
    [ "$regressed" -eq 0 ]
}

# Resolve the currently active real directory behind the symlink.
active_real_dir() {
    if [ -L "$INSTALL_DIR" ]; then
        readlink -f "$INSTALL_DIR"
    elif [ -d "$INSTALL_DIR" ]; then
        echo "$INSTALL_DIR"
    else
        echo ""
    fi
}

# Return the inactive blue/green directory.
next_real_dir() {
    local active
    active="$(active_real_dir)"
    if [ "$active" = "$DIR_A" ]; then
        echo "$DIR_B"
    else
        echo "$DIR_A"
    fi
}

# Read the install profile recorded by install.sh in the active slot's .env.
# Anything unrecognised — including every install predating profiles — reports
# "standard", which is exactly what those installs are.
install_profile() {
    local active env_file profile=""
    active="$(active_real_dir)"
    if [ -n "$active" ]; then
        env_file="$active/.env"
        if [ -f "$env_file" ]; then
            profile="$(grep -E '^SERVERKIT_PROFILE=' "$env_file" 2>/dev/null \
                       | head -1 | cut -d= -f2- | tr -d ' \r')"
        fi
    fi
    case "$profile" in
        minimal|standard|full) printf '%s' "$profile" ;;
        *)                     printf 'standard' ;;
    esac
}

# Whether this update has to insist on Docker. The minimal profile ships
# without it on purpose, so demanding it here would make every minimal box
# installable but un-updatable. Two escape hatches keep that honest: an
# all-Docker deployment obviously needs it, and if Docker is present at all we
# still validate compose so a half-provisioned box is caught early.
# Split out so the unit tests can redefine it. Restricting PATH to hide docker
# would also hide the coreutils install_profile parses with, and the test would
# then pass for the wrong reason.
have_docker() { command -v docker &>/dev/null; }

update_needs_docker() {
    [ "$(install_profile)" = "minimal" ] || return 0
    is_docker_deployment && return 0
    have_docker && return 0
    return 1
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
preflight_check() {
    phase "Pre-flight Checks"

    # Root
    if [ "$EUID" -ne 0 ]; then
        halt "Run as root."
    fi
    good "Running as root"

    # Install directory exists or can be created
    if [ ! -e "$BASE_DIR" ]; then
        halt "Base directory $BASE_DIR does not exist."
    fi
    good "Install base directory exists"

    # Python version
    local py_bin py_version
    py_bin="$(locate_python 2>/dev/null || true)"
    if [ -z "$py_bin" ]; then
        halt "Python 3.11-3.14 is required. Install a supported python3.1x and python3-venv."
    fi
    py_version="$($py_bin -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
    good "Python $py_version available ($py_bin)"

    # Required commands. Source-mode updates build the SPA on the host, so npm
    # must be checked up front — discovering it missing mid-update would abort
    # after the new slot is already half-built. Release tarballs ship a
    # prebuilt dist and stay npm-free.
    local cmd missing=() required=(git curl tar rsync systemctl nginx python3)
    [ "$USE_RELEASE" = "1" ] || required+=(npm)
    # Docker is conditional — see update_needs_docker().
    if update_needs_docker; then
        required+=(docker)
    fi
    for cmd in "${required[@]}"; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if update_needs_docker; then
        if ! docker compose version &>/dev/null && ! docker-compose --version &>/dev/null; then
            missing+=("docker compose")
        fi
    fi
    if [ ${#missing[@]} -gt 0 ]; then
        halt "Missing required tools: ${missing[*]}"
    fi
    good "Required tools available"
    if ! update_needs_docker; then
        good "Docker not required (minimal profile)"
    fi

    # Disk space.
    #
    # A fixed 2 GiB floor is not enough on its own: this update is about to
    # write TWO full copies of the database (a pre-upgrade snapshot and a tree
    # backup that contains it) plus a release tree. With an 800 MB database
    # that is ~2 GiB of new files, so a box with 2.1 GiB free passes the old
    # check and then fills the disk mid-update. Size the requirement to what
    # will actually be written, and reclaim stale snapshots before giving up.
    local avail_kb need_kb db_kb active_dir
    # -P (POSIX format) pins each filesystem to one line — a long device name
    # otherwise wraps and shifts the column the awk parse reads.
    avail_kb="$(df -Pk "$BASE_DIR" | awk 'NR==2 {print $4}')"
    active_dir="$(active_real_dir 2>/dev/null || echo "$INSTALL_DIR")"
    db_kb="$(du -sk "$active_dir/backend/instance/serverkit.db" 2>/dev/null | awk '{print $1}')"
    [ -n "$db_kb" ] || db_kb=0
    # 2 GiB floor + room for both database copies.
    need_kb=$(( 2 * 1024 * 1024 + db_kb * 2 ))

    if [ "$avail_kb" -lt "$need_kb" ]; then
        warn "Low disk: $((avail_kb / 1024)) MiB free, need $((need_kb / 1024)) MiB — trimming old backups"
        trim_backups
        enforce_backup_budget
        avail_kb="$(df -Pk "$BASE_DIR" | awk 'NR==2 {print $4}')"
    fi

    if [ "$avail_kb" -lt "$need_kb" ]; then
        halt "Insufficient disk space on $BASE_DIR: $((avail_kb / 1024)) MiB free, need $((need_kb / 1024)) MiB (2 GiB + two $((db_kb / 1024)) MiB database copies). Run 'sudo serverkit disk' to reclaim space."
    fi
    good "Disk space OK ($((avail_kb / 1024 / 1024)) GiB free, need $((need_kb / 1024 / 1024)) GiB)"

    # Memory (need 512 MiB free)
    local mem_avail_mb
    mem_avail_mb="$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
    if [ "$mem_avail_mb" -lt 512 ]; then
        warn "Low memory: ${mem_avail_mb} MiB available (recommend >= 512 MiB)."
    else
        good "Memory OK (${mem_avail_mb} MiB available)"
    fi

    # Network reachability (skip in offline mode)
    if [ -n "$SERVERKIT_OFFLINE_TARBALL" ]; then
        info "Offline tarball set; skipping network checks"
    elif [ "$USE_RELEASE" = "1" ] || [ -z "$TARGET_BRANCH" ]; then
        if ! curl -sfI "https://github.com" >/dev/null 2>&1; then
            halt "Cannot reach github.com. Set SERVERKIT_OFFLINE_TARBALL or fix network."
        fi
        good "Network reachability OK"
    fi

    # Current backend health (warn only; it may already be down)
    if curl -sf --max-time 5 http://127.0.0.1:5000/api/v1/system/health >/dev/null 2>&1; then
        good "Backend currently healthy"
    else
        warn "Backend is not currently responding on :5000 — will proceed anyway"
    fi
}

# ---------------------------------------------------------------------------
# Python virtual environment
# ---------------------------------------------------------------------------
locate_python() {
    local c v
    for c in python3.14 python3.13 python3.12 python3.11 python3; do
        if command -v "$c" &>/dev/null; then
            v="$($c -c 'import sys;print(".".join(map(str,sys.version_info[:2])))' 2>/dev/null || true)"
            # An EMPTY version means the binary/shim exists but can't actually
            # execute Python — and an empty string would pass the sort checks
            # below ("" sorts before everything). Reject it and move on.
            [ -n "$v" ] || continue
            if printf '%s\n%s' "3.11" "$v" | sort -C -V && \
               printf '%s\n%s' "$v" "3.14" | sort -C -V; then
                printf '%s' "$c"
                return 0
            fi
        fi
    done
    return 1
}

rebuild_virtualenv() {
    local target_dir="$1"
    step "Rebuilding Python virtual environment..."

    local py_bin
    py_bin="$(locate_python)" || halt "ServerKit requires Python 3.11-3.14."

    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would recreate venv at $target_dir using $py_bin"
        return 0
    fi

    rm -rf "$target_dir"
    "$py_bin" -m venv "$target_dir"
    # shellcheck source=/dev/null
    source "$target_dir/bin/activate"
    pip install --upgrade pip --quiet
    pip install -r "$target_dir/../backend/requirements.txt" --quiet
    pip install gunicorn gevent gevent-websocket --quiet
    good "Virtual environment rebuilt at $target_dir"
}

# Ensure the target directory has a usable venv. If a pre-built one exists AND
# is bound to this slot's path, use it; otherwise rebuild from requirements.
require_venv() {
    local target_dir="$1"
    if [ -f "$target_dir/bin/activate" ] && [ -x "$target_dir/bin/python" ]; then
        # A venv is path-bound: bin/activate and every entry-point shebang
        # embed the absolute path the venv was created at. The release
        # tarball's prebuilt venv is baked as $INSTALL_DIR/venv, which —
        # until atomic_switch flips the symlink — resolves to the OLD slot.
        # Running it would execute the new code with the old slot's
        # dependencies (and run the OLD flask binary for migrations), so a
        # venv bound to any other path must be rebuilt in place.
        local bound_path resolved_bound resolved_target
        # Matches both VIRTUAL_ENV="/path" (classic venv) and the unquoted
        # export VIRTUAL_ENV=/path form; skips cygpath-style indirection.
        bound_path="$(grep -m1 -oE 'VIRTUAL_ENV="?/[^")]+"?' "$target_dir/bin/activate" | cut -d= -f2- | tr -d '"')"
        # Compare RESOLVED paths: on a fresh install the symlink already points
        # at the only slot, so the baked path resolves to the target and the
        # prebuilt venv is genuinely usable (fast path). During an update it
        # resolves to the OLD slot — that mismatch triggers the rebuild.
        resolved_bound="$(readlink -f "$bound_path" 2>/dev/null || true)"
        resolved_target="$(readlink -f "$target_dir" 2>/dev/null || echo "$target_dir")"
        if [ -n "$resolved_bound" ] && [ "$resolved_bound" = "$resolved_target" ]; then
            good "Virtual environment ready at $target_dir"
            return 0
        fi
        warn "Virtual environment at $target_dir is bound to '${bound_path:-unknown path}' — rebuilding for this slot"
        rebuild_virtualenv "$target_dir"
        return 0
    fi
    warn "Virtual environment missing at $target_dir"
    rebuild_virtualenv "$target_dir"
}

# ---------------------------------------------------------------------------
# Database migration
# ---------------------------------------------------------------------------
migrate_database() {
    local work_dir="$1"
    local venv="$work_dir/venv"

    step "Running database migrations..."
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would run flask db upgrade in $work_dir/backend"
        return 0
    fi

    # For SQLite installs, migrate the COPY we placed in the *new* slot, not the
    # database the .env points at via the /opt/serverkit symlink (which still
    # resolves to the live OLD slot until atomic_switch). Touching the old slot's
    # DB here would corrupt rollback: reverting the symlink would hand the old
    # code a database already upgraded to the new schema. Pointing the migration
    # at the slot-absolute path keeps the old slot's DB byte-for-byte intact.
    #
    # Subshell: the venv activation and the cd must not leak into the main
    # shell — the rest of the update keeps running from the caller's cwd.
    local slot_db="$work_dir/backend/instance/serverkit.db"
    local use_slot_db=0
    if grep -qE '^DATABASE_URL=sqlite' "$work_dir/.env" 2>/dev/null && [ -f "$slot_db" ]; then
        use_slot_db=1
        # Fail fast with an actionable message when the SQLite slot copy is
        # corrupt, instead of dying deep inside app boot with "database disk
        # image is malformed". The sqlite3 CLI isn't guaranteed to be
        # installed — probe via the venv's Python instead. An EMPTY result
        # means the probe itself couldn't run (no usable venv python) — that
        # is not proof of corruption, so warn and let `flask db upgrade`
        # surface any real problem rather than halting on a false positive.
        local integrity
        integrity="$("$venv/bin/python" -c \
            "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" \
            "$slot_db" 2>/dev/null || true)"
        if [ -n "$integrity" ] && [ "$integrity" != "ok" ]; then
            halt "SQLite database is corrupt ($integrity). Repair or restore it from $BACKUP_DIR and re-run the update. The previous installation is still active."
        elif [ -z "$integrity" ]; then
            warn "Could not pre-check SQLite integrity (venv python unavailable) — proceeding"
        fi
    fi
    if ! (
        # shellcheck source=/dev/null
        source "$venv/bin/activate"
        cd "$work_dir/backend"
        # SERVERKIT_SKIP_BACKGROUND=1: create_app() must not start queue
        # consumers / schedulers / collectors while we only want migrations —
        # they query the DB before `flask db upgrade` runs, and any error
        # there aborts the migration (belt & braces; the app also detects the
        # flask CLI on its own).
        if [ "$use_slot_db" = "1" ]; then
            DATABASE_URL="sqlite:///$slot_db" FLASK_ENV=production SERVERKIT_SKIP_BACKGROUND=1 flask db upgrade
        else
            # Non-SQLite (e.g. PostgreSQL): the DB is shared/external, so there
            # is no per-slot copy to isolate — migrate it directly.
            FLASK_ENV=production SERVERKIT_SKIP_BACKGROUND=1 flask db upgrade
        fi
    ); then
        # PostgreSQL (and other external DBs): the migration ran IN PLACE
        # against the SHARED database — there is no untouched slot copy to
        # fall back on. Put the database back the way we found it using the
        # verified pre-upgrade dump before reporting the failure.
        if [ "$use_slot_db" != "1" ] && [ -n "${PRE_UPGRADE_DUMP:-}" ] && [ -f "${PRE_UPGRADE_DUMP:-}" ]; then
            warn "Migration failed — restoring the pre-upgrade dump into the shared database..."
            local pg_url
            pg_url="$(pg_url_from_env "$work_dir")"
            if command -v pg_restore >/dev/null 2>&1 \
               && pg_restore --clean --if-exists -d "$pg_url" "$PRE_UPGRADE_DUMP"; then
                halt "Database migration failed; the pre-upgrade dump was restored, so the database is back to its pre-update state. The previous installation is still active."
            fi
            halt "Database migration failed AND the automatic restore failed. Restore MANUALLY with: pg_restore --clean --if-exists -d '$pg_url' '$PRE_UPGRADE_DUMP' — the previous installation is still active."
        fi
        halt "Database migration failed. The previous installation is still active."
    fi
    good "Database migrated"
}

# ---------------------------------------------------------------------------
# Release download + checksum verification
# ---------------------------------------------------------------------------
# Prints exactly ONE line on stdout: the path to the verified tarball. The
# caller captures that stdout (tarball="$(download_release ...)"), so every
# bit of progress in here is routed to stderr — a step/good line on stdout
# would be captured into the "path" and break the tar that follows. stderr
# still reaches the terminal and the log (init_logging merges 2>&1 into tee).
download_release() {
    local version="$1"
    local arch output tmp_dir base_url checksum_url tarball_url

    case "$(uname -m)" in
        x86_64|amd64)  arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        *)             halt "Unsupported architecture: $(uname -m)" ;;
    esac

    if [ -n "$SERVERKIT_OFFLINE_TARBALL" ]; then
        [ -f "$SERVERKIT_OFFLINE_TARBALL" ] || halt "Offline tarball not found: $SERVERKIT_OFFLINE_TARBALL"
        echo "$SERVERKIT_OFFLINE_TARBALL"
        return 0
    fi

    if [ -n "$SERVERKIT_MIRROR_URL" ]; then
        base_url="$SERVERKIT_MIRROR_URL"
    else
        base_url="https://github.com/${GITHUB_REPO}/releases/download/${version}"
    fi

    tarball_url="${base_url}/serverkit-${version}-linux-${arch}.tar.gz"
    checksum_url="${base_url}/checksums.txt"
    tmp_dir="$(mktemp -d)"
    output="$tmp_dir/serverkit-${version}-linux-${arch}.tar.gz"

    # This runs in a command substitution, so the caller never learns tmp_dir
    # on failure: remove it here before halting, or every failed download
    # strands a directory in /tmp.
    step "Downloading release tarball (${arch})..." >&2
    if ! curl -sfL "$tarball_url" -o "$output"; then
        rm -rf "$tmp_dir"
        halt "Failed to download $tarball_url"
    fi

    step "Verifying checksum..." >&2
    if curl -sfL "$checksum_url" -o "$tmp_dir/checksums.txt"; then
        cd "$tmp_dir"
        if ! sha256sum -c <(grep "serverkit-${version}-linux-${arch}.tar.gz" checksums.txt) >/dev/null 2>&1; then
            cd /
            rm -rf "$tmp_dir"
            halt "Checksum verification failed for release tarball."
        fi
        good "Checksum verified" >&2
    else
        warn "Could not download checksums.txt — skipping verification" >&2
    fi

    echo "$output"
}

# Remove release downloads that earlier updates left in the temp dir (before
# the download dir was cleaned up, every update stranded ~70 MB there). Only
# directories holding a release tarball and untouched for an hour are taken,
# so a concurrent download is never pulled out from under itself.
sweep_stale_downloads() {
    local tarball
    while IFS= read -r tarball; do
        [ -n "$tarball" ] || continue
        rm -rf "$(dirname "$tarball")" 2>/dev/null || true
    done < <(find "${TMPDIR:-/tmp}" -mindepth 2 -maxdepth 2 -path '*/tmp.*/serverkit-*-linux-*.tar.gz' \
                 -mmin +60 2>/dev/null || true)
    return 0
}

# ---------------------------------------------------------------------------
# Blue/green directory management
# ---------------------------------------------------------------------------
ensure_bluegreen_layout() {
    # Convert legacy single-directory installs into the blue/green symlink layout.
    if [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ]; then
        step "Migrating to blue/green layout..."
        if [ "$DRY_RUN" = "1" ]; then
            info "[dry-run] would move $INSTALL_DIR → $DIR_A and symlink $INSTALL_DIR → $DIR_A"
            return 0
        fi
        mv "$INSTALL_DIR" "$DIR_A"
        ln -s "$DIR_A" "$INSTALL_DIR"
        good "Migrated to blue/green layout"
    fi

    # Ensure both slots exist.
    if [ "$DRY_RUN" = "0" ]; then
        mkdir -p "$DIR_A" "$DIR_B"
    fi
}

atomic_switch() {
    local target="$1"
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would switch symlink $INSTALL_DIR → $target"
        return 0
    fi
    ln -sfn "$target" "${INSTALL_DIR}.tmp"
    mv -Tf "${INSTALL_DIR}.tmp" "$INSTALL_DIR"
    good "Switched active install to $target"
}

# ---------------------------------------------------------------------------
# Consistent SQLite copy. A plain `cp` of the LIVE panel database races the
# backend's own writes: the copy can capture half a transaction and come out
# "database disk image is malformed" — which then sinks the migration in the
# new slot, and silently poisons the pre-upgrade backup that was supposed to
# be the safety net. The SQLite online backup API snapshots a consistent DB
# even while the panel is writing, using any interpreter that can import
# sqlite3; with none available we fail rather than fall back to a racy cp.
# ---------------------------------------------------------------------------
# Snapshotting or integrity-checking a SQLite file needs nothing more than an
# interpreter carrying the stdlib `sqlite3` module — NOT the gated 3.11-3.14
# that rebuild_virtualenv and preflight_check legitimately demand. Reusing
# locate_python here silently coupled the DATA-safety paths to the venv-builder
# constraint, and blocked updates for a reason unrelated to the data:
#   * an all-Docker install returns before preflight_check ever runs and does
#     all its work in the container, yet backup_docker_db verified the extracted
#     DB on the HOST — so a stock EL9 (python3.9) or Ubuntu 22.04 (python3.10)
#     host halted with "Could not obtain a VALID database backup", blaming the
#     backup for a missing interpreter.
#   * backup_current halts when it cannot snapshot, so the same boxes could
#     never update at all.
# Prefer the install's own venv interpreter: on a real box it is guaranteed
# present, guaranteed to have the module, and independent of what is on PATH.
locate_sqlite_python() {
    local c
    for c in "$@" python3.14 python3.13 python3.12 python3.11 python3 python; do
        [ -n "$c" ] || continue
        command -v "$c" >/dev/null 2>&1 || continue
        "$c" -c 'import sqlite3' >/dev/null 2>&1 || continue
        printf '%s' "$c"
        return 0
    done
    return 1
}

copy_sqlite_db() {
    local src="$1" dest="$2" py slot
    # The DB lives at <slot>/backend/instance/serverkit.db — derive that slot's
    # interpreter as the preferred candidate. A src outside a slot leaves the
    # path unchanged, yielding a non-executable candidate that is simply skipped.
    slot="${src%/backend/instance/*}"
    py="$(locate_sqlite_python "$slot/venv/bin/python")" \
        || { warn "No Python with the sqlite3 module available for a consistent DB snapshot"; return 1; }
    "$py" - "$src" "$dest" <<'PYEOF'
import sqlite3, sys
src, dest = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src)
d = sqlite3.connect(dest)
s.backup(d)
d.close()
s.close()
PYEOF
}

# A backup that was never verified is a hope, not a safety net. Verify every
# artifact BEFORE it is allowed to justify proceeding with the update.
verify_sqlite_db() {
    local db="$1" py active hint=""
    [ -s "$db" ] || return 1
    # Backups live in BACKUP_DIR, outside any slot, so hint at the active
    # install's interpreter instead of deriving one from the file's path.
    active="$(active_real_dir 2>/dev/null || true)"
    if [ -n "$active" ]; then
        hint="$active/venv/bin/python"
    fi
    py="$(locate_sqlite_python "$hint")" || return 1
    [ "$("$py" -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" "$db" 2>/dev/null || true)" = "ok" ]
}
verify_pg_dump() {
    local dump="$1"
    [ -s "$dump" ] || return 1
    # Structural validation when pg_restore is available: list the dump's TOC.
    if command -v pg_restore >/dev/null 2>&1; then
        pg_restore -l "$dump" >/dev/null 2>&1 || return 1
    fi
    return 0
}

# Extract the DATABASE_URL from a slot's .env, normalized for libpq: strip
# SQLAlchemy driver suffixes (postgresql+psycopg2://) and wrapping quotes.
pg_url_from_env() {
    grep -E '^DATABASE_URL=' "$1/.env" 2>/dev/null | head -1 | cut -d= -f2- \
        | tr -d "\"'" | sed -E 's|^postgres(ql)?\+[a-z0-9]+://|postgresql://|'
}

# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
snapshot_install_tree() {
    local source_dir="$1" target_dir="$2"
    rsync -a --exclude=venv --exclude=backups --exclude=node_modules \
        --exclude='backend/instance/*.db' \
        --exclude='backend/instance/*.db-wal' \
        --exclude='backend/instance/*.db-shm' \
        "$source_dir/" "$target_dir/" 2>/dev/null && return 0
    # Preserve the exclusions even on hosts without rsync; never stage a
    # second database copy just to delete it after copying the tree.
    mkdir -p "$target_dir" || return 1
    (set -o pipefail
        tar -C "$source_dir" --exclude=venv --exclude=backups --exclude=node_modules \
            --exclude='./backend/instance/*.db' \
            --exclude='./backend/instance/*.db-wal' \
            --exclude='./backend/instance/*.db-shm' -cf - . \
            | tar -C "$target_dir" -xpf -
    )
}


backup_current() {
    phase "Database Backup"
    mkdir -p "$BACKUP_DIR"

    # Trim BEFORE writing, not only in cleanup().
    #
    # cleanup() is the last phase of the update, so it never ran on a run that
    # died earlier — including the run that died BECAUSE the disk was full.
    # Retention that only executes on success cannot bound a failure loop: a
    # box was found carrying six snapshots under a cap of five for exactly
    # this reason. Trimming first also means peak usage is `keep` snapshots
    # rather than `keep + 1`.
    if [ "$DRY_RUN" != "1" ]; then
        trim_backups
        enforce_backup_budget
    fi

    local active db_file backup_file
    active="$(active_real_dir)"
    db_file="$active/backend/instance/serverkit.db"

    # Branch on the DECLARED engine, not on which files happen to exist. An
    # install migrated SQLite -> PostgreSQL keeps its old serverkit.db on disk
    # (both deploy paths copy it forward on file-existence alone, so it is
    # carried into every future slot), and a file-first test then backed up
    # that abandoned file, skipped the dump, and left PRE_UPGRADE_DUMP unset —
    # silently disabling the auto-restore in migrate_database, which reads it
    # as "${PRE_UPGRADE_DUMP:-}" and just does nothing. PostgreSQL is the ONE
    # install type that migrates in place and cannot roll back by switching
    # slots, so that dump is its only way home. migrate_database has always
    # keyed off DATABASE_URL; this keeps the two functions in agreement.
    if grep -qE '^DATABASE_URL=postgres' "$active/.env" 2>/dev/null; then
        # PostgreSQL: the DB is external and SHARED between slots — there is
        # no per-slot file to snapshot, and the migration runs in place. A
        # pre-upgrade dump is the only safety net AND the only way back:
        # without it, a rollback hands the old code a newer schema.
        backup_file="$BACKUP_DIR/serverkit-pre-upgrade-$(date +%Y%m%d-%H%M%S).dump"
        command -v pg_dump >/dev/null 2>&1 \
            || halt "PostgreSQL install but pg_dump is unavailable — install postgresql-client so the update has a safety net"
        local pg_url
        pg_url="$(pg_url_from_env "$active")"
        if ! run_or_dry pg_dump "$pg_url" -Fc -f "$backup_file"; then
            rm -f "$backup_file"
            halt "PostgreSQL backup failed — refusing to update without a safety net"
        fi
        if [ "$DRY_RUN" != "1" ] && ! verify_pg_dump "$backup_file"; then
            rm -f "$backup_file"
            halt "PostgreSQL backup verification failed — refusing to update without a VALID safety net"
        fi
        # Remember the dump path so migrate_database can offer/perform an
        # automatic restore if the in-place migration fails.
        PRE_UPGRADE_DUMP="$backup_file"
        good "Database backed up to $backup_file"
    elif [ -f "$db_file" ]; then
        backup_file="$BACKUP_DIR/serverkit-pre-upgrade-$(date +%Y%m%d-%H%M%S).db"
        # A torn backup is worse than none: it looks like a safety net but
        # restores a malformed DB. Snapshot consistently, VERIFY the result,
        # and refuse to proceed without a proven-valid net. Failed or invalid
        # artifacts are removed so they are never mistaken for a good backup.
        if ! run_or_dry copy_sqlite_db "$db_file" "$backup_file"; then
            rm -f "$backup_file"
            halt "Database backup failed — refusing to update without a safety net"
        fi
        if [ "$DRY_RUN" != "1" ] && ! verify_sqlite_db "$backup_file"; then
            rm -f "$backup_file"
            halt "Database backup verification failed — refusing to update without a VALID safety net"
        fi
        good "Database backed up to $backup_file"
    else
        # Neither engine matched. Say which one, and say plainly that the
        # update is proceeding unprotected — "No SQLite database" reads like a
        # benign nothing-to-do on, say, a MySQL install that in fact has no
        # safety net at all.
        local db_scheme
        db_scheme="$(grep -E '^DATABASE_URL=' "$active/.env" 2>/dev/null | head -1 \
            | cut -d= -f2- | tr -d "\"'" | cut -d: -f1)"
        if [ -n "$db_scheme" ] && [ "$db_scheme" != "sqlite" ]; then
            warn "DATABASE_URL uses '$db_scheme', which this updater cannot dump — proceeding with NO database backup"
        else
            warn "No SQLite database at $db_file — skipping DB backup"
        fi
    fi

    local tree_backup
    tree_backup="$BACKUP_DIR/serverkit-tree-$(date +%Y%m%d-%H%M%S)"
    if [ -L "$INSTALL_DIR" ] && [ -d "$active" ]; then
        # Blue/green: the update installs into the OTHER slot, so this one
        # stays on disk as the rollback. A tree copy of it was a second full
        # copy of the same code (~1 GB per update on a small VPS).
        info "Install tree kept in place as the rollback slot: $active"
    elif [ -d "$active" ]; then
        # The SQLite database is NOT part of the tree snapshot: it was just
        # written as the pre-upgrade copy above, and rollback restores from
        # that copy, never from the tree. Copying it here too doubled every
        # update's backup footprint (a 450 MB database became 900 MB per run),
        # which is how small VPSes kept filling their disk with backups.
        if run_or_dry snapshot_install_tree "$active" "$tree_backup"; then
            good "Install tree backed up to $tree_backup"
        else
            warn "Install tree backup failed: $tree_backup may be incomplete"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Carry user-installed plugins across a redeploy (#48).
#
# URL/registry/upload-installed extensions live as extracted dirs under
# backend/app/plugins/<slug> and frontend/src/plugins/<slug>. A fresh
# clone/tarball only contains the repo-tracked ones, so anything the user
# installed would silently vanish — copy forward every plugin dir the new
# tree doesn't already ship. Runs BEFORE the frontend build so carried
# frontends compile into the new bundle. The backend also has a boot-time
# repair pass as a backstop (plugin_service.repair_missing_plugins).
# ---------------------------------------------------------------------------

# Plugins deliberately deleted from the product (replaced or retired) — e.g.
# serverkit-workflows, superseded by serverkit-tramo (plan 45). Never carry
# these forward: their frontends import dependencies the new tree no longer
# ships (workflows → reactflow), which sinks the whole bundle build.
# Space-separated slugs; append here whenever an extension is retired.
# Mirror of extension_migration.RETIRED_EXTENSION_SLUGS in the backend.
RETIRED_PLUGIN_SLUGS="serverkit-workflows"

plugin_is_retired() {
    local slug
    for slug in $RETIRED_PLUGIN_SLUGS; do
        [ "$1" = "$slug" ] && return 0
    done
    return 1
}

# Strip retired plugin leftovers from a tree (docker in-place mode, where
# untracked plugin dirs survive the hard reset and would otherwise sink the
# bundle rebuild).
remove_retired_plugins() {
    local tree="$1" slug sub
    for slug in $RETIRED_PLUGIN_SLUGS; do
        for sub in backend/app/plugins frontend/src/plugins; do
            if [ -d "$tree/$sub/$slug" ]; then
                rm -rf "$tree/$sub/$slug" 2>/dev/null || true
                info "Removed retired plugin: $sub/$slug"
            fi
        done
    done
    return 0
}

# Frontend plugin dirs copied forward by preserve_installed_plugins this run.
# If the bundle build then fails these are the prime suspects — see
# quarantine_carried_plugins.
CARRIED_FRONTEND_PLUGINS=""

preserve_installed_plugins() {
    local src="$1" target="$2"
    local sub dir name
    for sub in backend/app/plugins frontend/src/plugins; do
        [ -d "$src/$sub" ] || continue
        for dir in "$src/$sub"/*/; do
            [ -d "$dir" ] || continue
            name="$(basename "$dir")"
            [ "$name" = "__pycache__" ] && continue
            if plugin_is_retired "$name"; then
                info "Not carrying forward retired plugin: $sub/$name"
                continue
            fi
            if [ ! -e "$target/$sub/$name" ]; then
                mkdir -p "$target/$sub" 2>/dev/null || true
                if cp -a "$dir" "$target/$sub/$name" 2>/dev/null; then
                    info "Preserved installed plugin: $sub/$name"
                    if [ "$sub" = "frontend/src/plugins" ]; then
                        CARRIED_FRONTEND_PLUGINS="$CARRIED_FRONTEND_PLUGINS $name"
                    fi
                else
                    warn "Could not preserve plugin $sub/$name"
                fi
            fi
        done
    done
    return 0
}

# Vite 8 / rolldown (the frontend bundler) require Node ^20.19.0 || >=22.12.0.
# Returns 1 (no output) on an absent/too-old Node so callers can warn+rollback or
# halt with an actionable message, instead of npm silently skipping rolldown's
# native binary and `npm run build` dying with a cryptic MODULE_NOT_FOUND.
_node_build_ok() {
    command -v node >/dev/null 2>&1 || return 1
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

# Build the SPA bundle in a target tree. Pass "reuse" as $2 to skip npm ci
# (node_modules already intact from a just-failed build). Subshell so the cd
# cannot leak into the main shell.
build_frontend_bundle() {
    if ! _node_build_ok; then
        warn "Node.js $(node --version 2>/dev/null || echo 'not found') is too old to build the frontend — vite 8 needs 20.19+ or 22.12+. Upgrade Node (e.g. NodeSource 22 LTS) and re-run the update."
        return 1
    fi
    ( cd "$1/frontend" && \
      { [ "${2:-}" = "reuse" ] || npm ci --prefer-offline 2>&1 | tail -3; } && \
      NODE_OPTIONS="--max-old-space-size=1024" npm run build 2>&1 | tail -5 )
}

# Re-apply this install's PERSISTENT favicon tint to the staged dist. The color
# is assigned once (on first install/update) and stored in /etc/serverkit so the
# mark keeps the same per-install color across updates instead of repainting —
# and an install that predates the feature picks one up on its first update.
# Mirror of install.sh randomize_favicon (kept standalone; the two installers
# already keep separate build fns). Also serves it at /favicon.ico so a blind
# favicon-hash fetch varies too.
apply_favicon_tint() {
    local root="$1"
    local fav="$root/frontend/dist/favicon.svg"
    [ -f "$fav" ] || return 0
    local color_file="/etc/serverkit/favicon-color"
    local hsl=""
    [ -s "$color_file" ] && hsl="$(cat "$color_file" 2>/dev/null)"
    if [ -z "$hsl" ]; then
        local hues=(210 225 235 250 265 285 320 345 12 25 160 175 190)
        local base="${hues[$RANDOM % ${#hues[@]}]}"
        local jitter=$(( RANDOM % 21 - 10 ))
        local h=$(( (base + jitter + 360) % 360 ))
        local s=$(( 48 + RANDOM % 28 ))
        local l=$(( 32 + RANDOM % 15 ))
        hsl="hsl(${h}, ${s}%, ${l}%)"
        mkdir -p /etc/serverkit 2>/dev/null || true
        printf '%s\n' "$hsl" > "$color_file" 2>/dev/null || true
    fi
    sed -i -E "s|(<rect width=\"32\" height=\"32\" rx=\"7\" fill=\")[^\"]+(\")|\1${hsl}\2|" "$fav" 2>/dev/null || true
    cp "$fav" "$root/frontend/dist/favicon.ico" 2>/dev/null || true
}

# A plugin carried forward from the old install can be incompatible with the
# new tree (imports that no longer resolve) and sink the whole bundle build.
# Rather than blocking the update, move the carried frontends aside so the
# caller can retry — fail the plugin, never the update. Returns 1 when there
# was nothing to quarantine (the failure is not plugin-borne).
QUARANTINED_PLUGINS=""
quarantine_carried_plugins() {
    local target="$1" qdir name moved=""
    [ -n "$CARRIED_FRONTEND_PLUGINS" ] || return 1
    qdir="$BACKUP_DIR/quarantined-plugins-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$qdir" 2>/dev/null || return 1
    for name in $CARRIED_FRONTEND_PLUGINS; do
        [ -d "$target/frontend/src/plugins/$name" ] || continue
        if mv "$target/frontend/src/plugins/$name" "$qdir/" 2>/dev/null; then
            moved="$moved $name"
        fi
    done
    CARRIED_FRONTEND_PLUGINS=""
    [ -n "$moved" ] || { rmdir "$qdir" 2>/dev/null || true; return 1; }
    QUARANTINED_PLUGINS="${moved# }"
    warn "Carried-forward plugin(s) quarantined after build failure: $QUARANTINED_PLUGINS"
    warn "Copies kept in $qdir — reinstall compatible versions from the Marketplace"
    return 0
}

# ---------------------------------------------------------------------------
# Deploy source into the next directory
# ---------------------------------------------------------------------------
deploy_source() {
    local target="$1"
    local branch="${2:-main}"

    phase "Updating Source"

    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would clone/pull $GITHUB_REPO:$branch into $target"
        return 0
    fi

    # Preserve .env and database across rewrites. The DB snapshot must be
    # consistent (online backup API) — a racy cp of the live DB can come out
    # malformed and sink the migration in the new slot.
    local tmp_env tmp_db live_db
    tmp_env="$(mktemp)"
    tmp_db="$(mktemp)"
    cp "$INSTALL_DIR/.env" "$tmp_env" 2>/dev/null || true
    live_db="$INSTALL_DIR/backend/instance/serverkit.db"
    if [ -f "$live_db" ]; then
        copy_sqlite_db "$live_db" "$tmp_db" \
            || halt "Could not snapshot the live database consistently — aborting before any switch"
    fi

    rm -rf "$target"
    git clone --depth 1 --branch "$branch" "https://github.com/${GITHUB_REPO}.git" "$target" \
        || halt "Failed to clone ${GITHUB_REPO}:$branch"

    cp "$tmp_env" "$target/.env" 2>/dev/null || true
    if [ -s "$tmp_db" ]; then
        mkdir -p "$target/backend/instance"
        cp "$tmp_db" "$target/backend/instance/serverkit.db" 2>/dev/null || true
    fi
    rm -f "$tmp_env" "$tmp_db"

    preserve_installed_plugins "$INSTALL_DIR" "$target"

    chmod +x "$target/serverkit"
    chmod +x "$target/scripts/"*.sh 2>/dev/null || true

    good "Source updated to $branch in $target"
}

# ---------------------------------------------------------------------------
# Deploy release tarball into the next directory
# ---------------------------------------------------------------------------
deploy_release() {
    local target="$1"
    local version="$2"

    phase "Downloading Release"

    local tarball stage unpacked
    sweep_stale_downloads
    tarball="$(download_release "$version")"
    # The download dir is ours to remove; an operator's offline tarball is not.
    # Both scratch dirs are recorded globally so cleanup_on_exit removes them
    # when a halt below aborts the update.
    if [ "$tarball" != "${SERVERKIT_OFFLINE_TARBALL:-}" ]; then
        RELEASE_DOWNLOAD_DIR="$(dirname "$tarball")"
    fi

    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would unpack $tarball into $target"
        remove_release_scratch
        return 0
    fi

    stage="$(mktemp -d)"
    RELEASE_STAGE_DIR="$stage"
    tar xzf "$tarball" -C "$stage"

    unpacked="$stage/serverkit"
    [ ! -d "$unpacked" ] && unpacked="$stage/opt/serverkit"
    if [ ! -d "$unpacked" ]; then
        # `|| true`: head exiting first can SIGPIPE find (rc 141 under
        # pipefail); an empty result is handled by the halt just below.
        unpacked="$(find "$stage" -maxdepth 2 -type d -name serverkit | head -n1 || true)"
    fi
    [ -d "$unpacked" ] || halt "Release tarball layout is unrecognized"

    # Preserve live state. Same consistent-snapshot rule as deploy_source:
    # never plain-cp the live DB into the new slot.
    cp "$INSTALL_DIR/.env" "$unpacked/.env" 2>/dev/null || true
    local live_db="$INSTALL_DIR/backend/instance/serverkit.db"
    if [ -f "$live_db" ]; then
        mkdir -p "$unpacked/backend/instance"
        copy_sqlite_db "$live_db" "$unpacked/backend/instance/serverkit.db" \
            || halt "Could not snapshot the live database consistently — aborting before any switch"
    fi
    preserve_installed_plugins "$INSTALL_DIR" "$unpacked"

    rm -rf "$target"
    cp -a "$unpacked" "$target"
    remove_release_scratch

    chmod +x "$target/serverkit"
    chmod +x "$target/scripts/"*.sh 2>/dev/null || true

    good "Release $version deployed to $target"
}

# ---------------------------------------------------------------------------
# Configuration refresh
# ---------------------------------------------------------------------------
refresh_config() {
    local target="$1"

    phase "Refreshing Configuration"

    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would refresh nginx + systemd configs from $target"
        return 0
    fi

    mkdir -p "$NGINX_DIR/sites-available" "$NGINX_DIR/sites-enabled"

    # Recover panel domain from live nginx config. The trailing `|| true` keeps a
    # no-match/missing-file grep (exit 1/2) from tripping `set -euo pipefail` and
    # silently killing the updater before the atomic switch — HTTP-only boxes have
    # no serverkit.conf, so this grep finds nothing.
    local prior_panel_domain=""
    prior_panel_domain=$(grep -oE "$LETSENCRYPT_DIR/live/[^/]+/" \
        "$NGINX_DIR/sites-available/serverkit.conf" 2>/dev/null | head -n1 | \
        sed -E 's|.*/live/([^/]+)/|\1|' || true)
    [ "$prior_panel_domain" = "YOUR_DOMAIN" ] && prior_panel_domain=""

    if [ -f "$target/nginx/sites-available/serverkit.conf" ]; then
        cp "$target/nginx/sites-available/serverkit.conf" "$NGINX_DIR/sites-available/"
    fi
    if [ -f "$target/nginx/sites-available/serverkit-insecure.conf" ]; then
        cp "$target/nginx/sites-available/serverkit-insecure.conf" "$NGINX_DIR/sites-available/"
    fi

    # The panel frontend is served statically by host nginx from
    # $INSTALL_DIR/frontend/dist (the /opt/serverkit symlink). The shipped config
    # roots at the default /opt/serverkit; re-point it when SERVERKIT_DIR differs,
    # exactly as install.sh does, so a custom install dir survives upgrades.
    if [ "$INSTALL_DIR" != "/opt/serverkit" ]; then
        local conf
        for conf in serverkit.conf serverkit-insecure.conf; do
            [ -f "$NGINX_DIR/sites-available/$conf" ] && \
                sed -i "s|/opt/serverkit/frontend/dist|$INSTALL_DIR/frontend/dist|g" \
                    "$NGINX_DIR/sites-available/$conf"
        done
    fi

    # TLS floor — prefer a reversible conf.d snippet when nginx.conf doesn't
    # already declare these (a second declaration in the same http{} context is a
    # "duplicate ssl_protocols" error); otherwise rewrite in place. Mirrors
    # install.sh's harden_global_tls.
    if [ -f "$NGINX_DIR/nginx.conf" ]; then
        local ciphers='ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384'
        local has_proto=0 has_ciphers=0
        grep -qE '^[[:space:]]*ssl_protocols[[:space:]]' "$NGINX_DIR/nginx.conf" && has_proto=1
        grep -qE '^[[:space:]]*ssl_ciphers[[:space:]]'   "$NGINX_DIR/nginx.conf" && has_ciphers=1
        if [ "$has_proto" = "0" ] && [ "$has_ciphers" = "0" ] && \
           grep -qE 'include[[:space:]]+/etc/nginx/conf\.d/\*\.conf' "$NGINX_DIR/nginx.conf"; then
            mkdir -p "$NGINX_DIR/conf.d"
            cat > "$NGINX_DIR/conf.d/serverkit-tls.conf" <<EOF
# Auto-generated by ServerKit — server-wide TLS floor. Safe to remove.
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers ${ciphers};
EOF
        else
            rm -f "$NGINX_DIR/conf.d/serverkit-tls.conf" 2>/dev/null || true
            if [ "$has_proto" = "1" ]; then
                sed -i -E 's|^([[:space:]]*)ssl_protocols[[:space:]].*|\1ssl_protocols TLSv1.2 TLSv1.3;|' "$NGINX_DIR/nginx.conf"
            else
                sed -i '/http {/a \    ssl_protocols TLSv1.2 TLSv1.3;' "$NGINX_DIR/nginx.conf"
            fi
            if [ "$has_ciphers" = "1" ]; then
                sed -i -E "s|^([[:space:]]*)ssl_ciphers[[:space:]].*|\1ssl_ciphers ${ciphers};|" "$NGINX_DIR/nginx.conf"
            else
                sed -i "/http {/a \\    ssl_ciphers ${ciphers};" "$NGINX_DIR/nginx.conf"
            fi
        fi
    fi

    # SSL mode
    local ssl_mode="insecure"
    if [ -f "$CONFIG_DIR/ssl-mode" ]; then
        ssl_mode="$(cat "$CONFIG_DIR/ssl-mode")"
    fi
    if [ "$ssl_mode" = "secure" ] && [ -f "$NGINX_DIR/sites-available/serverkit.conf" ]; then
        local panel_domain=""
        if [ -f "$CONFIG_DIR/panel-domain" ]; then
            panel_domain="$(cat "$CONFIG_DIR/panel-domain" 2>/dev/null || true)"
        fi
        [ -z "$panel_domain" ] && panel_domain="$prior_panel_domain"

        if [ -n "$panel_domain" ] && [ -d "$LETSENCRYPT_DIR/live/$panel_domain" ]; then
            sed -i "s|/etc/letsencrypt/live/YOUR_DOMAIN/|$LETSENCRYPT_DIR/live/$panel_domain/|g" \
                "$NGINX_DIR/sites-available/serverkit.conf"
            ln -sf "$NGINX_DIR/sites-available/serverkit.conf" "$NGINX_DIR/sites-enabled/serverkit.conf"
        else
            warn "SSL mode is 'secure' but no certificate found for '${panel_domain:-unknown}'"
            ln -sf "$NGINX_DIR/sites-available/serverkit-insecure.conf" "$NGINX_DIR/sites-enabled/serverkit.conf"
        fi
    else
        ln -sf "$NGINX_DIR/sites-available/serverkit-insecure.conf" "$NGINX_DIR/sites-enabled/serverkit.conf"
    fi

    # Service unit — render from the template so a custom SERVERKIT_DIR / venv /
    # log path survives upgrades (the unit references the /opt/serverkit symlink,
    # so blue/green switches need no re-render). Fall back to a plain unit if an
    # older tree still ships one.
    local svc_template="$target/templates/serverkit-backend.service.in"
    # Bind the API to loopback by default — host nginx fronts it on :80/:443, so
    # the raw gunicorn port must not be world-reachable. Mirror install.sh so an
    # update re-renders @BIND_HOST@ instead of leaving the literal placeholder
    # (which would make gunicorn fail to bind). Override with SERVERKIT_BIND_HOST.
    local bind_host="${SERVERKIT_BIND_HOST:-127.0.0.1}"
    if [ -f "$svc_template" ]; then
        sed -e "s|@SERVERKIT_DIR@|$INSTALL_DIR|g" \
            -e "s|@SERVERKIT_VENV_DIR@|$VENV_DIR|g" \
            -e "s|@PORT@|5000|g" \
            -e "s|@BIND_HOST@|$bind_host|g" \
            -e "s|@USER@|root|g" \
            -e "s|@LOG_DIR@|$LOG_DIR|g" \
            "$svc_template" > "$SYSTEMD_DIR/serverkit.service"
    elif [ -f "$target/serverkit-backend.service" ]; then
        cp "$target/serverkit-backend.service" "$SYSTEMD_DIR/serverkit.service"
    fi
    systemctl daemon-reload 2>/dev/null \
        || warn "systemd daemon-reload failed — the backend may restart with a stale unit"

    report_stale_panel_vhosts
    good "Configuration refreshed"
}

# The panel used to be served by a frontend container on :3847; it is now
# served statically by host nginx. A leftover custom vhost proxying to :3847
# keeps serving whatever bundle the (never-updated) container holds — the
# panel looks "stuck on an old version" for that hostname only, which reads
# like a browser/CDN cache problem and wastes hours (2026-07-03 incident).
# Observation only (R1): warn and continue, tolerate a missing/empty dir.
report_stale_panel_vhosts() {
    local hits=""
    hits="$(grep -rls 'proxy_pass http://127\.0\.0\.1:3847' \
        "$NGINX_DIR/sites-enabled/" 2>/dev/null | sort -u | tr '\n' ' ' || true)"
    if [ -n "$hits" ]; then
        warn "Vhost(s) still proxy the panel to the retired :3847 frontend container: ${hits}"
        warn "  These serve a STALE panel bundle. Repoint them at the static SPA"
        warn "  (root $INSTALL_DIR/frontend/dist + /api and /socket.io → :5000),"
        warn "  mirroring sites-available/serverkit.conf."
    fi
}

# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
# Probe the backend health endpoint; returns 0 if healthy within <timeout>s.
# Side-effect free (unlike health_check, which triggers a rollback) so it is
# safe to call *after* a rollback to confirm the restored version came back.
quick_health() {
    local timeout="${1:-30}" waited=0
    while [ "$waited" -lt "$timeout" ]; do
        curl -sf --max-time 5 http://127.0.0.1:5000/api/v1/system/health >/dev/null 2>&1 && return 0
        sleep 2
        waited=$((waited + 2))
    done
    return 1
}

rollback() {
    # Flag FIRST: halt() below exits the script, which fires the EXIT trap
    # (cleanup_on_exit) — without this flag already set, that trap would run a
    # second, redundant rollback on top of the one that just finished.
    ROLLING_BACK=1
    warn "Update failed — rolling back to previous slot..."

    if [ -z "${PREVIOUS_DIR:-}" ] || [ ! -d "$PREVIOUS_DIR" ]; then
        halt "Cannot roll back: previous installation directory not available"
    fi

    # Same zero-downtime discipline as the forward path: cycle only the backend,
    # never stop nginx (it fronts every hosted app), and reload its config after
    # the slot is switched back.
    systemctl stop "$BACKEND_SERVICE" 2>/dev/null || true
    wait_for_service "$BACKEND_SERVICE" inactive 30 || true

    atomic_switch "$PREVIOUS_DIR"

    # A rollback must never abort mid-flight over one failed step — everything
    # from here on is best-effort so the backend still gets started.
    systemctl daemon-reload 2>/dev/null \
        || warn "systemd daemon-reload failed during rollback — continuing"
    systemctl start "$BACKEND_SERVICE" 2>/dev/null || true
    wait_for_service "$BACKEND_SERVICE" active 30 || true
    # Frontend is static (served from the restored slot); just re-label + reload.
    selinux_label_dist "$PREVIOUS_DIR"
    reload_nginx_graceful

    # Confirm the restored version actually answers — a rollback that itself
    # comes up unhealthy is a far worse state to leave the operator guessing in.
    if quick_health 30; then
        halt "Rolled back to $(active_real_dir) and it is healthy. Inspect logs: journalctl -u serverkit -n 50"
    else
        halt "Rolled back to $(active_real_dir) but it is STILL UNHEALTHY — manual intervention needed. Logs: journalctl -u serverkit -n 50"
    fi
}

# Remove deploy_release's download and unpack dirs. Warn-and-continue: a
# scratch dir that will not delete must never fail an update or its rollback.
remove_release_scratch() {
    if [ -n "${RELEASE_DOWNLOAD_DIR:-}" ]; then
        rm -rf "$RELEASE_DOWNLOAD_DIR" 2>/dev/null || true
        RELEASE_DOWNLOAD_DIR=""
    fi
    if [ -n "${RELEASE_STAGE_DIR:-}" ]; then
        rm -rf "$RELEASE_STAGE_DIR" 2>/dev/null || true
        RELEASE_STAGE_DIR=""
    fi
    return 0
}

# If the update fails after we have switched the symlink, roll back to the
# previous slot automatically. Registered as the EXIT trap by the run block
# below; defined here (above the source guard) so it stays unit-testable.
cleanup_on_exit() {
    local rc=$?
    remove_release_scratch
    [ "$rc" -eq 0 ] && return 0
    if [ "$DRY_RUN" = "0" ] && [ -n "${PREVIOUS_DIR:-}" ] && \
       [ "${ROLLING_BACK:-0}" != "1" ] && [ "${HEALTH_PASSED:-0}" != "1" ]; then
        ROLLING_BACK=1
        rollback
    fi
    # Always return 0: the script's exit code is already $rc (an EXIT trap does
    # not change it) — returning $rc here only re-fires the ERR trap, which
    # appends a second, misleading "Update aborted" report.
    return 0
}

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
health_check() {
    phase "Health Check"

    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would probe http://127.0.0.1:5000/api/v1/system/health"
        return 0
    fi

    step "Waiting for backend..."
    local waited=0
    while [ "$waited" -lt 60 ]; do
        if curl -sf --max-time 5 http://127.0.0.1:5000/api/v1/system/health >/dev/null 2>&1; then
            good "Backend healthy"
            break
        fi
        sleep 2
        waited=$((waited + 2))
    done
    if [ "$waited" -ge 60 ]; then
        rollback
    fi

    if ! curl -sf --max-time 5 http://127.0.0.1:5000/api/v1/system/health >/dev/null 2>&1; then
        rollback
    fi

    # No frontend probe: the panel frontend is static files served by host
    # nginx from the active slot — there is no frontend container anymore.
    HEALTH_PASSED=1
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
# Trim a family of backups down to its newest <keep> entries. Deliberately
# find-based: the old `ls -t "$BACKUP_DIR"/<glob> | tail | xargs` aborted the
# whole updater under `set -Eeuo pipefail` whenever the glob matched nothing
# (ls exits 2) — i.e. on every box with no prior backups of that family, AFTER
# an otherwise successful update. find treats "nothing matched" as a clean
# empty result, and an absent BACKUP_DIR is an explicit no-op.
prune_old_backups() {
    local pattern="$1" keep="$2"
    [ -d "$BACKUP_DIR" ] || return 0
    find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -name "$pattern" -printf '%T@\t%p\n' 2>/dev/null \
        | sort -rn | tail -n +"$((keep + 1))" | cut -f2- \
        | xargs -r -d '\n' rm -rf || true
}

# Bytes currently held by all backup artifacts.
backups_size_kb() {
    [ -d "$BACKUP_DIR" ] || { echo 0; return 0; }
    du -sk "$BACKUP_DIR" 2>/dev/null | awk '{print $1}' || echo 0
}

# How many upgrade snapshots to keep when SERVERKIT_BACKUP_RETENTION is unset:
# 1 on a filesystem under 50 GiB, else 3. Each snapshot is a full database
# copy, and on a 25 GB VPS three of them compete with the apps for the disk.
# An unreadable size keeps the old default.
default_backup_keep() {
    local total_kb
    total_kb="$(df -Pk "$BACKUP_DIR" 2>/dev/null | awk 'NR==2 {print $2}' || true)"
    if [ -z "$total_kb" ]; then
        total_kb="$(df -Pk / 2>/dev/null | awk 'NR==2 {print $2}' || true)"
    fi
    if [ -n "$total_kb" ] && [ "$total_kb" -lt $((50 * 1024 * 1024)) ] 2>/dev/null; then
        echo 1
    else
        echo 3
    fi
}

# Trim every backup kind to the retention cap. Safe to call repeatedly.
trim_backups() {
    local keep="${1:-${SERVERKIT_BACKUP_RETENTION:-$(default_backup_keep)}}"
    prune_old_backups 'serverkit-tree-*'             "$keep"
    prune_old_backups 'serverkit-pre-upgrade-*.db'   "$keep"
    prune_old_backups 'serverkit-pre-upgrade-*.dump' "$keep"
}

# Drop the OLDEST snapshots until the backup directory fits its size budget.
#
# A count cap alone is not a disk guarantee: five snapshots of an 800 MB
# database is 8 GB, which is a third of a 25 GB VPS. The budget is a share of
# the filesystem (default 15%), and at least one snapshot is always kept —
# an update with no way back is worse than a full disk.
enforce_backup_budget() {
    [ -d "$BACKUP_DIR" ] || return 0
    local pct="${SERVERKIT_BACKUP_MAX_PERCENT:-15}"
    local total_kb budget_kb used_kb
    total_kb="$(df -Pk "$BACKUP_DIR" | awk 'NR==2 {print $2}')"
    [ -n "$total_kb" ] && [ "$total_kb" -gt 0 ] || return 0
    budget_kb=$(( total_kb * pct / 100 ))

    local guard=0
    while [ "$guard" -lt 50 ]; do
        used_kb="$(backups_size_kb)"
        [ "$used_kb" -le "$budget_kb" ] && return 0
        # Oldest stamp still on disk, across every artifact kind.
        local oldest
        oldest="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 \
            \( -name 'serverkit-tree-*' -o -name 'serverkit-pre-upgrade-*' \) \
            -printf '%T@\t%p\n' 2>/dev/null | sort -n | head -1 | cut -f2-)"
        [ -n "$oldest" ] || return 0
        # Never delete the last remaining snapshot set.
        local remaining
        remaining="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 \
            -name 'serverkit-pre-upgrade-*' 2>/dev/null | wc -l)"
        if [ "$remaining" -le 1 ] && [[ "$(basename "$oldest")" == serverkit-pre-upgrade-* ]]; then
            warn "Backups exceed ${pct}% of the disk but only one restore point remains — keeping it"
            return 0
        fi
        info "Backup budget exceeded — removing oldest backup $(basename "$oldest")"
        rm -rf "$oldest" || return 0
        guard=$((guard + 1))
    done
}

cleanup() {
    phase "Cleanup"

    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would trim old backups"
        return 0
    fi

    # Retention is capped (default 3, SERVERKIT_BACKUP_RETENTION overrides)
    # AND budgeted by size: pre-upgrade DB backups are full-size copies, so
    # five of an 800 MB database is 8 GB — a third of a 25 GB droplet. Seen in
    # the wild twice: 6.3G of stale backups, then a disk filled to 0 bytes.
    #
    # backup_current already trimmed before writing; this is the second pass
    # that also reclaims the snapshot this run just superseded.
    trim_backups
    enforce_backup_budget

    local new_version
    new_version="$(cat "$INSTALL_DIR/VERSION" 2>/dev/null | tr -d '\n\r ' || echo "unknown")"
    curl -s "https://serverkit.ai/track/update?v=${new_version}" >/dev/null 2>&1 || true
    good "Cleanup complete"
}

# ---------------------------------------------------------------------------
# Firewall — keep 80/443 open across updates.
# ---------------------------------------------------------------------------
# Idempotent and best-effort: a box installed before firewall automation existed
# gets its ports opened on the next update; an already-configured box is a no-op.
# Records what we opened in install-state.json so uninstall can undo it. Sources
# the helpers from the (now-current) install tree, which carries scripts/lib.
ensure_firewall() {
    local lib="$INSTALL_DIR/scripts/lib"
    [ -f "$lib/firewall.sh" ] || return 0
    # shellcheck source=/dev/null
    source "$lib/firewall.sh"
    if [ -f "$lib/state.sh" ]; then
        # shellcheck source=/dev/null
        source "$lib/state.sh"
    fi

    local backend
    backend="$(firewall_detect)"
    if [ "$backend" = "none" ]; then
        return 0
    fi

    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would ensure firewall ($backend) allows 80/tcp and 443/tcp"
        return 0
    fi

    firewall_open "$backend" 80/tcp 443/tcp || true
    if command -v state_set >/dev/null 2>&1; then
        state_set firewall_backend "$backend" || true
        state_append firewall_ports 80/tcp || true
        state_append firewall_ports 443/tcp || true
    fi
    good "Firewall ensured ($backend): 80/tcp and 443/tcp open"
}

refresh_bash_completion() {
    # Regenerate bash tab-completion from the switched-in CLI so new commands
    # complete immediately after an update. Best-effort: boxes without the
    # bash-completion package simply skip it.
    local dir="${SERVERKIT_COMPLETION_DIR:-/etc/bash_completion.d}"
    [ -d "$dir" ] || return 0
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would refresh $dir/serverkit"
        return 0
    fi
    bash "$INSTALL_DIR/serverkit" completion > "$dir/serverkit" 2>/dev/null \
        || { rm -f "$dir/serverkit"; warn "Could not refresh bash completion (non-fatal)"; }
}

# ---------------------------------------------------------------------------
# All-Docker deployment (compose) detection + update path
# ---------------------------------------------------------------------------
# A ServerKit box runs in one of two shapes:
#   * Hybrid (canonical): backend in a host venv under systemd `serverkit`,
#     only the frontend in Docker. The blue/green + venv + systemd flow below
#     targets this shape.
#   * All-Docker: both backend AND frontend run as docker-compose services
#     (container_name serverkit-backend / serverkit-frontend), no host venv.
# Running the hybrid flow on an all-Docker box builds a useless host venv,
# migrates the wrong (non-container) database, and stops a systemd unit that
# never serves traffic — which is exactly the failure that ends in
# "/opt/serverkit/venv/bin/activate: No such file or directory". Detect the
# all-Docker shape and route it to a dedicated compose update instead.
is_docker_deployment() {
    [ -f "$INSTALL_DIR/docker-compose.yml" ] || return 1
    # A usable host venv means this is the hybrid shape — take precedence.
    [ -x "$INSTALL_DIR/venv/bin/python" ] && return 1
    # The backend container is defined by the all-Docker compose project.
    docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'serverkit-backend'
}

dc() {
    if docker compose version &>/dev/null; then
        ( cd "$INSTALL_DIR" && docker compose "$@" )
    else
        ( cd "$INSTALL_DIR" && docker-compose "$@" )
    fi
}

backup_docker_db() {
    mkdir -p "$BACKUP_DIR"
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would copy the SQLite DB out of the backend container"
        return 0
    fi
    local backup_file
    backup_file="$BACKUP_DIR/serverkit-pre-upgrade-$(date +%Y%m%d-%H%M%S).db"
    # The live DB lives in the serverkit-data volume at /app/instance inside
    # the container, not in the source tree. Prefer a consistent snapshot via
    # the container's Python (SQLite online backup API) — a plain docker cp
    # of the live file races the backend's writes and can come out malformed.
    local py snap_ok=0
    for py in python python3; do
        if docker exec -i serverkit-backend "$py" - /app/instance/serverkit.db /tmp/.sk-snap.db <<'PYEOF' 2>/dev/null; then
import sqlite3, sys
s = sqlite3.connect(sys.argv[1])
d = sqlite3.connect(sys.argv[2])
s.backup(d)
d.close()
s.close()
PYEOF
            snap_ok=1
            break
        fi
    done
    if [ "$snap_ok" = "1" ] && docker cp serverkit-backend:/tmp/.sk-snap.db "$backup_file" 2>/dev/null; then
        docker exec serverkit-backend rm -f /tmp/.sk-snap.db 2>/dev/null || true
    else
        docker cp serverkit-backend:/app/instance/serverkit.db "$backup_file" 2>/dev/null || true
    fi
    # Same rule as the host path: an unverified backup is not a safety net.
    if [ -f "$backup_file" ] && verify_sqlite_db "$backup_file"; then
        good "Database backed up to $backup_file"
    else
        rm -f "$backup_file"
        halt "Could not obtain a VALID database backup from the container — refusing to update without a safety net"
    fi
}

# L6 — Heal layout left by an interrupted or legacy (hybrid) run before the
# docker update touches anything.
heal_layout_for_docker() {
    # Pin the compose project name. A blue/green symlink otherwise makes compose
    # derive the project name from the link target and warn — worse, a changed
    # name would orphan the running containers.
    export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$BASE_NAME}"

    [ "$DRY_RUN" = "1" ] && return 0

    # On an all-Docker box the blue/green slots are vestigial. If the install is
    # a symlink (a prior hybrid run migrated it), reclaim the *inactive* slot it
    # left behind — the live tree + DB volume are elsewhere, so this is safe.
    if [ -L "$INSTALL_DIR" ]; then
        local active inactive
        active="$(active_real_dir)"
        inactive="$DIR_A"; [ "$active" = "$DIR_A" ] && inactive="$DIR_B"
        if [ -d "$inactive" ]; then
            warn "Reclaiming stale slot from a prior run: $inactive"
            rm -rf "$inactive"
        fi
    fi
}

# L4 — Snapshot the current docker deployment so a bad upgrade can be reverted.
DOCKER_PREV_SHA=""
DOCKER_ROLLBACK_READY=0
snapshot_docker_state() {
    [ "$DRY_RUN" = "1" ] && return 0
    DOCKER_PREV_SHA="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || true)"
    # Tag the currently-running images so we can re-point :latest back to them,
    # and stash the host-built bundle that the new build is about to overwrite.
    local img
    for img in serverkit-backend serverkit-frontend; do
        docker image inspect "$img:latest" >/dev/null 2>&1 && \
            docker tag "$img:latest" "$img:rollback" 2>/dev/null || true
    done
    if [ -d "$INSTALL_DIR/frontend/dist" ]; then
        rm -rf "$INSTALL_DIR/frontend/dist.rollback"
        cp -a "$INSTALL_DIR/frontend/dist" "$INSTALL_DIR/frontend/dist.rollback" 2>/dev/null || true
    fi
    DOCKER_ROLLBACK_READY=1
}

rollback_docker() {
    [ "$DOCKER_ROLLBACK_READY" = "1" ] || halt "New version is unhealthy and no rollback snapshot exists — inspect: cd $INSTALL_DIR && docker compose logs backend"
    warn "New version unhealthy — rolling back to the previous deployment..."

    # Guard the reset on a non-empty SHA: an empty SHA would make `git reset
    # --hard ''` reset to HEAD (a no-op at best, surprising at worst). The image
    # re-tag + bundle restore below still recover the previous deployment even
    # when no commit was recorded.
    if [ -n "$DOCKER_PREV_SHA" ]; then
        git -C "$INSTALL_DIR" reset --hard "$DOCKER_PREV_SHA" 2>&1 | tail -n2 || \
            warn "git reset to $DOCKER_PREV_SHA failed during rollback"
    else
        warn "No previous commit recorded — skipping git reset (images/bundle still restored)"
    fi
    if [ -d "$INSTALL_DIR/frontend/dist.rollback" ]; then
        rm -rf "$INSTALL_DIR/frontend/dist"
        mv "$INSTALL_DIR/frontend/dist.rollback" "$INSTALL_DIR/frontend/dist"
    fi
    local img
    for img in serverkit-backend serverkit-frontend; do
        docker image inspect "$img:rollback" >/dev/null 2>&1 && \
            docker tag "$img:rollback" "$img:latest" 2>/dev/null || true
    done
    dc up -d 2>&1 | tail -n10 || true

    # Confirm the restored deployment came back healthy.
    local waited=0 status=""
    while [ "$waited" -lt 60 ]; do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' serverkit-backend 2>/dev/null || echo missing)"
        { [ "$status" = "healthy" ] || [ "$status" = "none" ]; } && break
        sleep 3
        waited=$((waited + 3))
    done
    if [ "$status" = "healthy" ] || [ "$status" = "none" ]; then
        halt "Rolled back to previous version (${DOCKER_PREV_SHA:-unknown}) and it is healthy. Logs: cd $INSTALL_DIR && docker compose logs backend"
    else
        halt "Rolled back to previous version (${DOCKER_PREV_SHA:-unknown}) but it is STILL UNHEALTHY (status: $status) — manual intervention needed. Logs: cd $INSTALL_DIR && docker compose logs backend"
    fi
}

# Drop rollback artifacts once an upgrade is confirmed healthy.
clear_docker_rollback() {
    [ "$DRY_RUN" = "1" ] && return 0
    rm -rf "$INSTALL_DIR/frontend/dist.rollback" 2>/dev/null || true
    local img
    for img in serverkit-backend serverkit-frontend; do
        docker image inspect "$img:rollback" >/dev/null 2>&1 && \
            docker rmi "$img:rollback" >/dev/null 2>&1 || true
    done
}

update_docker_compose() {
    printf '\n  %s%sServerKit Updater — Docker deployment%s\n' "$BLD" "$PAPER" "$RST"
    STARTED_AT=$(date +%s)
    [ "$DRY_RUN" = "1" ] && warn "DRY RUN — no changes will be made"

    command -v docker &>/dev/null || halt "docker is required but not installed"
    command -v git &>/dev/null || halt "git is required but not installed"

    heal_layout_for_docker

    # Resolve the git ref to update to (branch / release tag / main).
    local ref="origin/main"
    local mode="main"
    if [ -n "$TARGET_BRANCH" ]; then
        ref="origin/$TARGET_BRANCH"
        mode="branch:$TARGET_BRANCH"
    elif [ "$USE_RELEASE" = "1" ]; then
        # `|| true`: head exiting first can SIGPIPE git (rc 141 under
        # pipefail); an empty ref is caught by the halt just below.
        ref="${RELEASE_VERSION:-$(git -C "$INSTALL_DIR" tag -l 'v*' --sort=-v:refname | head -n1 || true)}"
        [ -n "$ref" ] || halt "Could not determine a release tag to update to"
        mode="release"
    fi

    # Skip when already current (unless --force). Capture the starting version
    # for the summary before we touch the tree.
    local old_version
    old_version="$(local_version)"
    version_gate

    phase "Database Backup"
    backup_docker_db

    # Snapshot the current (still-running, last-known-good) deployment before we
    # mutate the tree, rebuild the bundle, or rebuild images — so an unhealthy
    # upgrade can be reverted.
    snapshot_docker_state

    phase "Syncing Source"
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would git fetch and reset $INSTALL_DIR to $ref"
    else
        # The compose build context IS the live tree, so update it in place
        # (no blue/green). Untracked .env, instance DB and frontend/dist are
        # preserved across a hard reset.
        git -C "$INSTALL_DIR" fetch --all --tags --prune 2>&1 | tail -n2 || halt "git fetch failed"
        git -C "$INSTALL_DIR" reset --hard "$ref" 2>&1 | tail -n2 || halt "git reset to $ref failed"
        chmod +x "$INSTALL_DIR/serverkit" "$INSTALL_DIR/scripts/"*.sh 2>/dev/null || true
        # Untracked plugin dirs survive the hard reset — drop retired ones so
        # they can't sink the bundle rebuild below.
        remove_retired_plugins "$INSTALL_DIR"
        good "Source synced to $ref"
    fi

    # When the compose serves a host-built bundle (bind-mounted ./frontend/dist)
    # the image's own assets are shadowed, so the bundle must be rebuilt on the
    # host. Otherwise the frontend image carries its assets and needs no host build.
    if grep -qE '\./frontend/dist[ :]' "$INSTALL_DIR/docker-compose.yml" 2>/dev/null; then
        phase "Building Frontend"
        if [ "$DRY_RUN" = "1" ]; then
            info "[dry-run] would npm ci + npm run build in frontend/"
        elif command -v npm &>/dev/null; then
            _node_build_ok || halt "Node.js $(node --version 2>/dev/null || echo 'not found') is too old to build the frontend — vite 8 needs 20.19+ or 22.12+. Upgrade Node (e.g. NodeSource 22 LTS) and re-run."
            ( cd "$INSTALL_DIR/frontend" && npm ci 2>&1 | tail -n3 && \
              NODE_OPTIONS="--max-old-space-size=1024" npm run build 2>&1 | tail -n5 ) \
              || halt "Frontend build failed"
            good "Frontend assets rebuilt"
        else
            warn "npm not on host; relying on the frontend image build for assets"
        fi
    fi

    phase "Building Images"
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would docker compose build"
    else
        # Build before recreating: running containers are untouched until
        # 'up -d', so a failed build leaves the current version serving.
        dc build 2>&1 | tail -n15 || halt "docker compose build failed — current version still running"
        good "Images built"
    fi

    phase "Recreating Containers"
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would docker compose up -d (backend auto-migrates on boot)"
    else
        dc up -d 2>&1 | tail -n15 || halt "docker compose up -d failed"
        # If the frontend serves a host-built bundle, restart it so nginx picks
        # up freshly built assets even when its own image/config did not change.
        if grep -qE '\./frontend/dist[ :]' "$INSTALL_DIR/docker-compose.yml" 2>/dev/null; then
            dc restart frontend 2>&1 | tail -n3 || true
        fi
        good "Containers recreated"
    fi

    phase "Health Check"
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] would wait for serverkit-backend to report healthy"
    else
        local waited=0 status=""
        while [ "$waited" -lt 90 ]; do
            status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' serverkit-backend 2>/dev/null || echo missing)"
            [ "$status" = "healthy" ] && { good "Backend healthy"; break; }
            [ "$status" = "none" ] && { good "Backend container running (no healthcheck defined)"; break; }
            sleep 3
            waited=$((waited + 3))
        done
        if [ "$status" != "healthy" ] && [ "$status" != "none" ]; then
            warn "Backend did not report healthy within 90s (status: $status)"
            rollback_docker   # L4 — revert to the snapshot; this halts the script
        fi
    fi

    # Healthy (or no healthcheck defined): the upgrade stuck, drop the snapshot.
    clear_docker_rollback

    # Keep the firewall in sync (idempotent, best-effort).
    ensure_firewall || true

    local new_version
    new_version="$(cat "$INSTALL_DIR/VERSION" 2>/dev/null | tr -d '\n\r ' || echo unknown)"
    curl -s "https://serverkit.ai/track/update?v=${new_version}" >/dev/null 2>&1 || true

    printf '\n  %s%s✔  Update complete (Docker)%s   %s%s%s\n\n' \
        "$BLD" "$HUE_OK" "$RST" "$FOG" "$(clock)" "$RST"
    printf '  Updated   %s → %s\n' "${old_version:-unknown}" "$new_version"
    printf '  Mode      docker/%s\n' "$mode"
    printf '  Duration  %s\n' "$(clock)"
    printf '  Backend   %s\n' "$(docker inspect -f '{{.State.Status}}' serverkit-backend 2>/dev/null || echo unknown)"
    printf '  Frontend  %s\n' "$(docker inspect -f '{{.State.Status}}' serverkit-frontend 2>/dev/null || echo unknown)"
    [ -n "${UPDATE_LOG:-}" ] && printf '  Log       %s\n' "$UPDATE_LOG"
    printf '\n  %sCLI%s       serverkit status\n\n' "$BLD" "$RST"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Sourcing this file (e.g. from scripts/test/test_update.sh) exposes every
# function above for unit testing without running an update. Only a direct
# execution falls through to the run below.
[ "${BASH_SOURCE[0]}" = "${0}" ] || return 0

maybe_reexec_latest_updater   # may exec into the newest updater and not return
acquire_update_lock
init_logging
# L5 — turn any unguarded failure (in the main flow OR a helper, thanks to -E)
# into a labelled report instead of a silent drop back to the prompt.
trap 'report_failure "$?" "$LINENO" "$BASH_COMMAND"' ERR

# All-Docker deployments don't use the host venv / systemd / blue-green flow —
# route them to the compose path. This is what prevents the missing-venv crash.
if is_docker_deployment; then
    update_docker_compose
    exit 0
fi

printf '\n  %s%sServerKit Updater%s\n' "$BLD" "$PAPER" "$RST"
STARTED_AT=$(date +%s)

[ "$DRY_RUN" = "1" ] && warn "DRY RUN — no changes will be made"

# Roll back automatically if the update fails after the symlink switch (the
# handler, cleanup_on_exit, is defined next to rollback() above).
trap cleanup_on_exit EXIT

preflight_check

# Capture where we are coming from and what mode this is, then skip the whole
# update when the box is already current (unless --force). Doing this before any
# backup/deploy work means an up-to-date box does zero churn.
OLD_VERSION="$(local_version)"
if [ "$USE_RELEASE" = "1" ]; then
    UPDATE_MODE="release"
elif [ -n "$TARGET_BRANCH" ]; then
    UPDATE_MODE="branch:$TARGET_BRANCH"
else
    UPDATE_MODE="main"
fi
version_gate

ensure_bluegreen_layout
backup_current

# Determine update mode.
NEXT_DIR="$(next_real_dir)"

if [ "$USE_RELEASE" = "1" ]; then
    if [ -z "$RELEASE_VERSION" ]; then
        RELEASE_VERSION="$(curl -sf "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" \
            | grep '"tag_name"' | head -1 | cut -d'"' -f4 || true)"
        [ -n "$RELEASE_VERSION" ] || halt "Could not determine the latest release"
    fi
    step "Updating to release $RELEASE_VERSION"
    deploy_release "$NEXT_DIR" "$RELEASE_VERSION"
elif [ -n "$TARGET_BRANCH" ]; then
    step "Updating to branch $TARGET_BRANCH"
    deploy_source "$NEXT_DIR" "$TARGET_BRANCH"
else
    deploy_source "$NEXT_DIR" "main"
fi

# Ensure venv in the new tree.
require_venv "$NEXT_DIR/venv"

# Run database migrations before switching.
migrate_database "$NEXT_DIR"

# Sync templates.
if [ "$DRY_RUN" = "0" ]; then
    mkdir -p /etc/serverkit/templates
    cp -r "$NEXT_DIR/backend/templates/"*.yaml /etc/serverkit/templates/ 2>/dev/null || true
    cp -r "$NEXT_DIR/backend/templates/"*.yml  /etc/serverkit/templates/ 2>/dev/null || true
fi

# Build frontend if dist is missing (source mode or older release).
if [ ! -d "$NEXT_DIR/frontend/dist" ]; then
    step "Building frontend..."
    if [ "$DRY_RUN" = "0" ]; then
        # The build is guarded so a failure halts loudly while the old slot
        # still serves. When plugins were carried forward from the old install
        # they are the usual culprit (imports the new tree no longer satisfies)
        # — quarantine them and give the build one retry before giving up.
        if ! build_frontend_bundle "$NEXT_DIR"; then
            quarantine_carried_plugins "$NEXT_DIR" \
                || halt "Frontend build failed — previous installation still active"
            step "Retrying frontend build without carried-forward plugins..."
            build_frontend_bundle "$NEXT_DIR" reuse \
                || halt "Frontend build failed — previous installation still active"
        fi
    else
        info "[dry-run] would npm ci + npm run build in $NEXT_DIR/frontend"
    fi
fi

# Re-apply the persistent per-install favicon tint to the staged dist (whether
# just rebuilt above or shipped prebuilt), so the color carries across updates.
[ "$DRY_RUN" = "0" ] && apply_favicon_tint "$NEXT_DIR"

# Refresh nginx/systemd configs in the active tree before switch.
refresh_config "$NEXT_DIR"

# Stop services.
#
# nginx is deliberately NOT stopped: it fronts every managed app, so taking it
# down would black out unrelated sites for the whole switch. Only the panel
# backend is cycled (a brief panel-API gap that never touches hosted apps), and
# nginx picks up any refreshed config via a graceful reload after the switch.
#
# First snapshot which hosted apps are reachable through nginx right now, so we
# can prove afterwards that the update did not knock any of them offline.
APP_BASELINE="$(snapshot_app_reachability)"

phase "Stopping Services"
# Guarded like the rollback path: `systemctl stop` exits non-zero when the unit
# is not loaded (exit 5) — a reason to warn, never to abort a healthy update.
run_or_dry systemctl stop "$BACKEND_SERVICE" \
    || warn "Backend service did not stop cleanly (unit may not be loaded) — continuing"
wait_for_service "$BACKEND_SERVICE" inactive 30 || warn "Backend did not stop within 30 seconds"

# Record the currently active directory before switching.
PREVIOUS_DIR="$(active_real_dir)"

# Atomic switch.
atomic_switch "$NEXT_DIR"

# Start services.
#
# The frontend is now static files under the switched-in slot's frontend/dist,
# served directly by host nginx (no container to recreate). The atomic switch
# already swapped the served assets; nginx only needs a graceful config reload.
phase "Starting Services"
run_or_dry systemctl start "$BACKEND_SERVICE"
wait_for_service "$BACKEND_SERVICE" active 30 || warn "Backend did not report active within 30 seconds"
# Re-label the now-active slot's bundle for SELinux hosts before nginx serves it.
selinux_label_dist "$NEXT_DIR"
# Graceful, zero-downtime config swap — never a stop/start (see reload_nginx_graceful).
reload_nginx_graceful
good "Services started"

# Health check.
health_check

# Verify the panel update did not take any hosted app down. Compare the
# pre-switch reachability snapshot with a fresh probe now that nginx has
# reloaded. Best-effort: a regression is reported loudly but does not fail the
# (already-healthy) update — the operator decides what to do about an app that
# was likely already unhealthy.
if [ "$DRY_RUN" = "0" ]; then
    phase "Verifying App Uptime"
    APP_AFTER="$(snapshot_app_reachability)"
    report_app_uptime_regressions "$APP_BASELINE" "$APP_AFTER" || true
fi

# Keep the firewall in sync (idempotent, best-effort).
ensure_firewall || true

# Keep bash tab-completion in sync with the updated CLI (best-effort).
refresh_bash_completion || true

# Cleanup.
cleanup

# Summary.
NEW_VERSION="$(local_version)"
printf '\n  %s%s✔  Update complete%s   %s%s%s\n\n' \
    "$BLD" "$HUE_OK" "$RST" "$FOG" "$(clock)" "$RST"
printf '  Updated   %s → %s\n' "${OLD_VERSION:-unknown}" "${NEW_VERSION:-unknown}"
printf '  Mode      %s\n' "${UPDATE_MODE:-main}"
printf '  Duration  %s\n' "$(clock)"
printf '  Active    %s\n' "$(active_real_dir)"
printf '  Backend   %s\n' "$(systemctl is-active serverkit 2>/dev/null || echo unknown)"
printf '  Nginx     %s\n\n' "$(systemctl is-active nginx 2>/dev/null || echo unknown)"
[ -n "${UPDATE_LOG:-}" ] && printf '  Log       %s\n\n' "$UPDATE_LOG"
printf '  %sCLI%s       serverkit status\n\n' "$BLD" "$RST"
