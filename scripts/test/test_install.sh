#!/usr/bin/env bash
#
# Unit tests for install.sh + scripts/lib/{firewall,state,uninstall}.sh —
# runs in seconds, no server, no install.
#
# install.sh is source-able: when sourced it defines every function and then
# returns *before* main() (the BASH_SOURCE guard). That lets us exercise the
# Python/Node detection, the firewall abstraction, the install-state tracker,
# and the canonical uninstall routine against throwaway fixtures and PATH stubs
# instead of a real distro and a real /etc.
#
# Each unit-under-test runs in a subshell that re-enables `set -Eeuo pipefail`,
# so an unguarded command silently aborting under set -e is caught here as a
# failed assertion rather than on someone's server.
#
# Run:  bash scripts/test/test_install.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALL_SH="$REPO_DIR/install.sh"
LIB_DIR="$REPO_DIR/scripts/lib"

# Shared stub factories + the fresh-box fixture builder.
# shellcheck source=stubs.sh
source "$SCRIPT_DIR/stubs.sh"

PASS=0
FAIL=0
SKIP=0
ok()   { PASS=$((PASS + 1)); printf '  \033[32m✔\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  \033[31m✘\033[0m %s\n' "$1"; }
skip() { SKIP=$((SKIP + 1)); printf '  \033[33m∼\033[0m %s (skipped)\n' "$1"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --------------------------------------------------------------------------
# PATH stubs.
#   STUB_BIN  — node/npm fakes, added on the global PATH only during T3.
#   PY_STUB   — fake python interpreters, applied ONLY inside T1's subshell so
#               the real python3 stays available for the state.sh test (T6).
# Each python stub echoes a fixed "major.minor" when called with `-c`.
# --------------------------------------------------------------------------
STUB_BIN="$WORK/bin"
PY_STUB="$WORK/pybin"
mkdir -p "$STUB_BIN" "$PY_STUB"
# A no-op docker stub so the uninstall routine's `command -v docker` succeeds and
# its compose-down branch runs deterministically — the CI containers don't have
# docker installed, and we're only asserting on dry-run output anyway.
make_stub_ok "$STUB_BIN" docker
mkpy() {  # mkpy <name> <major.minor>
    {
        printf '#!/usr/bin/env bash\n'
        printf 'if [ "${1:-}" = "-c" ]; then printf "%s"; fi\n' "$2"
    } > "$PY_STUB/$1"
    chmod +x "$PY_STUB/$1"
}
# The newer minor-version stubs report an out-of-range 3.15 (rejected);
# python3.11 is in range; bare python3 is an old 3.10 (rejected). locate_python
# must therefore pick python3.11 — proving it no longer blindly trusts
# `python3`.
#
# The out-of-range stub used to report 3.13, which stopped being out of range
# when the gate moved to cover Debian 13 (issue #99), then 3.14, which stopped
# being out of range when the gate moved to cover Arch rolling (2026-08
# baseline). Pick a version ABOVE PYTHON_MAX, and move it whenever the ceiling
# moves — the point of the stub is "too new", not any particular number. Every
# candidate name locate_python probes is shadowed so a real python3.1x on the
# host can never win over the fixtures.
mkpy python3.14 3.15
mkpy python3.13 3.15
mkpy python3.12 3.15
mkpy python3.11 3.11
mkpy python3     3.10
export PATH="$STUB_BIN:$PATH"

# --------------------------------------------------------------------------
# Source install.sh (functions only). Point the install dir at the sandbox.
# --------------------------------------------------------------------------
export SERVERKIT_DIR="$WORK/opt/serverkit"
# shellcheck disable=SC1090
source "$INSTALL_SH"
set +e +u   # hand control back to the harness; tests re-arm set -e per subshell

printf '\ninstall.sh + lib unit tests\n\n'

# --------------------------------------------------------------------------
# T1 — locate_python prefers a supported minor version over a too-old python3.
# --------------------------------------------------------------------------
# && chain: set -e is suppressed inside an if-condition substitution (see T16).
if ! res="$( set -Eeuo pipefail; PATH="$PY_STUB:$PATH"
             locate_python >/dev/null 2>&1 && printf '%s' "$PYTHON_BIN" )"; then
    bad "locate_python aborted under set -Eeuo pipefail"
elif [ "$res" = "python3.11" ]; then
    ok "locate_python picks python3.11 when python3 is 3.10 and python3.12 is out of range"
else
    bad "locate_python chose [$res], expected python3.11"
fi

# --------------------------------------------------------------------------
# T2 — ver_in_range gate (3.11/3.12/3.13/3.14 accepted, 3.10/3.15 rejected).
#
# 3.13 MUST be accepted: Debian 13 ships only python3.13, and this assertion
# previously required it to be rejected — the suite was actively pinning the
# bug that made trixie uninstallable (issue #99). 3.14 MUST be accepted: Arch/
# Manjaro rolling ship only python 3.14 and carry no versioned fallback
# packages at all (2026-08 compatibility baseline).
# --------------------------------------------------------------------------
if ( set -Eeuo pipefail; ver_in_range 3.11 ) && ( set -Eeuo pipefail; ver_in_range 3.12 ) && \
   ( set -Eeuo pipefail; ver_in_range 3.13 ) && ( set -Eeuo pipefail; ver_in_range 3.14 ) && \
   ! ( set -Eeuo pipefail; ver_in_range 3.10 ) && ! ( set -Eeuo pipefail; ver_in_range 3.15 ); then
    ok "ver_in_range accepts 3.11/3.12/3.13/3.14 and rejects 3.10/3.15"
else
    bad "ver_in_range gate is wrong"
fi

# Debian 13 carries ONLY python3.13, so the ceiling has to include it or the
# installer can never satisfy itself from trixie's repos. Guard the constant
# directly — the gate above would still pass if PYTHON_MAX regressed to 3.12
# and someone "fixed" the assertion to match.
if [ "$PYTHON_MAX" = "3.13" ] || printf '%s\n%s' "3.13" "$PYTHON_MAX" | sort -C -V; then
    ok "PYTHON_MAX ($PYTHON_MAX) covers Debian 13's python3.13"
else
    bad "PYTHON_MAX is $PYTHON_MAX — Debian 13 ships only python3.13 and would fall to a source build"
fi

# Same guard for the rolling ceiling: Arch/Manjaro carry ONLY the newest python
# (3.14 at the 2026-08 baseline) and no versioned fallback packages, so a
# ceiling below it can never be satisfied from their repos.
if [ "$PYTHON_MAX" = "3.14" ] || printf '%s\n%s' "3.14" "$PYTHON_MAX" | sort -C -V; then
    ok "PYTHON_MAX ($PYTHON_MAX) covers Arch rolling's python 3.14"
else
    bad "PYTHON_MAX is $PYTHON_MAX — Arch/Manjaro ship only python 3.14 and would fall to a source build"
fi

# --------------------------------------------------------------------------
# T3 — node_ready gates on vite 8's Node floor (^20.19.0 || >=22.12.0) AND npm
# present. The frontend bundler (rolldown) can't build on older Node — npm
# silently drops its native binary — so a source install must fail fast rather
# than mid-build; 21.x and 22.11 are also rejected (outside the range).
# --------------------------------------------------------------------------
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB_BIN/npm"; chmod +x "$STUB_BIN/npm"
_mk_node_stub() { printf '#!/usr/bin/env bash\necho v%s\n' "$1" > "$STUB_BIN/node"; chmod +x "$STUB_BIN/node"; }
node_gate_ok=1
for v in 20.19.0 20.19.5 22.12.0 22.13.1 24.2.0; do
    _mk_node_stub "$v"
    ( set -Eeuo pipefail; node_ready ) || node_gate_ok=0
done
for v in 16.20.0 18.19.0 20.17.0 20.18.9 21.7.0 22.11.0; do
    _mk_node_stub "$v"
    ( set -Eeuo pipefail; node_ready ) && node_gate_ok=0
done
if [ "$node_gate_ok" = 1 ]; then
    ok "node_ready accepts 20.19+/22.12+ (with npm) and rejects older (incl. v18, 21.x, 22.11)"
else
    bad "node_ready mis-gated a Node version against the vite 8 floor"
fi
rm -f "$STUB_BIN/node" "$STUB_BIN/npm"

# --------------------------------------------------------------------------
# T4 — configure_firewall dry-run prints the exact firewall commands and
# records nothing (FW_DRY_RUN suppresses state writes).
# --------------------------------------------------------------------------
state_file="$WORK/state-t4.json"
if ! out="$(
    set -Eeuo pipefail
    export SERVERKIT_STATE_FILE="$state_file"
    FW_DRY_RUN=1 FIREWALL_BACKEND=firewalld configure_firewall 2>&1
)"; then
    bad "configure_firewall dry-run returned non-zero: [$out]"
elif printf '%s' "$out" | grep -q 'firewall-cmd --permanent --add-port=80/tcp' && \
     printf '%s' "$out" | grep -q 'firewall-cmd --permanent --add-port=443/tcp'; then
    ok "configure_firewall dry-run prints the expected firewalld commands"
else
    bad "configure_firewall dry-run did not print the expected commands"
fi
if [ ! -f "$state_file" ]; then
    ok "configure_firewall dry-run writes no install-state.json"
else
    bad "configure_firewall dry-run unexpectedly wrote state"
fi

# --------------------------------------------------------------------------
# T5 — firewall_detect honors the FIREWALL_BACKEND override.
# --------------------------------------------------------------------------
if ! res="$( set -Eeuo pipefail; source "$LIB_DIR/firewall.sh" \
             && FIREWALL_BACKEND=ufw firewall_detect )"; then
    bad "firewall_detect aborted under set -Eeuo pipefail"
elif [ "$res" = "ufw" ]; then
    ok "firewall_detect honors FIREWALL_BACKEND override"
else
    bad "firewall_detect ignored the override (got [$res])"
fi

# --------------------------------------------------------------------------
# T6 — state.sh roundtrip: set/get scalar, append dedup, list.
# --------------------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    t6_rc=0
    res="$(
        # && chain: this capture sits on the left of `||`, where bash
        # suppresses set -e even inside the substitution (see T16).
        # tr -d '\r' normalizes the CRLF that Python's text-mode stdout emits on
        # Windows; on Linux there is nothing to strip.
        set -Eeuo pipefail
        export SERVERKIT_STATE_FILE="$WORK/state-t6.json"
        source "$LIB_DIR/state.sh" \
            && state_set firewall_backend firewalld \
            && state_append firewall_ports 80/tcp \
            && state_append firewall_ports 443/tcp \
            && state_append firewall_ports 80/tcp `# duplicate — must be ignored` \
            && printf '%s|%s' "$(state_get firewall_backend | tr -d '\r')" \
                "$(state_list firewall_ports | tr -d '\r' | tr '\n' ',')"
    )" || t6_rc=$?
    if [ "$t6_rc" -eq 0 ] && [ "$res" = "firewalld|80/tcp,443/tcp," ]; then
        ok "state.sh set/get/append(dedup)/list roundtrip"
    else
        bad "state.sh roundtrip wrong: rc=$t6_rc got [$res]"
    fi
else
    skip "state.sh roundtrip — python3 unavailable here"
fi

# --------------------------------------------------------------------------
# T7 — the canonical uninstall routine: default preserves data, --purge
# removes it, --keep-data preserves the config dir. All in dry-run.
# --------------------------------------------------------------------------
inst="$WORK/opt/serverkit"
mkdir -p "$inst/scripts/lib"
cp "$LIB_DIR/firewall.sh" "$LIB_DIR/state.sh" "$inst/scripts/lib/"
touch "$inst/docker-compose.yml"

uninstall_out() {  # uninstall_out <extra-env>
    (
        # && chain: called from if-conditions, where set -e is suppressed
        # all the way down (see T16).
        set -Eeuo pipefail
        source "$LIB_DIR/uninstall.sh" \
            && export SERVERKIT_DIR="$inst" SERVERKIT_UNINSTALL_DRY_RUN=1 \
            && eval "$1" \
            && serverkit_uninstall_core 2>&1
    )
}

if ! def_out="$(uninstall_out '')"; then
    bad "uninstall default (dry-run) returned non-zero"
elif printf '%s' "$def_out" | grep -q 'down --remove-orphans' && \
     ! printf '%s' "$def_out" | grep -q 'down -v' && \
     ! printf '%s' "$def_out" | grep -q 'rm -rf /var/lib/serverkit'; then
    ok "uninstall default: compose down WITHOUT -v and keeps /var/lib/serverkit"
else
    bad "uninstall default behaved wrong (deleted data or used -v)"
fi

if ! purge_out="$(uninstall_out 'export SERVERKIT_PURGE=1')"; then
    bad "uninstall --purge (dry-run) returned non-zero"
elif printf '%s' "$purge_out" | grep -q 'down -v' && \
     printf '%s' "$purge_out" | grep -q 'rm -rf /var/lib/serverkit'; then
    ok "uninstall --purge: compose down -v and removes data dirs"
else
    bad "uninstall --purge did not remove volumes/data"
fi

if ! keep_out="$(uninstall_out 'export SERVERKIT_KEEP_DATA=1')"; then
    bad "uninstall --keep-data (dry-run) returned non-zero"
elif printf '%s' "$keep_out" | grep -q 'Preserving data' && \
     ! printf '%s' "$keep_out" | grep -q 'rm -rf /etc/serverkit'; then
    ok "uninstall --keep-data: preserves /etc/serverkit"
else
    bad "uninstall --keep-data removed the config dir"
fi

# --------------------------------------------------------------------------
# T8 — os_family_from: ID mapping + ID_LIKE fallback (incl. rhel-before-fedora).
# --------------------------------------------------------------------------
fam_ok=1
check_fam() { # check_fam <id> <id_like> <expected>
    local got; got="$(os_family_from "$1" "$2")"
    [ "$got" = "$3" ] || { bad "os_family_from('$1','$2') -> [$got], expected $3"; fam_ok=0; }
}
check_fam ubuntu "" debian
check_fam debian "" debian
check_fam rocky "" rhel
check_fam fedora "" fedora
check_fam opensuse-leap "" suse
check_fam arch "" arch
check_fam alpine "" alpine
check_fam gentoo "" gentoo
check_fam funtoo "" gentoo
check_fam mydistro "debian" debian              # unknown ID, debian-like
check_fam clone "rhel centos fedora" rhel        # RHEL clone: rhel wins over fedora
check_fam spin "fedora" fedora                    # pure fedora spin
check_fam suselike "suse opensuse" suse
check_fam wat "" unknown
[ "$fam_ok" = "1" ] && ok "os_family_from maps known IDs and falls back to ID_LIKE (rhel before fedora)"

# --------------------------------------------------------------------------
# T9 — render_service_unit honors a custom SERVERKIT_DIR (no @PLACEHOLDERS@).
# --------------------------------------------------------------------------
inst2="$WORK/custom/srv"
mkdir -p "$inst2/templates"
cp "$REPO_DIR/templates/serverkit-backend.service.in" "$inst2/templates/"
unit_out="$WORK/rendered.service"
t9_rc=0
(
    set -Eeuo pipefail
    INSTALL_DIR="$inst2"; VENV_DIR="$inst2/venv"; LOG_DIR="/var/log/serverkit"
    render_service_unit "$unit_out"
) || t9_rc=$?
if [ "$t9_rc" -eq 0 ] && grep -q "WorkingDirectory=$inst2/backend" "$unit_out" && \
   grep -q "$inst2/venv/bin/gunicorn" "$unit_out" && \
   ! grep -q '@SERVERKIT_DIR@\|@SERVERKIT_VENV_DIR@\|@PORT@' "$unit_out"; then
    ok "render_service_unit substitutes a custom SERVERKIT_DIR and leaves no placeholders"
else
    bad "render_service_unit left placeholders or used the wrong paths"
fi

# --------------------------------------------------------------------------
# T10 — harden_global_tls: conf.d snippet when safe, in-place edit otherwise.
# --------------------------------------------------------------------------
# (A) RHEL-style: no ssl_protocols, has conf.d include → reversible snippet.
ndA="$WORK/nginxA"; mkdir -p "$ndA/conf.d"
printf 'http {\n    include /etc/nginx/conf.d/*.conf;\n}\n' > "$ndA/nginx.conf"
tlsA_rc=0
( set -Eeuo pipefail; SERVERKIT_NGINX_DIR="$ndA" harden_global_tls ) || tlsA_rc=$?
if [ "$tlsA_rc" -eq 0 ] && [ -f "$ndA/conf.d/serverkit-tls.conf" ] && \
   grep -q 'TLSv1.2 TLSv1.3' "$ndA/conf.d/serverkit-tls.conf" && \
   ! grep -q 'ssl_protocols' "$ndA/nginx.conf"; then
    ok "harden_global_tls drops a reversible conf.d snippet when nginx.conf has none"
else
    bad "harden_global_tls should have written a conf.d snippet (RHEL-style)"
fi

# (B) Debian-style: ssl_protocols already present → edit in place, NO snippet
# (a second declaration would be a 'duplicate ssl_protocols' error).
ndB="$WORK/nginxB"; mkdir -p "$ndB/conf.d"
printf 'http {\n    ssl_protocols TLSv1.1 TLSv1.2;\n    include /etc/nginx/conf.d/*.conf;\n}\n' > "$ndB/nginx.conf"
tlsB_rc=0
( set -Eeuo pipefail; SERVERKIT_NGINX_DIR="$ndB" harden_global_tls ) || tlsB_rc=$?
if [ "$tlsB_rc" -eq 0 ] && [ ! -f "$ndB/conf.d/serverkit-tls.conf" ] && \
   grep -q 'ssl_protocols TLSv1.2 TLSv1.3;' "$ndB/nginx.conf"; then
    ok "harden_global_tls edits nginx.conf in place (no duplicate) when ssl_protocols exists"
else
    bad "harden_global_tls should have edited in place (Debian-style)"
fi

# --------------------------------------------------------------------------
# T11 — choose_pkg_manager halts cleanly when no package manager is present
# (empty PATH hides apt/dnf/yum/zypper/pacman/apk). No /etc writes happen
# because the apt branch is never reached.
# --------------------------------------------------------------------------
if ( set -Eeuo pipefail; PATH=""; choose_pkg_manager ) >/dev/null 2>&1; then
    bad "choose_pkg_manager should fail when no package manager exists"
else
    ok "choose_pkg_manager halts cleanly when no package manager is found"
fi

# --------------------------------------------------------------------------
# T12 — docker-compose.yml shape.
#
# Two separate contracts live in this one file, and both have already been
# broken once:
#
#   1. No `backend:` service. On a HOST install the backend manages the host
#      (creates /var/serverkit/apps/*, drives host Docker, reloads nginx),
#      which a container cannot do — a backend container fails app creation
#      with `Permission denied: /var/serverkit/apps/...` and has no host
#      docker.sock. This broke WordPress/app creation once already.
#   2. Plain `docker compose up -d` must start something. Every service was
#      once behind the `legacy-frontend` profile, so the default command
#      started NOTHING while the README still told people to run it — issue
#      #96, where the reporter ran the profile by hand and got an nginx
#      container serving an empty dist directory (403).
# --------------------------------------------------------------------------
compose="$REPO_DIR/docker-compose.yml"
# Service keys inside the `services:` block that carry no `profiles:` — i.e.
# exactly what a plain `docker compose up -d` would start. Literal spaces
# rather than {2}/{4} intervals so this works on any awk.
compose_default_services="$(awk '
    /^services:/ { in_services = 1; next }
    /^[a-zA-Z]/  { in_services = 0 }
    !in_services { next }
    /^  [a-zA-Z0-9_-]+:[ ]*$/ { svc = $1; sub(/:$/, "", svc); order[++n] = svc; cur = svc; next }
    /^    profiles:/ { if (cur != "") profiled[cur] = 1 }
    END { for (i = 1; i <= n; i++) if (!profiled[order[i]]) print order[i] }
' "$compose" | sort | tr '\n' ' ')"

if grep -Eq '^[[:space:]]{2}backend:' "$compose"; then
    bad "docker-compose.yml defines a 'backend' service — the backend must run on the host, not in a container"
elif ! grep -Eq '^[[:space:]]{2}frontend:' "$compose"; then
    bad "docker-compose.yml is missing the legacy 'frontend' service"
elif ! grep -q 'backend:host-gateway' "$compose"; then
    bad "docker-compose.yml frontend is missing the 'backend:host-gateway' alias to reach the host backend"
elif ! awk '/^  frontend:/{f=1} f&&/^    profiles:/{print;exit}' "$compose" | grep -q 'legacy-frontend'; then
    bad "the legacy frontend service is no longer behind the 'legacy-frontend' profile"
else
    ok "docker-compose.yml has no backend service and keeps the legacy frontend profiled"
fi

# `docker compose up -d` must resolve to at least one service. Anything without
# a `profiles:` key starts by default.
if [ -z "${compose_default_services// /}" ]; then
    bad "every docker-compose.yml service is behind a profile — 'docker compose up -d' would start NOTHING (issue #96)"
elif ! grep -Eq '^[[:space:]]{2}serverkit:' "$compose"; then
    bad "docker-compose.yml no longer defines the all-in-one 'serverkit' service"
else
    ok "'docker compose up -d' starts the all-in-one panel [$compose_default_services]"
fi

# The all-in-one image writes its SQLite database to /app/instance. The
# Dockerfile must create that directory and chown it to the non-root runtime
# user, or the container dies on boot with "unable to open database file" —
# and a named volume mounted there inherits root ownership (issue #96).
dockerfile="$REPO_DIR/Dockerfile"
if ! grep -q '/app/instance' "$dockerfile"; then
    bad "Dockerfile never creates /app/instance — the container cannot open its database"
elif ! grep -Eq 'mkdir -p .*/app/instance' "$dockerfile"; then
    bad "Dockerfile mentions /app/instance but does not mkdir it"
else
    ok "Dockerfile creates /app/instance for the database volume"
fi

# --------------------------------------------------------------------------
# T13 — I1: the default no-domain install must survive install_nginx_config_
# for_mode. `[ -n "$PANEL_DOMAIN" ] && printf ...` as the function's LAST
# statement returned 1 on an empty domain and set -e killed every curl-pipe
# install at the nginx phase (shipped broken since v1.6.25).
# --------------------------------------------------------------------------
t="$WORK/t13"; mkdir -p "$t/nginx/sites-available" "$t/nginx/sites-enabled" "$t/cfg"
printf 'server { listen 80; }\n' > "$t/nginx/sites-available/serverkit-insecure.conf"
printf 'ssl_certificate /etc/letsencrypt/live/YOUR_DOMAIN/fullchain.pem;\n' \
    > "$t/nginx/sites-available/serverkit.conf"
if ( set -Eeuo pipefail
     SERVERKIT_NGINX_DIR="$t/nginx" SERVERKIT_CONFIG_DIR="$t/cfg" \
     PANEL_DOMAIN="" SSL_MODE=insecure install_nginx_config_for_mode ) >/dev/null 2>&1; then
    if [ "$(cat "$t/cfg/ssl-mode")" = "insecure" ] && [ ! -f "$t/cfg/panel-domain" ]; then
        ok "install_nginx_config_for_mode survives an empty PANEL_DOMAIN (the v1.6.25 no-domain abort)"
    else
        bad "install_nginx_config_for_mode wrote the wrong mode/domain state for a no-domain install"
    fi
else
    bad "install_nginx_config_for_mode DIED with an empty PANEL_DOMAIN under set -e (I1 regression)"
fi
if ( set -Eeuo pipefail
     SERVERKIT_NGINX_DIR="$t/nginx" SERVERKIT_CONFIG_DIR="$t/cfg" \
     PANEL_DOMAIN="panel.example.com" SSL_MODE=secure install_nginx_config_for_mode ) >/dev/null 2>&1 \
   && grep -q 'live/panel.example.com/' "$t/nginx/sites-available/serverkit.conf" \
   && [ "$(cat "$t/cfg/panel-domain")" = "panel.example.com" ]; then
    ok "install_nginx_config_for_mode (secure) applies the cert path and persists the domain"
else
    bad "install_nginx_config_for_mode (secure) did not apply the cert path / persist the domain"
fi

# --------------------------------------------------------------------------
# T14 — I2: plain Enter at the interactive domain prompt must not abort.
# Same end-of-function `[ -n ] && ...` shape as I1. SERVERKIT_FORCE_PROMPT=1
# lets the test drive the prompt from a pipe instead of a tty.
# --------------------------------------------------------------------------
if printf '\n' | ( set -Eeuo pipefail
                   PANEL_DOMAIN="" SERVERKIT_FORCE_PROMPT=1 prompt_for_domain ) >/dev/null 2>&1; then
    ok "prompt_for_domain survives a blank answer (plain Enter, the no-domain default)"
else
    bad "prompt_for_domain DIED on a blank answer under set -e (I2 regression)"
fi
if ! res="$(printf 'panel.example.com\n' | (
    set -Eeuo pipefail
    PANEL_DOMAIN=""; PANEL_PORT=""; SERVERKIT_FORCE_PROMPT=1
    prompt_for_domain >/dev/null 2>&1 \
        && printf '%s|%s' "$PANEL_DOMAIN" "$PANEL_PORT"
))"; then
    bad "prompt_for_domain aborted on a typed domain under set -Eeuo pipefail"
elif [ "$res" = "panel.example.com|80" ]; then
    ok "prompt_for_domain accepts a typed domain and sets the port"
else
    bad "prompt_for_domain returned [$res], expected panel.example.com|80"
fi

# --------------------------------------------------------------------------
# T15 — I4 (detection half): should_default_to_release must see an EXISTING
# install. The old check probed backend/src (never exists — the tree is
# backend/app), so every re-run picked release mode and fetch_release
# rm -rf'd the live install, .env + SQLite DB included.
# --------------------------------------------------------------------------
t="$WORK/t15"
mkdir -p "$t/fresh" "$t/existing/backend/app" "$t/envonly"
printf 'SECRET_KEY=x\n' > "$t/envonly/.env"
det_ok=1
( set -Eeuo pipefail; INSTALL_DIR="$t/fresh"; BUILD_FROM_SOURCE=0; SERVERKIT_VERSION=""
  should_default_to_release ) || { bad "fresh box should default to a release install"; det_ok=0; }
( set -Eeuo pipefail; INSTALL_DIR="$t/existing"; BUILD_FROM_SOURCE=0; SERVERKIT_VERSION=""
  should_default_to_release ) && { bad "existing backend/app tree must NOT default to release (data loss)"; det_ok=0; }
( set -Eeuo pipefail; INSTALL_DIR="$t/envonly"; BUILD_FROM_SOURCE=0; SERVERKIT_VERSION=""
  should_default_to_release ) && { bad "existing .env must NOT default to release (data loss)"; det_ok=0; }
( set -Eeuo pipefail; INSTALL_DIR="$t/fresh"; BUILD_FROM_SOURCE=1; SERVERKIT_VERSION=""
  should_default_to_release ) && { bad "BUILD_FROM_SOURCE=1 must force the source path"; det_ok=0; }
[ "$det_ok" = "1" ] && ok "should_default_to_release detects an existing install (backend/app or .env)"

# --------------------------------------------------------------------------
# T16 — I4 (preservation half): live state survives a slot rewrite
# byte-identical. First the helpers in isolation, then the real fetch_release
# offline-tarball path over a symlinked blue/green layout (Linux CI; skipped
# where symlinks are unsupported, same convention as test_update.sh T4).
# --------------------------------------------------------------------------
t="$WORK/t16"; mkdir -p "$t/slot/backend/instance"
printf 'SECRET_KEY=sentinel-abc123\nSERVERKIT_ENCRYPTION_KEY=k\n' > "$t/slot/.env"
printf 'SQLITE-SENTINEL-BYTES\n' > "$t/slot/backend/instance/serverkit.db"
cp "$t/slot/.env" "$t/env.orig"
cp "$t/slot/backend/instance/serverkit.db" "$t/db.orig"
if (
    # One && chain, not sequential statements: bash suppresses set -e inside
    # an if-condition subshell, so only an explicit chain makes every
    # assertion gate the result (a cmp-less EL9 CI image proved this —
    # the sibling t16r test passed vacuously while cmp exited 127).
    set -Eeuo pipefail
    stash="$(stash_live_state "$t/slot")" \
        && [ -n "$stash" ] \
        && rm -rf "$t/slot" \
        && mkdir -p "$t/slot/backend" `# fresh tree: no .env, no instance/` \
        && restore_live_state "$stash" "$t/slot" \
        && files_identical "$t/slot/.env" "$t/env.orig" \
        && files_identical "$t/slot/backend/instance/serverkit.db" "$t/db.orig"
) >/dev/null 2>&1; then
    ok "stash/restore_live_state carries .env + instance DB byte-identical across a rewrite"
else
    bad "stash/restore_live_state lost or altered the live state"
fi

t="$WORK/t16r"; slotA="$t/sk-a"; slotB="$t/sk-b"; inst="$t/sk"
mkdir -p "$slotA/backend/instance"
printf 'SECRET_KEY=keep-me\n' > "$slotA/.env"
printf 'DB-SENTINEL\n' > "$slotA/backend/instance/serverkit.db"
cp "$slotA/.env" "$t/env.orig"; cp "$slotA/backend/instance/serverkit.db" "$t/db.orig"
ln -sfn "$slotA" "$inst" 2>/dev/null || true
if [ ! -L "$inst" ]; then
    skip "fetch_release live-state carry — symlinks unsupported here (runs on Linux CI)"
elif ! command -v tar >/dev/null 2>&1; then
    # openSUSE Leap minimal images ship without tar; neither the fixture nor
    # fetch_release itself can run there. Proven on the other 6 distros.
    skip "fetch_release live-state carry — tar unavailable here"
else
    stage="$t/stage"; mkdir -p "$stage/serverkit/backend/app" "$stage/serverkit/scripts"
    printf '#!/bin/sh\n' > "$stage/serverkit/serverkit"
    printf '9.9.9\n' > "$stage/serverkit/VERSION"
    tar czf "$t/rel.tar.gz" -C "$stage" serverkit
    if (
        set -Eeuo pipefail
        INSTALL_DIR="$inst"; DIR_A="$slotA"; DIR_B="$slotB"; FIRST_SLOT="$slotA"
        SERVERKIT_OFFLINE_TARBALL="$t/rel.tar.gz"
        fetch_release \
            && files_identical "$slotA/.env" "$t/env.orig" \
            && files_identical "$slotA/backend/instance/serverkit.db" "$t/db.orig" \
            && [ -d "$slotA/backend/app" ]
    ) >/dev/null 2>&1; then
        ok "fetch_release re-run preserves .env + database byte-identical (the data-loss bug)"
    else
        bad "fetch_release re-run destroyed or altered .env / the database (I4 regression)"
    fi
fi

# --------------------------------------------------------------------------
# T17 — I20: snapshot_existing must refresh a stale .backup instead of
# silently keeping a months-old tree (which revert_install would then
# restore over a much newer install).
# --------------------------------------------------------------------------
t="$WORK/t17"; mkdir -p "$t/inst"
printf 'v1\n' > "$t/inst/marker"
snap_rc=0
( set -Eeuo pipefail; INSTALL_DIR="$t/inst"; snapshot_existing ) >/dev/null 2>&1 || snap_rc=$?
if [ "$snap_rc" -eq 0 ] && [ -f "$t/inst.backup/marker" ] && grep -q v1 "$t/inst.backup/marker"; then
    ok "snapshot_existing creates the first backup"
else
    bad "snapshot_existing rc=$snap_rc or did not create a backup"
fi
printf 'v2\n' > "$t/inst/marker"
snap_rc=0
( set -Eeuo pipefail; INSTALL_DIR="$t/inst"; snapshot_existing ) >/dev/null 2>&1 || snap_rc=$?
if [ "$snap_rc" -eq 0 ] && grep -q v2 "$t/inst.backup/marker" 2>/dev/null && [ ! -d "$t/inst.backup.new" ]; then
    ok "snapshot_existing refreshes a stale .backup on re-run (no months-old rollback source)"
else
    bad "snapshot_existing rc=$snap_rc, kept the stale backup (I20 regression) or left .backup.new behind"
fi

# --------------------------------------------------------------------------
# T18 — I3: the release-venv install must not delete its own copy source. In
# the default layout $VENV_DIR resolves to $FIRST_SLOT/venv, so the old
# rm -rf + cp deleted the venv then failed to copy it. Same-path → keep in
# place; distinct path → still copies.
# --------------------------------------------------------------------------
t="$WORK/t18"; mkdir -p "$t/slot/venv/bin" "$t/elsewhere"
: > "$t/slot/venv/bin/activate"
printf '#!/bin/sh\n' > "$t/slot/venv/bin/python"; chmod +x "$t/slot/venv/bin/python"
if (
    set -Eeuo pipefail
    INSTALL_FROM_RELEASE=1; FIRST_SLOT="$t/slot"; VENV_DIR="$t/slot/venv"
    build_virtualenv \
        && [ -f "$t/slot/venv/bin/activate" ]
) >/dev/null 2>&1; then
    ok "build_virtualenv keeps the venv when source and destination are the same directory"
else
    bad "build_virtualenv deleted its own venv on the same-path layout (I3 regression)"
fi
if (
    set -Eeuo pipefail
    INSTALL_FROM_RELEASE=1; FIRST_SLOT="$t/slot"; VENV_DIR="$t/elsewhere/venv"
    build_virtualenv \
        && [ -f "$t/elsewhere/venv/bin/activate" ] \
        && [ -f "$t/slot/venv/bin/activate" ]
) >/dev/null 2>&1; then
    ok "build_virtualenv still copies the release venv to a distinct VENV_DIR"
else
    bad "build_virtualenv failed to copy the release venv to a custom VENV_DIR"
fi
ln -sfn "$t/slot" "$t/link" 2>/dev/null || true
if [ ! -L "$t/link" ]; then
    skip "build_virtualenv symlinked default layout — symlinks unsupported here (runs on Linux CI)"
else
    if (
        set -Eeuo pipefail
        INSTALL_FROM_RELEASE=1; FIRST_SLOT="$t/slot"; VENV_DIR="$t/link/venv"
        build_virtualenv \
            && [ -x "$t/slot/venv/bin/python" ]
    ) >/dev/null 2>&1; then
        ok "build_virtualenv survives the default symlinked layout (/opt/serverkit → slot)"
    else
        bad "build_virtualenv broke on the symlinked layout (I3 regression)"
    fi
fi

# --------------------------------------------------------------------------
# T19 — I6/I12: pkg_add promises warn-and-continue, so a failing package
# manager must produce a warning and exit 0 — the old body died on the
# out=$(...) assignment under set -e, printing NOTHING. refresh_pkg_index is
# best-effort too, and its yum arm must not use the dnf-only --refresh flag.
# --------------------------------------------------------------------------
t="$WORK/t19"; mkdir -p "$t/bin"
printf '#!/usr/bin/env bash\necho "E: Unable to locate package"\nexit 100\n' > "$t/bin/apt-get"
chmod +x "$t/bin/apt-get"
out="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"; PKG_MGR=apt pkg_add nosuchpkg 2>&1 )"
rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'Could not install: nosuchpkg'; then
    ok "pkg_add warns and returns 0 when the package manager fails (errexit-immune body)"
else
    bad "pkg_add rc=$rc (expected 0) or printed no warning: [$out]"
fi
if ( set -Eeuo pipefail; PATH="$t/bin:$PATH"; PKG_MGR=apt refresh_pkg_index ) >/dev/null 2>&1; then
    ok "refresh_pkg_index is best-effort (a failed index refresh never aborts)"
else
    bad "refresh_pkg_index propagated a failure under set -e (I6 regression)"
fi
if ! grep -qE '^[^#]*yum makecache --refresh' "$INSTALL_SH"; then
    ok "refresh_pkg_index yum arm avoids the dnf-only --refresh flag (I12)"
else
    bad "the yum arm uses 'makecache --refresh' again — that flag is dnf-only (I12 regression)"
fi

# --------------------------------------------------------------------------
# T20 — I5: the RHEL-9 upfront openssh+openssl paired upgrade (drops sshd
# 'OpenSSL version mismatch' on Rocky 9) must run the exact paired
# transaction, survive a failing dnf, and no-op on other families.
# --------------------------------------------------------------------------
t="$WORK/t20"; mkdir -p "$t/bin"
DNF_LOG="$t/dnf.log"; : > "$DNF_LOG"
cat > "$t/bin/dnf" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$DNF_LOG"
exit 1
EOF
chmod +x "$t/bin/dnf"
if ( set -Eeuo pipefail; PATH="$t/bin:$PATH"; OS_FAMILY=rhel upgrade_rhel_crypto_stack ) >/dev/null 2>&1 \
   && grep -q 'upgrade -y openssh openssh-server openssh-clients openssl openssl-libs openssl-devel' "$DNF_LOG"; then
    ok "upgrade_rhel_crypto_stack upgrades openssh+openssl in ONE transaction and survives a failing dnf"
else
    bad "upgrade_rhel_crypto_stack missing/wrong dnf transaction or aborted (I5 regression)"
fi
: > "$DNF_LOG"
rhel_rc=0
( set -Eeuo pipefail; PATH="$t/bin:$PATH"; OS_FAMILY=debian upgrade_rhel_crypto_stack ) >/dev/null 2>&1 || rhel_rc=$?
if [ "$rhel_rc" -eq 0 ] && [ ! -s "$DNF_LOG" ]; then
    ok "upgrade_rhel_crypto_stack is a no-op outside the RHEL family"
else
    bad "upgrade_rhel_crypto_stack rc=$rhel_rc or ran dnf on a non-RHEL family"
fi

# --------------------------------------------------------------------------
# T21 — I7: sync_source needs git but nothing installed it. Without git and
# with the on-demand install failing, sync_source must halt LOUDLY (clear
# message) before ever attempting the clone. Curated PATH: a failing apt-get
# plus a real tail; no git anywhere.
# --------------------------------------------------------------------------
t="$WORK/t21"; mkdir -p "$t/bin"
printf '#!/usr/bin/env bash\nexit 1\n' > "$t/bin/apt-get"; chmod +x "$t/bin/apt-get"
printf '#!/usr/bin/env bash\nexec %s "$@"\n' "$(command -v tail)" > "$t/bin/tail"; chmod +x "$t/bin/tail"
out="$( set -Eeuo pipefail; PATH="$t/bin"; PKG_MGR=apt sync_source 2>&1 )"
rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q 'git is required'; then
    ok "sync_source installs git on demand and halts clearly when that fails (never a cryptic clone error)"
else
    bad "sync_source rc=$rc without the 'git is required' halt: [$out]"
fi

# --------------------------------------------------------------------------
# T22 — I8: locate_python must reject an interpreter whose venv module is
# broken (Debian/Ubuntu minimal without pythonX.Y-venv → 'ensurepip is not
# available' much later), and on Debian-family it must first try to install
# the matching pythonX.Y-venv package.
# --------------------------------------------------------------------------
t="$WORK/t22"; PYX="$t/py"; mkdir -p "$PYX" "$t/bin"
mkvless() {  # mkvless <name> <major.minor> — version OK, `-m venv` broken
    {
        printf '#!/usr/bin/env bash\n'
        printf 'if [ "${1:-}" = "-c" ]; then printf "%s"; exit 0; fi\n' "$2"
        printf 'if [ "${1:-}" = "-m" ]; then exit 1; fi\n'
        printf 'exit 0\n'
    } > "$PYX/$1"
    chmod +x "$PYX/$1"
}
# Every candidate name locate_python probes must be shadowed here — a real
# python3.14 (Manjaro) or python3.13 (Gentoo stage3) earlier in PATH would
# otherwise win over the fixtures and turn this into a host-dependent test.
mkvless python3.14 3.15     # out of range — rejected before the venv probe
mkvless python3.13 3.15     # out of range — rejected before the venv probe
mkvless python3.12 3.12     # in range but venv-less — must be rejected
mkvless python3.11 3.11     # in range but venv-less — must be rejected
mkvless python3     3.10    # too old
if ( set -Eeuo pipefail; PATH="$PYX:$PATH"; locate_python ) >/dev/null 2>&1; then
    bad "locate_python accepted a venv-less interpreter (I8 regression)"
else
    ok "locate_python rejects an interpreter whose '-m venv' is broken"
fi
APT_LOG="$t/apt.log"; : > "$APT_LOG"
cat > "$t/bin/apt-get" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$APT_LOG"
exit 1
EOF
chmod +x "$t/bin/apt-get"
( set -Eeuo pipefail; PATH="$t/bin:$PYX:$PATH"; OS_FAMILY=debian PKG_MGR=apt locate_python ) >/dev/null 2>&1
# Match the PACKAGE, not the exact argv: pkg_add also passes
# -o Dpkg::Options::=… now, and an assertion pinned to `install -y <pkg>`
# breaks on any future flag without the behaviour under test changing.
if grep -qE 'install .*python3\.11-venv' "$APT_LOG"; then
    ok "locate_python (Debian family) tries to install the matching pythonX.Y-venv package"
else
    bad "locate_python never attempted the python3.11-venv fallback; apt saw: $(tr '\n' ';' < "$APT_LOG")"
fi

# --------------------------------------------------------------------------
# T23 — I9: distro-repo Docker (docker.io) has no Docker-repo
# 'docker-compose-plugin'; ensure_compose_plugin must fall back to Ubuntu's
# 'docker-compose-v2' and, when neither lands, warn and continue instead of
# aborting the install.
# --------------------------------------------------------------------------
t="$WORK/t23"; mkdir -p "$t/bin"
APT_LOG="$t/apt.log"; : > "$APT_LOG"
printf '#!/usr/bin/env bash\nexit 1\n' > "$t/bin/docker"; chmod +x "$t/bin/docker"
cat > "$t/bin/apt-get" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$APT_LOG"
exit 100
EOF
chmod +x "$t/bin/apt-get"
out="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"; PKG_MGR=apt ensure_compose_plugin 2>&1 )"
rc=$?
if [ "$rc" -eq 0 ] && grep -qE 'install .*docker-compose-plugin' "$APT_LOG" \
   && grep -qE 'install .*docker-compose-v2' "$APT_LOG" \
   && printf '%s' "$out" | grep -q 'docker compose'; then
    ok "ensure_compose_plugin tries plugin → docker-compose-v2 → warn-and-continue (never aborts)"
else
    bad "ensure_compose_plugin rc=$rc; apt saw: $(tr '\n' ';' < "$APT_LOG")"
fi

# --------------------------------------------------------------------------
# T24 — I10: dnf5 (Fedora 41+) removed `config-manager --add-repo URL`;
# docker_repo_add must fall back to the dnf5 `addrepo --from-repofile=` form,
# and stay warn-and-continue when both forms fail.
# --------------------------------------------------------------------------
t="$WORK/t24"; mkdir -p "$t/bin"
DNF_LOG="$t/dnf.log"; : > "$DNF_LOG"
cat > "$t/bin/dnf" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$DNF_LOG"
case "\$*" in
    "config-manager --add-repo "*) exit 1 ;;   # dnf5: flag removed
    "config-manager addrepo "*)    exit 0 ;;
esac
exit 0
EOF
chmod +x "$t/bin/dnf"
if ( set -Eeuo pipefail; PATH="$t/bin:$PATH"
     docker_repo_add https://example.invalid/docker-ce.repo ) >/dev/null 2>&1 \
   && grep -q 'config-manager addrepo --from-repofile=https://example.invalid/docker-ce.repo' "$DNF_LOG"; then
    ok "docker_repo_add falls back to the dnf5 addrepo syntax when --add-repo fails"
else
    bad "docker_repo_add never tried the dnf5 syntax; dnf saw: $(tr '\n' ';' < "$DNF_LOG")"
fi
printf '#!/usr/bin/env bash\nexit 1\n' > "$t/bin/dnf"; chmod +x "$t/bin/dnf"
if ( set -Eeuo pipefail; PATH="$t/bin:$PATH"
     docker_repo_add https://example.invalid/docker-ce.repo ) >/dev/null 2>&1; then
    ok "docker_repo_add warns and continues when both config-manager forms fail"
else
    bad "docker_repo_add aborted when both config-manager forms failed (must warn-and-continue)"
fi

# --------------------------------------------------------------------------
# T25 — I21: a failing state_set/state_append (e.g. unwritable
# install-state.json) must not abort the firewall phase whose actual firewall
# work already succeeded. The state file is pointed under a regular FILE so
# state.sh's python genuinely fails; ufw is stubbed so firewall_open runs.
# --------------------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    t="$WORK/t25"; mkdir -p "$t/bin"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$t/bin/ufw"; chmod +x "$t/bin/ufw"
    : > "$t/blocker"                       # regular file — state.json can't live under it
    out="$(
        set -Eeuo pipefail
        export PATH="$t/bin:$PATH"
        export SERVERKIT_STATE_FILE="$t/blocker/state.json"
        FIREWALL_BACKEND=ufw configure_firewall 2>&1
    )"
    rc=$?
    if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'Firewall configured'; then
        ok "configure_firewall survives failing state_set/state_append (records are best-effort)"
    else
        bad "configure_firewall rc=$rc when state writes fail (I21 regression): [$out]"
    fi
else
    skip "configure_firewall state-failure guard — python3 unavailable here"
fi

# --------------------------------------------------------------------------
# T26 — regression guards baked into the source (same spirit as
# test_update.sh T26): the audited failure shapes must never creep back in.
# --------------------------------------------------------------------------
guards_ok=1
if grep -qE '^[^#]*backend/src' "$INSTALL_SH"; then
    bad "install.sh probes backend/src again — that path never exists (I4 detection regression)"
    guards_ok=0
fi
if ! grep -qE '^[[:space:]]*upgrade_rhel_crypto_stack$' "$INSTALL_SH"; then
    bad "main() no longer calls upgrade_rhel_crypto_stack (I5 regression)"
    guards_ok=0
fi
[ "$guards_ok" = "1" ] && ok "source guards: no backend/src probe; main runs the RHEL crypto upgrade"

# --------------------------------------------------------------------------
# T27 — I11: nothing may write to the host before the root check. main() must
# run preflight BEFORE choose_pkg_manager (which drops the apt lock-wait file
# into /etc), and inside preflight the root check must come first — so a
# non-root run halts on the friendly message, not a raw "Permission denied".
# Behavioral half is dual: as non-root the friendly halt fires even with NO
# df/free on PATH (the check precedes every probe); as root (distro-container
# CI) preflight passes on stubbed df/free and probes the actual install
# target with `df -Pk` (I14).
# --------------------------------------------------------------------------
main_body="$(awk '/^main\(\) \{/,/^\}/' "$INSTALL_SH")"
pf_at="$(printf '%s\n' "$main_body" | grep -n '^[[:space:]]*preflight$' | cut -d: -f1 | head -1)"
pkg_at="$(printf '%s\n' "$main_body" | grep -n '^[[:space:]]*choose_pkg_manager$' | cut -d: -f1 | head -1)"
if [ -n "$pf_at" ] && [ -n "$pkg_at" ] && [ "$pf_at" -lt "$pkg_at" ]; then
    ok "main() runs preflight (root check) before choose_pkg_manager's host write"
else
    bad "main() order wrong: preflight at [$pf_at], choose_pkg_manager at [$pkg_at] (I11 regression)"
fi
t="$WORK/t27"; mkdir -p "$t/bin" "$t/deep"
if [ "$(id -u)" -ne 0 ]; then
    out="$( set -Eeuo pipefail; PATH="$t/bin"; preflight 2>&1 )"
    rc=$?
    if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q 'as root'; then
        ok "preflight halts a non-root run on the friendly message before any probe or write"
    else
        bad "preflight (non-root) rc=$rc without the friendly root halt (I11 regression): [$out]"
    fi
else
    DF_LOG="$t/df.log"; : > "$DF_LOG"
    cat > "$t/bin/df" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$DF_LOG"
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
printf '/dev/sda1 99999999 1 99999999 1%% /\n'
EOF
    chmod +x "$t/bin/df"
    printf '#!/usr/bin/env bash\nprintf "Mem: 4096 1024 3072 0 0 3072\\n"\n' > "$t/bin/free"
    chmod +x "$t/bin/free"
    out="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"; INSTALL_DIR="$t/deep/nested/target" preflight 2>&1 )"
    rc=$?
    if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'Pre-flight checks passed' && \
       grep -q -- "-Pk $t/deep" "$DF_LOG"; then
        ok "preflight (root) probes the install target's filesystem with df -Pk (I14)"
    else
        bad "preflight (root) rc=$rc; df saw: $(tr '\n' ';' < "$DF_LOG") (I14 regression): [$out]"
    fi
fi

# --------------------------------------------------------------------------
# T28 — I14: `free` is absent from some LXC templates; gauge_memory must
# degrade to no-safe-mode with a warning, not abort on the bare assignment
# under pipefail.
# --------------------------------------------------------------------------
t="$WORK/t28"; mkdir -p "$t/nofree"
if ! res="$( set -Eeuo pipefail; PATH="$t/nofree"; SAFE_MODE=true
             gauge_memory >/dev/null 2>&1 && printf '%s' "$SAFE_MODE" )"; then
    bad "gauge_memory aborted without 'free' under set -Eeuo pipefail (I14 regression)"
elif [ "$res" = "false" ]; then
    ok "gauge_memory degrades gracefully when 'free' is absent (LXC templates)"
else
    bad "gauge_memory aborted or left SAFE_MODE=[$res] without 'free' (I14 regression)"
fi

# --------------------------------------------------------------------------
# T29 — I19: ensure_swap must verify swap actually came up before claiming
# "Swap active." (on btrfs/containers swapon silently fails and the Vite
# build later OOMs), and a full disk (fallocate AND dd fail) must degrade to
# a warning, never an abort.
# --------------------------------------------------------------------------
t="$WORK/t29"; mkdir -p "$t/bin"
printf '#!/usr/bin/env bash\nprintf "Mem: 2048 512 1536 0 0 1536\\nSwap: 0 0 0\\n"\n' > "$t/bin/free"
printf '#!/usr/bin/env bash\nexit 1\n' > "$t/bin/fallocate"
printf '#!/usr/bin/env bash\nexit 1\n' > "$t/bin/dd"
printf '#!/usr/bin/env bash\nexit 1\n' > "$t/bin/mkswap"
printf '#!/usr/bin/env bash\nexit 1\n' > "$t/bin/swapon"
chmod +x "$t/bin/"*
out="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"
        SERVERKIT_SWAPFILE="$t/swapfile" SERVERKIT_PROC_SWAPS="$t/proc_swaps" ensure_swap 2>&1 )"
rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'Could not activate swap' && \
   ! printf '%s' "$out" | grep -q 'Swap active'; then
    ok "ensure_swap warns and continues when no swap can be brought up (full disk / btrfs)"
else
    bad "ensure_swap rc=$rc or claimed swap it never activated (I19 regression): [$out]"
fi
PROCS="$t/proc_swaps2"; : > "$PROCS"
cat > "$t/bin/fallocate" <<'FALLOC_EOF'
#!/usr/bin/env bash
touch "${@: -1}"
FALLOC_EOF
cat > "$t/bin/swapon" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--show" ]; then cat "$PROCS" 2>/dev/null; exit 0; fi
printf '%s\n' "\$1" >> "$PROCS"
EOF
printf '#!/usr/bin/env bash\nexit 0\n' > "$t/bin/mkswap"
chmod +x "$t/bin/"*
if ! out="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"
        SERVERKIT_SWAPFILE="$t/swapfile2" SERVERKIT_PROC_SWAPS="$PROCS" ensure_swap 2>&1 )"; then
    bad "ensure_swap returned non-zero on the success path"
elif printf '%s' "$out" | grep -q 'Swap active' && grep -q "$t/swapfile2" "$PROCS"; then
    ok "ensure_swap claims success only after swapon verifiably activated the file"
else
    bad "ensure_swap did not verify/activate swap on the success path (I19): [$out]"
fi

# --------------------------------------------------------------------------
# T30 — I13: the get.docker.com convenience script (the DEFAULT Debian/Ubuntu
# path) must be staged to a file with curl --retry and run from disk — never
# piped straight into sh — and a failed download must warn and fall back to
# the distro package instead of aborting. Curated PATH: exec-wrappers for the
# real tools plus curl/apt-get stubs, and crucially NO docker (the global
# stub would short-circuit provision_docker).
# --------------------------------------------------------------------------
t="$WORK/t30"; mkdir -p "$t/bin"
# Absolute-path shebangs: this test runs with PATH="$t/bin" ONLY (so that
# `command -v docker` genuinely fails), which means `/usr/bin/env bash` could
# not resolve bash for the stubs themselves.
REAL_BASH="${BASH:-$(command -v bash)}"
for tool in sh mktemp rm tail; do
    printf '#!%s\nexec %s "$@"\n' "$REAL_BASH" "$(command -v "$tool")" > "$t/bin/$tool"
    chmod +x "$t/bin/$tool"
done
CURL_LOG="$t/curl.log"; : > "$CURL_LOG"
cat > "$t/bin/curl" <<EOF
#!$REAL_BASH
printf '%s\n' "\$*" >> "$CURL_LOG"
out=""
while [ \$# -gt 0 ]; do
    if [ "\$1" = "-o" ]; then out="\$2"; shift; fi
    shift
done
if [ -n "\$out" ]; then printf 'echo ran > "%s"\n' "$t/get-docker-ran" > "\$out"; fi
exit 0
EOF
chmod +x "$t/bin/curl"
if ( set -Eeuo pipefail; PATH="$t/bin"
     OS_FAMILY=debian PKG_MGR=apt provision_docker ) >/dev/null 2>&1 \
   && [ -f "$t/get-docker-ran" ] && grep -q -- '--retry 3' "$CURL_LOG"; then
    ok "provision_docker stages get.docker.com to a file with retries and executes it (no curl|sh)"
else
    bad "provision_docker did not stage/run the docker script (I13); curl saw: $(tr '\n' ';' < "$CURL_LOG")"
fi
APT_LOG="$t/apt.log"; : > "$APT_LOG"
printf '#!%s\nexit 22\n' "$REAL_BASH" > "$t/bin/curl"; chmod +x "$t/bin/curl"
cat > "$t/bin/apt-get" <<EOF
#!$REAL_BASH
printf '%s\n' "\$*" >> "$APT_LOG"
exit 100
EOF
chmod +x "$t/bin/apt-get"
out="$( set -Eeuo pipefail; PATH="$t/bin"
        OS_FAMILY=debian PKG_MGR=apt provision_docker 2>&1 )"
rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'get.docker.com' && \
   grep -qE 'install .*docker\.io' "$APT_LOG"; then
    ok "provision_docker warns and falls back to the distro package when the download fails"
else
    bad "provision_docker rc=$rc on a failed download (I13); apt saw: $(tr '\n' ';' < "$APT_LOG"): [$out]"
fi

# --------------------------------------------------------------------------
# T31 — I15: the release tag must come from the releases/latest redirect
# (no API quota) with api.github.com only as a fallback — the 60-req/hr
# unauthenticated API limit is routinely exhausted behind shared cloud NAT —
# and an unresolvable tag must produce a LOUD source-build warning, never the
# old silent downgrade.
# --------------------------------------------------------------------------
t="$WORK/t31"; mkdir -p "$t/bin"
CURL_LOG="$t/curl.log"; : > "$CURL_LOG"
cat > "$t/bin/curl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CURL_LOG"
case "\$*" in
    *github.com/*/releases/latest*) printf 'https://github.com/x/y/releases/tag/v9.9.9'; exit 0 ;;
esac
exit 22
EOF
chmod +x "$t/bin/curl"
if ! res="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"; SERVERKIT_VERSION="" resolve_release_tag )"; then
    bad "resolve_release_tag (redirect path) returned non-zero"
elif [ "$res" = "v9.9.9" ] && ! grep -q 'api.github.com' "$CURL_LOG"; then
    ok "resolve_release_tag reads the tag from the releases/latest redirect (no API quota burned)"
else
    bad "resolve_release_tag primary path returned [$res]; curl saw: $(tr '\n' ';' < "$CURL_LOG")"
fi
: > "$CURL_LOG"
cat > "$t/bin/curl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CURL_LOG"
case "\$*" in
    *api.github.com*) printf '{"tag_name": "v8.8.8",\n'; exit 0 ;;
esac
exit 22
EOF
chmod +x "$t/bin/curl"
if ! res="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"; SERVERKIT_VERSION="" resolve_release_tag )"; then
    bad "resolve_release_tag (API fallback) returned non-zero"
elif [ "$res" = "v8.8.8" ]; then
    ok "resolve_release_tag falls back to the GitHub API when the redirect is unreachable"
else
    bad "resolve_release_tag API fallback returned [$res] (I15)"
fi
printf '#!/usr/bin/env bash\nexit 22\n' > "$t/bin/curl"; chmod +x "$t/bin/curl"
res="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"
        SERVERKIT_OFFLINE_TARBALL=""; SERVERKIT_VERSION=""; INSTALL_FROM_RELEASE=1
        rc=0; fetch_release >"$t/fetch.out" 2>&1 || rc=$?
        printf '%s|%s' "$rc" "$INSTALL_FROM_RELEASE" )"
if [ "$res" = "1|0" ] && grep -q 'SOURCE BUILD' "$t/fetch.out"; then
    ok "fetch_release warns LOUDLY and flips to source when no tag resolves (no silent downgrade)"
else
    bad "fetch_release tag-fallback wrong: state [$res]: $(tr '\n' ';' < "$t/fetch.out" 2>/dev/null)"
fi

# --------------------------------------------------------------------------
# T32 — I16: failures inside fetch_release's unpack/copy (corrupt download,
# disk full) must take the clean fallback-to-source path — return nonzero so
# main()'s existing fallback fires — never a half-copied tree that "succeeds"
# and fails confusingly later. The cp half also proves the live .env is put
# back before handing over (symlink layout: skipped on Git Bash, runs on CI).
# --------------------------------------------------------------------------
t="$WORK/t32"; mkdir -p "$t"
printf 'this is not a tarball\n' > "$t/garbage.tar.gz"
res="$( set -Eeuo pipefail
        INSTALL_DIR="$t/opt/sk"; DIR_A="$t/opt/sk-a"; DIR_B="$t/opt/sk-b"; FIRST_SLOT="$t/opt/sk-a"
        SERVERKIT_OFFLINE_TARBALL="$t/garbage.tar.gz"; INSTALL_FROM_RELEASE=1
        rc=0; fetch_release >"$t/fetch.out" 2>&1 || rc=$?
        printf '%s|%s' "$rc" "$INSTALL_FROM_RELEASE" )"
if [ "$res" = "1|0" ] && grep -q 'falling back to source' "$t/fetch.out"; then
    ok "fetch_release: a failed tar unpack takes the clean source fallback, not a fake success"
else
    bad "fetch_release tar-failure path wrong (I16 regression): [$res]: $(tr '\n' ';' < "$t/fetch.out" 2>/dev/null)"
fi
t="$WORK/t32c"; slotA="$t/sk-a"; slotB="$t/sk-b"; inst="$t/sk"
mkdir -p "$slotA/backend/instance" "$t/bin"
printf 'SECRET_KEY=survive-cp\n' > "$slotA/.env"
cp "$slotA/.env" "$t/env.orig"
ln -sfn "$slotA" "$inst" 2>/dev/null || true
if [ ! -L "$inst" ]; then
    skip "fetch_release cp-failure fallback — symlinks unsupported here (runs on Linux CI)"
elif ! command -v tar >/dev/null 2>&1; then
    skip "fetch_release cp-failure fallback — tar unavailable here"
else
    stage="$t/stage"; mkdir -p "$stage/serverkit/backend/app"
    printf '#!/bin/sh\n' > "$stage/serverkit/serverkit"
    tar czf "$t/rel.tar.gz" -C "$stage" serverkit
    cat > "$t/bin/cp" <<EOF
#!/usr/bin/env bash
last=""
for a in "\$@"; do last="\$a"; done
if [ "\$last" = "$slotA" ]; then exit 1; fi
exec $(command -v cp) "\$@"
EOF
    chmod +x "$t/bin/cp"
    res="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"
            INSTALL_DIR="$inst"; DIR_A="$slotA"; DIR_B="$slotB"; FIRST_SLOT="$slotA"
            SERVERKIT_OFFLINE_TARBALL="$t/rel.tar.gz"; INSTALL_FROM_RELEASE=1
            rc=0; fetch_release >/dev/null 2>&1 || rc=$?
            printf '%s|%s' "$rc" "$INSTALL_FROM_RELEASE" )"
    if [ "$res" = "1|0" ] && files_identical "$slotA/.env" "$t/env.orig"; then
        ok "fetch_release: a failed slot copy restores the live .env and falls back to source"
    else
        bad "fetch_release cp-failure lost live state or faked success (I16 regression): [$res]"
    fi
fi

# --------------------------------------------------------------------------
# T33 — I17: the inline service verbs (mirroring scripts/lib/init.sh, which
# install.sh cannot source pre-clone) must drive systemctl on systemd boxes,
# fall back to OpenRC/SysV elsewhere, and warn-and-continue when nothing can
# be driven. INIT_OVERRIDE pins detection (same contract as init.sh).
# --------------------------------------------------------------------------
t="$WORK/t33"; mkdir -p "$t/sysd" "$t/rc" "$t/none"
SVC_LOG="$t/svc.log"; : > "$SVC_LOG"
cat > "$t/sysd/systemctl" <<EOF
#!/usr/bin/env bash
printf 'systemctl %s\n' "\$*" >> "$SVC_LOG"
EOF
chmod +x "$t/sysd/systemctl"
svc_rc=0
( set -Eeuo pipefail; PATH="$t/sysd:$PATH"
  INIT_OVERRIDE=systemd svc_enable nginx \
      && INIT_OVERRIDE=systemd svc_start nginx ) >/dev/null 2>&1 || svc_rc=$?
if [ "$svc_rc" -eq 0 ] && grep -q 'systemctl enable nginx' "$SVC_LOG" && grep -q 'systemctl start nginx' "$SVC_LOG"; then
    ok "svc_enable/svc_start drive systemctl on systemd boxes"
else
    bad "svc verbs rc=$svc_rc or missed systemctl (I17): $(tr '\n' ';' < "$SVC_LOG")"
fi
: > "$SVC_LOG"
cat > "$t/rc/rc-update" <<EOF
#!/usr/bin/env bash
printf 'rc-update %s\n' "\$*" >> "$SVC_LOG"
EOF
cat > "$t/rc/rc-service" <<EOF
#!/usr/bin/env bash
printf 'rc-service %s\n' "\$*" >> "$SVC_LOG"
EOF
chmod +x "$t/rc/"*
# Full PATH kept (the stubs' env-bash shebang needs it); the override makes
# svc_has_systemd false, so the real systemctl is never consulted.
( set -Eeuo pipefail; PATH="$t/rc:$PATH"
  INIT_OVERRIDE=openrc svc_enable docker; INIT_OVERRIDE=openrc svc_start docker ) >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ] && grep -q 'rc-update add docker default' "$SVC_LOG" && \
   grep -q 'rc-service docker start' "$SVC_LOG"; then
    ok "svc verbs fall back to OpenRC without systemd (no bare-systemctl abort)"
else
    bad "svc OpenRC fallback rc=$rc (I17): $(tr '\n' ';' < "$SVC_LOG")"
fi
out="$( set -Eeuo pipefail; PATH="$t/none"
        INIT_OVERRIDE=none svc_start nginx 2>&1; INIT_OVERRIDE=none svc_enable nginx 2>&1 )"
rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'No init system'; then
    ok "svc verbs warn-and-continue when no init system can be driven (WSL/containers)"
else
    bad "svc verbs rc=$rc with no init system — must warn and return 0 (I17): [$out]"
fi

# --------------------------------------------------------------------------
# T34 — I18: migrating a legacy real-directory install while a stale slot
# from an aborted earlier run exists must not `mv` the live tree INSIDE it
# (/opt/serverkit-a/serverkit); the stale slot is cleared first.
# --------------------------------------------------------------------------
t="$WORK/t34"; mkdir -p "$t/opt/serverkit" "$t/opt/serverkit-a/stale"
printf 'live\n' > "$t/opt/serverkit/marker"
# Deliberately status-unchecked: ensure_install_layout's trailing `ln -s`
# legitimately fails on Git Bash (MSYS "symlinks" are copies, so the -L probe
# is false and the re-link hits an existing dir); the file assertions below
# fully constrain the behaviour under test. On Linux CI the subshell exits 0.
( set -Eeuo pipefail
  INSTALL_DIR="$t/opt/serverkit"; DIR_A="$t/opt/serverkit-a"; DIR_B="$t/opt/serverkit-b"
  FIRST_SLOT="$t/opt/serverkit-a"
  ensure_install_layout ) >/dev/null 2>&1
if [ -f "$t/opt/serverkit-a/marker" ] && [ ! -e "$t/opt/serverkit-a/serverkit" ]; then
    ok "ensure_install_layout clears a stale slot instead of nesting the live tree inside it"
else
    bad "ensure_install_layout nested/lost the live tree on a stale-slot collision (I18 regression)"
fi

# --------------------------------------------------------------------------
# T35 — I22: /etc/os-release defines its own VERSION ("24.04.1 LTS ..."),
# which used to clobber the installer's version string when sourced.
# --------------------------------------------------------------------------
t="$WORK/t35"; mkdir -p "$t"
cat > "$t/os-release" <<'OSR_EOF'
ID=ubuntu
ID_LIKE=debian
PRETTY_NAME="Ubuntu 24.04 LTS"
VERSION="24.04.1 LTS (Noble Numbat)"
OSR_EOF
if ! res="$( set -Eeuo pipefail
        VERSION="sk-sentinel"
        SERVERKIT_OS_RELEASE="$t/os-release" identify_system >/dev/null 2>&1 \
            && printf '%s|%s' "$VERSION" "$OS_FAMILY" )"; then
    bad "identify_system aborted under set -Eeuo pipefail"
elif [ "$res" = "sk-sentinel|debian" ]; then
    ok "identify_system keeps the installer's \$VERSION across the os-release source"
else
    bad "identify_system clobbered VERSION/OS_FAMILY (I22 regression): [$res]"
fi

# --------------------------------------------------------------------------
# T36 — I23: ping_telemetry runs AFTER a successful install; a missing
# VERSION file (pipefail on cat|tr) must never abort it. Empty PATH also
# proves the curl is best-effort.
# --------------------------------------------------------------------------
t="$WORK/t36"; mkdir -p "$t/empty" "$t/noinstall"
if ( set -Eeuo pipefail; PATH="$t/empty"; INSTALL_DIR="$t/noinstall" ping_telemetry ) >/dev/null 2>&1; then
    ok "ping_telemetry survives a missing VERSION file after a successful install"
else
    bad "ping_telemetry DIED on a missing VERSION under pipefail (I23 regression)"
fi

# --------------------------------------------------------------------------
# T37 — I24: write_config's early return must still refresh the SSL-mode key
# in a pre-existing .env — a re-run that gained/lost HTTPS otherwise leaves
# the panel's HSTS gate reading a stale value — without touching the
# irreplaceable generated secrets.
# --------------------------------------------------------------------------
t="$WORK/t37"; mkdir -p "$t/inst"
printf 'SECRET_KEY=sentinel-keep\nSERVERKIT_SSL_MODE=insecure\nPORT=80\n' > "$t/inst/.env"
wc_rc=0
( set -Eeuo pipefail; INSTALL_DIR="$t/inst"; SSL_MODE=secure; write_config ) >/dev/null 2>&1 || wc_rc=$?
if [ "$wc_rc" -eq 0 ] && grep -q '^SERVERKIT_SSL_MODE=secure$' "$t/inst/.env" && \
   grep -q '^SECRET_KEY=sentinel-keep$' "$t/inst/.env" && \
   [ "$(grep -c '^SERVERKIT_SSL_MODE=' "$t/inst/.env")" = "1" ]; then
    ok "write_config early-return refreshes SERVERKIT_SSL_MODE in an existing .env (secrets intact)"
else
    bad "write_config rc=$wc_rc or left a stale/duplicated SSL mode in the existing .env (I24 regression)"
fi
printf 'SECRET_KEY=nokey-line\n' > "$t/inst/.env"
wc_rc=0
( set -Eeuo pipefail; INSTALL_DIR="$t/inst"; SSL_MODE=insecure; write_config ) >/dev/null 2>&1 || wc_rc=$?
if [ "$wc_rc" -eq 0 ] && grep -q '^SERVERKIT_SSL_MODE=insecure$' "$t/inst/.env" && grep -q '^SECRET_KEY=nokey-line$' "$t/inst/.env"; then
    ok "write_config appends SERVERKIT_SSL_MODE when a pre-existing .env lacks it"
else
    bad "write_config rc=$wc_rc or did not add the missing SSL-mode key to a pre-existing .env (I24)"
fi

# --------------------------------------------------------------------------
# T38 — apply_frontend_root: same end-of-function `[ -f ] && ...` species as
# I1/I2, reachable with a custom SERVERKIT_DIR when the last conf is missing.
# --------------------------------------------------------------------------
t="$WORK/t38"; mkdir -p "$t"
printf 'root /opt/serverkit/frontend/dist;\n' > "$t/site.conf"
if ( set -Eeuo pipefail; INSTALL_DIR="$t/custom"
     apply_frontend_root "$t/site.conf" "$t/does-not-exist.conf" ) >/dev/null 2>&1 \
   && grep -q "root $t/custom/frontend/dist;" "$t/site.conf"; then
    ok "apply_frontend_root rewrites the root and survives a missing conf as the last arg"
else
    bad "apply_frontend_root aborted on a missing conf under set -e (the I1/I2 species)"
fi

# --------------------------------------------------------------------------
# T39 — static guards for this slice's one-liner shapes (same spirit as T26).
# --------------------------------------------------------------------------
guards2_ok=1
if grep -qE '^[^#]*get\.docker\.com[^|#]*\|[[:space:]]*sh' "$INSTALL_SH"; then
    bad "install.sh pipes get.docker.com straight into sh again (I13 regression)"
    guards2_ok=0
fi
if grep -qE '^[^#]*df /opt' "$INSTALL_SH"; then
    bad "preflight hardcodes 'df /opt' again — must probe \$INSTALL_DIR with df -Pk (I14 regression)"
    guards2_ok=0
fi
if ! grep -q 'svc_enable nginx' "$INSTALL_SH" || ! grep -q 'svc_start serverkit' "$INSTALL_SH"; then
    bad "nginx/serverkit are enabled/started with bare systemctl again (I17 regression)"
    guards2_ok=0
fi
[ "$guards2_ok" = "1" ] && ok "source guards: no curl|sh docker pipe, no 'df /opt', svc verbs at the call sites"

# --------------------------------------------------------------------------
# T40 — the fresh-minimal-box loop (twin of test_update.sh's; see the
# 2026-07-02 outage note there). EVERY observation/discovery/snapshot/report/
# probe function in install.sh must survive the emptiest valid world a fresh
# box presents — nothing installed yet, zero apps, zero containers, no
# optional confs, dead network, empty state — under set -Eeuo pipefail.
#
# POLICY: every new observation/discovery/snapshot/report function added to
# install.sh MUST be appended here (add args/disposition to the case table
# when needed). Dispositions:
#   must0 — output/result is captured by assignment somewhere (X="$(fn)"), so
#           a non-zero exit IS the outage: the function must exit 0.
#   pred  — used only in conditional position (if fn; ...) or legitimately
#           environment-dependent (root checks, host tool probes): it must
#           merely return normally (no set -u crash, no signal death).
# --------------------------------------------------------------------------
FRESH="$WORK/freshbox"
make_fresh_box_fixture "$FRESH"

# --------------------------------------------------------------------------
# T31 — install profiles: recommend_profile must read the hardware, and the
# minimal profile must genuinely skip Docker. A profile is what we install,
# not a licence tier, so the only thing under test here is "did the right
# provisioning get skipped".
# --------------------------------------------------------------------------
t="$WORK/t31"; mkdir -p "$t/bin"
mkfree() {  # mkfree <total_mb>
    printf '#!/usr/bin/env bash\nprintf "              total\\nMem: %s 0 0\\nSwap: 0 0 0\\n"\n' "$1" \
        > "$t/bin/free"
    chmod +x "$t/bin/free"
}
mknproc() { printf '#!/usr/bin/env bash\nprintf "%s"\n' "$1" > "$t/bin/nproc"; chmod +x "$t/bin/nproc"; }
mkdf() {    # mkdf <avail_gb>
    printf '#!/usr/bin/env bash\nprintf "FS 1G 1G %sG 1%%%% /\\nFS 1G 1G %sG 1%%%% /\\n"\n' "$1" "$1" \
        > "$t/bin/df"
    chmod +x "$t/bin/df"
}

# 700MB box → minimal, no matter how many cores it claims.
mkfree 700; mknproc 8; mkdf 100
res="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"; BASE_DIR="$t"
        RECOMMENDED_PROFILE=""; recommend_profile >/dev/null 2>&1
        printf '%s' "$RECOMMENDED_PROFILE" )"
if [ "$res" = "minimal" ]; then
    ok "recommend_profile: 700MB box → minimal"
else
    bad "recommend_profile: 700MB box gave [$res], expected minimal"
fi

# 2GB / 2 cores → standard.
mkfree 2048; mknproc 2; mkdf 50
res="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"; BASE_DIR="$t"
        RECOMMENDED_PROFILE=""; recommend_profile >/dev/null 2>&1
        printf '%s' "$RECOMMENDED_PROFILE" )"
if [ "$res" = "standard" ]; then
    ok "recommend_profile: 2GB/2-core box → standard"
else
    bad "recommend_profile: 2GB/2-core box gave [$res], expected standard"
fi

# 8GB / 4 cores / 50GB free → full.
mkfree 8192; mknproc 4; mkdf 50
res="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"; BASE_DIR="$t"
        RECOMMENDED_PROFILE=""; recommend_profile >/dev/null 2>&1
        printf '%s' "$RECOMMENDED_PROFILE" )"
if [ "$res" = "full" ]; then
    ok "recommend_profile: 8GB/4-core box → full"
else
    bad "recommend_profile: 8GB/4-core box gave [$res], expected full"
fi

# A roomy box with a nearly-full disk still drops to minimal — images and
# backups need somewhere to land before RAM is the binding constraint.
mkfree 8192; mknproc 8; mkdf 2
res="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"; BASE_DIR="$t"
        RECOMMENDED_PROFILE=""; recommend_profile >/dev/null 2>&1
        printf '%s' "$RECOMMENDED_PROFILE" )"
if [ "$res" = "minimal" ]; then
    ok "recommend_profile: 8GB box with 2GB disk free → minimal"
else
    bad "recommend_profile: low-disk box gave [$res], expected minimal"
fi

# No `free` at all (LXC templates again) must not abort under pipefail.
mkdir -p "$t/empty"
if res="$( set -Eeuo pipefail; PATH="$t/empty"; BASE_DIR="$t"
           RECOMMENDED_PROFILE=""; recommend_profile >/dev/null 2>&1
           printf '%s' "$RECOMMENDED_PROFILE" )"; then
    ok "recommend_profile survives a box with no free/nproc/df (got [$res])"
else
    bad "recommend_profile aborted with no free/nproc/df under set -Eeuo pipefail"
fi

# The whole point of minimal: provision_docker must not touch the box.
out="$( set -Eeuo pipefail; PROFILE=minimal
        provision_docker 2>&1 )"
if printf '%s' "$out" | grep -q 'Skipping Docker'; then
    ok "provision_docker is a no-op on the minimal profile"
else
    bad "provision_docker ran on the minimal profile: [$(printf '%s' "$out" | tail -c 120)]"
fi

out="$( set -Eeuo pipefail; PROFILE=minimal
        ensure_compose_plugin 2>&1 )"
if [ -z "$out" ]; then
    ok "ensure_compose_plugin is a no-op on the minimal profile"
else
    bad "ensure_compose_plugin ran on the minimal profile: [$out]"
fi

# "full" has to earn its name — it was byte-identical to "standard" until
# provision_hardening existed, so the card promised things nothing installed.
pkgt="$WORK/t31pkg"; mkdir -p "$pkgt"
out="$( set -Eeuo pipefail; PROFILE=minimal; OS_FAMILY=debian; PKG_MGR=apt
        SERVERKIT_SKIP_SSL=0
        pkg_add() { printf 'PKGADD:%s\n' "$*"; }
        provision_hardening 2>&1 )"
if [ -z "$out" ]; then
    ok "provision_hardening is a no-op on the minimal profile"
else
    bad "provision_hardening ran on minimal: [$out]"
fi

# fail2ban is standard-and-up (plan 73 item 8): the small VPS is exactly the box
# that gets SSH-brute-forced, so leaving it to "full" meant the typical install
# got no jail at all. certbot stays a "full" extra — HTTPS is optional.
out="$( set -Eeuo pipefail; PROFILE=standard; OS_FAMILY=debian; PKG_MGR=apt
        SERVERKIT_SKIP_SSL=0
        pkg_add() { printf 'PKGADD:%s\n' "$*"; }
        svc_enable() { :; }; svc_start() { :; }
        provision_hardening 2>&1 )"
if printf '%s' "$out" | grep -q 'PKGADD:fail2ban' && \
   ! printf '%s' "$out" | grep -q 'PKGADD:certbot'; then
    ok "provision_hardening installs fail2ban (not certbot) on the standard profile"
else
    bad "standard profile hardening is wrong: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

out="$( set -Eeuo pipefail; PROFILE=full; OS_FAMILY=debian; PKG_MGR=apt
        SERVERKIT_SKIP_SSL=0
        pkg_add() { printf 'PKGADD:%s\n' "$*"; }
        svc_enable() { :; }; svc_start() { :; }
        provision_hardening 2>&1 )"
if printf '%s' "$out" | grep -q 'PKGADD:fail2ban' && \
   printf '%s' "$out" | grep -q 'PKGADD:certbot'; then
    ok "provision_hardening installs fail2ban + certbot on the full profile"
else
    bad "full profile did not provision the hardening stack: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# HTTPS off means certbot is pointless; fail2ban still applies.
out="$( set -Eeuo pipefail; PROFILE=full; OS_FAMILY=debian; PKG_MGR=apt
        SERVERKIT_SKIP_SSL=1
        pkg_add() { printf 'PKGADD:%s\n' "$*"; }
        svc_enable() { :; }; svc_start() { :; }
        provision_hardening 2>&1 )"
if printf '%s' "$out" | grep -q 'PKGADD:fail2ban' && \
   ! printf '%s' "$out" | grep -q 'PKGADD:certbot'; then
    ok "provision_hardening skips certbot when SERVERKIT_SKIP_SSL=1"
else
    bad "SERVERKIT_SKIP_SSL=1 did not suppress certbot: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# A failing package must warn, never abort the install (pkg_add's contract).
if out="$( set -Eeuo pipefail; PROFILE=full; OS_FAMILY=debian; PKG_MGR=apt
           SERVERKIT_SKIP_SSL=0
           pkg_add() { return 0; }
           svc_enable() { :; }; svc_start() { :; }
           provision_hardening 2>&1 )"; then
    ok "provision_hardening survives packages that never appear"
else
    bad "provision_hardening aborted when a hardening package failed to install"
fi

# provision_hardening must run before configure_nginx — certbot has to be on
# the box before the HTTPS attempt, or full silently degrades to plain HTTP.
# Match call lines only — a nearby comment mentioning configure_nginx would
# otherwise satisfy the ordering grep.
if awk '/^main\(\)/,/^}/' "$INSTALL_SH" \
   | grep -E '^[[:space:]]*(provision_hardening|configure_nginx)[[:space:]]*$' \
   | head -2 | tr '\n' ' ' | grep -q 'provision_hardening.*configure_nginx'; then
    ok "main() runs provision_hardening before configure_nginx"
else
    bad "provision_hardening runs after configure_nginx — certbot would arrive too late"
fi

# An explicit SERVERKIT_PROFILE must be honoured verbatim and never prompt.
res="$( set -Eeuo pipefail; PROFILE="full"
        prompt_for_profile >/dev/null 2>&1; printf '%s' "$PROFILE" )"
if [ "$res" = "full" ]; then
    ok "prompt_for_profile honours an explicit SERVERKIT_PROFILE"
else
    bad "prompt_for_profile clobbered an explicit profile: got [$res]"
fi

# A garbage value falls back to detection rather than installing "banana".
mkfree 2048; mknproc 2; mkdf 50
res="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"; BASE_DIR="$t"; PROFILE="banana"
        prompt_for_profile >/dev/null 2>&1 </dev/null; printf '%s' "$PROFILE" )"
if [ "$res" = "standard" ]; then
    ok "prompt_for_profile rejects an unknown SERVERKIT_PROFILE and re-detects"
else
    bad "prompt_for_profile accepted a bogus profile: got [$res]"
fi

# Non-interactive (curl | bash) must take the recommendation, not block.
mkfree 700; mknproc 1; mkdf 20
res="$( set -Eeuo pipefail; PATH="$t/bin:$PATH"; BASE_DIR="$t"; PROFILE=""
        prompt_for_profile >/dev/null 2>&1 </dev/null; printf '%s' "$PROFILE" )"
if [ "$res" = "minimal" ]; then
    ok "prompt_for_profile auto-selects the recommendation with no tty"
else
    bad "prompt_for_profile gave [$res] non-interactively, expected minimal"
fi

INSTALL_OBSERVERS=(
    os_family_from ver_in_range py_venv_ok node_major node_ready
    locate_python gauge_memory identify_system preflight resolve_release_tag
    should_default_to_release svc_has_systemd snapshot_existing
    choose_ssl_mode await_health ping_telemetry
    detect_container recommend_profile
)

loop_fail=0
for fn in "${INSTALL_OBSERVERS[@]}"; do
    args=(); mode=must0
    case "$fn" in
        os_family_from) args=(ubuntu "") ;;
        ver_in_range)   args=(3.11) ;;
        py_venv_ok)     args=(python3); mode=pred ;;
        node_major|node_ready|locate_python|preflight|svc_has_systemd|await_health) mode=pred ;;
    esac
    out="$(
        {
            set -Eeuo pipefail
            export PATH="$FRESH/bin:$PATH"
            INSTALL_DIR="$FRESH/opt/serverkit"; SERVERKIT_DIR="$INSTALL_DIR"
            DIR_A="$FRESH/opt/serverkit-a"; DIR_B="$FRESH/opt/serverkit-b"
            FIRST_SLOT="$DIR_A"
            OS_FAMILY=unknown; PKG_MGR=""; PANEL_DOMAIN=""; SSL_MODE=insecure
            SERVERKIT_SKIP_SSL=0; BUILD_FROM_SOURCE=0; SERVERKIT_VERSION=""
            INSTALL_FROM_RELEASE=0; SERVERKIT_OFFLINE_TARBALL=""; SERVERKIT_MIRROR_URL=""
            SERVERKIT_OS_RELEASE="$FRESH/etc/os-release"
            SERVERKIT_NGINX_DIR="$FRESH/etc/nginx"
            SERVERKIT_CONFIG_DIR="$FRESH/etc/serverkit"
            "$fn" ${args[@]+"${args[@]}"}
        } </dev/null 2>&1
    )"
    rc=$?
    if [ "$mode" = "must0" ] && [ "$rc" -ne 0 ]; then
        bad "fresh-box loop: $fn exited $rc on a fresh box: [$(printf '%s' "$out" | tail -c 160)]"
        loop_fail=1
    elif [ "$mode" = "pred" ] && { [ "$rc" -gt 128 ] || printf '%s' "$out" | grep -q 'unbound variable'; }; then
        bad "fresh-box loop: predicate $fn crashed (rc=$rc) on a fresh box: [$(printf '%s' "$out" | tail -c 160)]"
        loop_fail=1
    fi
done
if [ "$loop_fail" = "0" ]; then
    ok "fresh-box loop: all ${#INSTALL_OBSERVERS[@]} observation/discovery functions survive a fresh box"
fi

# --------------------------------------------------------------------------
# T41 — the source guard must not break piped execution (`curl … | bash`, the
# one-liner README/DEPLOYMENT document). Piped, bash leaves BASH_SOURCE unset,
# so a bare ${BASH_SOURCE[0]} is an unbound variable under `set -u` and aborts
# before main() ever runs — which is exactly what shipped and broke every
# curl-piped install ("line NNNN: BASH_SOURCE[0]: unbound variable").
#
# CI only ever runs `bash install.sh` from a checkout, where BASH_SOURCE IS
# set, so the matrix cannot see this. Running the real installer piped would
# install a server, so exercise the shipped tail — the guard plus the `main
# "$@"` dispatch, extracted verbatim — against a harmless main() stub.
# --------------------------------------------------------------------------
guard_ln="$(grep -n 'BASH_SOURCE' "$INSTALL_SH" | grep -vE ':[[:space:]]*#' | tail -1 | cut -d: -f1)"
guard_tail="$WORK/t41_tail.sh"
{
    printf 'set -euo pipefail\n'
    printf 'main() { printf "REACHED_MAIN"; }\n'
    sed -n "${guard_ln},\$p" "$INSTALL_SH"
} > "$guard_tail"

out="$(bash < "$guard_tail" 2>&1)"; rc=$?
if [ "$rc" -eq 0 ] && [ "$out" = "REACHED_MAIN" ]; then
    ok "piped execution (stdin, as curl|bash) still reaches main()"
else
    bad "piped execution broken by the source guard: rc=$rc out=[$out]"
fi

# The other half of the contract: a real `source` must still return early, or
# this whole suite would run an install instead of unit-testing one.
out="$( set -euo pipefail; source "$guard_tail"; printf 'EARLY_RETURN' )"
if [ "$out" = "EARLY_RETURN" ]; then
    ok "sourcing still returns before main()"
else
    bad "the source guard no longer short-circuits a source: out=[$out]"
fi

# --------------------------------------------------------------------------
# T42 — plan 73 item 8: a stock Debian/Ubuntu box gets a real firewall, and the
# SSH port is discovered BEFORE default-deny is switched on.
#
# This is the single most dangerous thing the installer does: enabling
# default-deny without allowing the port sshd actually answers on locks the
# operator out over the very session running the install. Every case below is a
# lockout-safety assertion.
#
# `sshd` and `ss` are stubbed to fail throughout so detection is forced down to
# the config-file parser and the fixtures decide the answer — otherwise the
# result would depend on whether the CI runner happens to run sshd.
# --------------------------------------------------------------------------
t42="$WORK/t42"; mkdir -p "$t42/bin" "$t42/ssh/sshd_config.d" "$t42/state"
make_stub_exit "$t42/bin" 1 sshd ss

# -- detection: a drop-in Port wins over a commented-out one in the main file.
printf '#Port 22\nPermitRootLogin no\n'  > "$t42/ssh/sshd_config"
printf 'Port 2222\n'                     > "$t42/ssh/sshd_config.d/99-port.conf"
res="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
        SSHD_CONFIG_ROOT="$t42/ssh" detect_ssh_ports | tr '\n' ' ' )"
if [ "$res" = "2222 " ]; then
    ok "detect_ssh_ports reads sshd_config.d drop-ins and ignores commented Ports"
else
    bad "detect_ssh_ports got [$res], expected the drop-in's 2222"
fi

# -- detection: several Port lines all come back, de-duplicated.
printf 'Port 22\nPort 2222\nPort 22\n' > "$t42/ssh/sshd_config"
: > "$t42/ssh/sshd_config.d/99-port.conf"
res="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
        SSHD_CONFIG_ROOT="$t42/ssh" detect_ssh_ports | tr '\n' ' ' )"
if [ "$res" = "22 2222 " ]; then
    ok "detect_ssh_ports returns every declared port, de-duplicated"
else
    bad "detect_ssh_ports got [$res], expected [22 2222 ]"
fi

# -- detection: NO sshd_config.d directory at all — the common shape, and the
#    one that bit hardest. The unmatched glob makes `cat` exit non-zero; under
#    the installer's `set -o pipefail` that wiped the parsed answer and the
#    function fell through to its "22" default. On a box whose sshd is on 2222
#    that means UFW allows the wrong port and locks the operator out. Pinning
#    it here because the failure only ever shows up on a real server.
t42b="$WORK/t42b"; mkdir -p "$t42b/ssh"
printf 'Port 2222\n' > "$t42b/ssh/sshd_config"
res="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
        SSHD_CONFIG_ROOT="$t42b/ssh" detect_ssh_ports | tr '\n' ' ' )"
if [ "$res" = "2222 " ]; then
    ok "detect_ssh_ports reads sshd_config with no drop-in dir (pipefail regression)"
else
    bad "detect_ssh_ports got [$res] with no sshd_config.d — expected 2222, LOCKOUT RISK"
fi

# -- detection: a config that names no Port means sshd's built-in default.
printf 'PermitRootLogin no\n' > "$t42/ssh/sshd_config"
res="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
        SSHD_CONFIG_ROOT="$t42/ssh" detect_ssh_ports | tr '\n' ' ' )"
if [ "$res" = "22 " ]; then
    ok "detect_ssh_ports falls back to 22 when the config names no Port"
else
    bad "detect_ssh_ports got [$res] for a Port-less config, expected 22"
fi

# -- detection: nothing to go on at all → NON-ZERO. This is the fail-safe the
#    bootstrap keys off; if it ever starts guessing, the guard is gone.
if ( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
     SSHD_CONFIG_ROOT="$t42/nonexistent" detect_ssh_ports >/dev/null 2>&1 ); then
    bad "detect_ssh_ports invented an SSH port with no config, no sshd and no ss"
else
    ok "detect_ssh_ports fails loudly when no source can name a port"
fi

# -- bootstrap: the happy path. Allow rules for SSH/80/443 must all be issued
#    BEFORE `ufw default deny incoming`, and default-deny before `ufw enable`.
printf 'Port 2222\n' > "$t42/ssh/sshd_config"
out="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
        OS_FAMILY=debian; PANEL_PORT=80; FW_DRY_RUN=1; FIREWALL_BACKEND=none
        SSHD_CONFIG_ROOT="$t42/ssh"
        detect_container() { printf ''; }
        pkg_add() { :; }
        source "$LIB_DIR/firewall.sh"
        command -v ufw >/dev/null 2>&1 || ufw() { :; }
        bootstrap_firewall 2>&1 )"
allow_ssh_ln="$(printf '%s\n' "$out" | grep -n 'ufw allow 2222/tcp'   | head -1 | cut -d: -f1)"
allow_80_ln="$( printf '%s\n' "$out" | grep -n 'ufw allow 80/tcp'     | head -1 | cut -d: -f1)"
allow_443_ln="$(printf '%s\n' "$out" | grep -n 'ufw allow 443/tcp'    | head -1 | cut -d: -f1)"
deny_ln="$(     printf '%s\n' "$out" | grep -n 'ufw default deny incoming' | head -1 | cut -d: -f1)"
enable_ln="$(   printf '%s\n' "$out" | grep -n 'ufw --force enable'   | head -1 | cut -d: -f1)"
if [ -n "$allow_ssh_ln" ] && [ -n "$allow_80_ln" ] && [ -n "$allow_443_ln" ] \
   && [ -n "$deny_ln" ] && [ -n "$enable_ln" ] \
   && [ "$allow_ssh_ln" -lt "$deny_ln" ] && [ "$allow_80_ln" -lt "$deny_ln" ] \
   && [ "$allow_443_ln" -lt "$deny_ln" ] && [ "$deny_ln" -lt "$enable_ln" ]; then
    ok "bootstrap_firewall allows SSH/80/443 before default-deny, and denies before enabling"
else
    bad "bootstrap_firewall rule ordering is unsafe: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# -- bootstrap: the interlock. No discoverable SSH port ⇒ NOTHING is enabled.
out="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
        OS_FAMILY=debian; FW_DRY_RUN=1; FIREWALL_BACKEND=none
        SSHD_CONFIG_ROOT="$t42/nonexistent"
        detect_container() { printf ''; }
        pkg_add() { :; }
        source "$LIB_DIR/firewall.sh"
        command -v ufw >/dev/null 2>&1 || ufw() { :; }
        bootstrap_firewall 2>&1 )"
if ! printf '%s' "$out" | grep -q 'ufw --force enable' \
   && ! printf '%s' "$out" | grep -q 'default deny' \
   && printf '%s' "$out" | grep -qi 'NOT enabling'; then
    ok "bootstrap_firewall refuses to enable a firewall when the SSH port is unknown"
else
    bad "bootstrap_firewall enabled default-deny without knowing the SSH port: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# -- bootstrap: a non-standard PANEL_PORT is allowed too, or default-deny takes
#    the panel UI down with it.
printf 'Port 22\n' > "$t42/ssh/sshd_config"
out="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
        OS_FAMILY=debian; PANEL_PORT=8443; FW_DRY_RUN=1; FIREWALL_BACKEND=none
        SSHD_CONFIG_ROOT="$t42/ssh"
        detect_container() { printf ''; }
        pkg_add() { :; }
        source "$LIB_DIR/firewall.sh"
        command -v ufw >/dev/null 2>&1 || ufw() { :; }
        bootstrap_firewall 2>&1 )"
if printf '%s' "$out" | grep -q 'ufw allow 8443/tcp'; then
    ok "bootstrap_firewall opens a non-standard PANEL_PORT before default-deny"
else
    bad "bootstrap_firewall would lock the panel out on PANEL_PORT=8443: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# --------------------------------------------------------------------------
# "Is this box already protected?" — the question the bootstrap's skip-guard
# has to ask. It originally asked firewall_detect() != none, which answers a
# DIFFERENT question ("which backend do I open a port through") and returns
# `nftables` for a bare nft binary with an empty ruleset. Every case below was
# written after a real Ubuntu 24.04 VM install logged "via nftables" while the
# bootstrap printed nothing at all.
# --------------------------------------------------------------------------
fw_probe_dir() {  # fw_probe_dir <name> -> fresh bin dir on stdout
    local d="$t42/fw-$1"; rm -rf "$d"; mkdir -p "$d"; printf '%s' "$d"
}
run_bootstrap() {  # run_bootstrap <bindir>
    ( set -Eeuo pipefail; PATH="$1:$PATH"
      OS_FAMILY=debian; FW_DRY_RUN=1
      SSHD_CONFIG_ROOT="$t42/ssh"
      detect_container() { printf ''; }
      pkg_add() { :; }
      source "$LIB_DIR/firewall.sh"
      bootstrap_firewall 2>&1 )
}

# -- The two predicates answer DIFFERENT questions, and conflating them is what
#    broke this. Pinned at the predicate level so the distinction survives any
#    future refactor of either one.
d="$(fw_probe_dir predicates)"
make_stub_stdout "$d" nft 0 </dev/null
make_stub_stdout "$d" ufw 0 <<'EOF'
Status: inactive
EOF
res="$( set -Eeuo pipefail; PATH="$d:$PATH"
        source "$LIB_DIR/firewall.sh"
        printf '%s/' "$(firewall_detect)"
        firewall_inbound_default_deny && printf 'protected' || printf 'unprotected' )"
if [ "$res" = "nftables/unprotected" ]; then
    ok "firewall_detect names a backend where inbound_default_deny sees no protection"
else
    bad "expected [nftables/unprotected], got [$res]"
fi

# -- THE REGRESSION: stock cloud image — nft binary present, ruleset EMPTY, ufw
#    installed but inactive. Nothing is filtering, so the bootstrap MUST run.
d="$(fw_probe_dir empty)"
make_stub_stdout "$d" nft 0 </dev/null
make_stub_stdout "$d" ufw 0 <<'EOF'
Status: inactive
EOF
out="$(run_bootstrap "$d")"
if printf '%s' "$out" | grep -q 'ufw allow'; then
    ok "bootstrap_firewall runs on a box whose nft ruleset is empty (stock-image regression)"
else
    bad "bootstrap_firewall skipped an UNPROTECTED box: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# -- Docker's chains are not a host firewall. A box running Docker has plenty of
#    nft rules and still accepts every inbound port.
d="$(fw_probe_dir docker)"
make_stub_stdout "$d" nft 0 <<'EOF'
table ip nat {
	chain DOCKER {
		iifname "docker0" counter packets 0 bytes 0 return
	}
}
table ip filter {
	chain FORWARD {
		type filter hook forward priority filter; policy drop;
	}
}
EOF
make_stub_stdout "$d" ufw 0 <<'EOF'
Status: inactive
EOF
out="$(run_bootstrap "$d")"
if printf '%s' "$out" | grep -q 'ufw allow'; then
    ok "bootstrap_firewall is not fooled by Docker's nftables chains"
else
    bad "Docker's rules read as a protected box: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# -- A real default-deny nft policy on the INPUT hook IS protection: hands off.
d="$(fw_probe_dir nftdeny)"
make_stub_stdout "$d" nft 0 <<'EOF'
table inet filter {
	chain input {
		type filter hook input priority filter; policy drop;
		tcp dport 22 accept
	}
}
EOF
out="$(run_bootstrap "$d")"
if printf '%s' "$out" | grep -qi 'already filtered'; then
    ok "bootstrap_firewall leaves a real nftables default-deny alone"
else
    bad "bootstrap_firewall did not respect an nft input policy drop: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# -- An ALREADY-ACTIVE ufw is the operator's. Open ports on it
#    (configure_firewall's job), never rewrite its default policy.
d="$(fw_probe_dir ufwactive)"
make_stub_stdout "$d" ufw 0 <<'EOF'
Status: active
Default: deny (incoming), allow (outgoing), disabled (routed)
EOF
out="$(run_bootstrap "$d")"
if printf '%s' "$out" | grep -qi 'already filtered' \
   && ! printf '%s' "$out" | grep -q 'ufw default deny'; then
    ok "bootstrap_firewall leaves an already-active firewall's policy alone"
else
    bad "bootstrap_firewall touched an existing firewall: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# -- ufw installed and active but ALLOWING inbound is not protection.
d="$(fw_probe_dir ufwallow)"
make_stub_stdout "$d" ufw 0 <<'EOF'
Status: active
Default: allow (incoming), allow (outgoing), disabled (routed)
EOF
out="$(run_bootstrap "$d")"
if printf '%s' "$out" | grep -q 'ufw default deny'; then
    ok "bootstrap_firewall fixes an active ufw that still allows inbound"
else
    bad "an allow-inbound ufw read as protected: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# -- bootstrap: non-Debian is a no-op (UFW is Debian/Ubuntu; RHEL boots with
#    firewalld active and is handled by configure_firewall's open step).
out="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
        OS_FAMILY=rhel; FW_DRY_RUN=1; FIREWALL_BACKEND=none
        SSHD_CONFIG_ROOT="$t42/ssh"
        detect_container() { printf ''; }
        pkg_add() { :; }
        source "$LIB_DIR/firewall.sh"
        bootstrap_firewall 2>&1 )"
if [ -z "$out" ]; then
    ok "bootstrap_firewall is a no-op outside the Debian family"
else
    bad "bootstrap_firewall tried to install UFW on a non-Debian box: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# -- bootstrap: SERVERKIT_SKIP_FIREWALL=1 and containers are both hands-off.
out="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
        OS_FAMILY=debian; FW_DRY_RUN=1; FIREWALL_BACKEND=none
        SERVERKIT_SKIP_FIREWALL=1; SSHD_CONFIG_ROOT="$t42/ssh"
        detect_container() { printf ''; }
        pkg_add() { :; }
        source "$LIB_DIR/firewall.sh"
        bootstrap_firewall 2>&1 )"
out2="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
         OS_FAMILY=debian; FW_DRY_RUN=1; FIREWALL_BACKEND=none
         SSHD_CONFIG_ROOT="$t42/ssh"
         detect_container() { printf 'lxc'; }
         pkg_add() { :; }
         source "$LIB_DIR/firewall.sh"
         bootstrap_firewall 2>&1 )"
if ! printf '%s' "$out" | grep -q 'ufw' && ! printf '%s' "$out2" | grep -q 'ufw allow'; then
    ok "bootstrap_firewall honours SERVERKIT_SKIP_FIREWALL and skips containers"
else
    bad "bootstrap_firewall ignored an opt-out: [$out] [$out2]"
fi

# -- the re-run guard: we bootstrapped once, the operator switched the firewall
#    off, a later install must NOT switch it back on.
out="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
        OS_FAMILY=debian; FW_DRY_RUN=1; FIREWALL_BACKEND=none
        SSHD_CONFIG_ROOT="$t42/ssh"
        detect_container() { printf ''; }
        pkg_add() { :; }
        source "$LIB_DIR/firewall.sh"
        state_get() { [ "$1" = "firewall_bootstrapped" ] && printf '1' || printf ''; }
        bootstrap_firewall 2>&1 )"
if ! printf '%s' "$out" | grep -q 'ufw --force enable'; then
    ok "bootstrap_firewall does not re-enable a firewall the operator turned off"
else
    bad "bootstrap_firewall re-enabled a deliberately disabled firewall: [$(printf '%s' "$out" | tr '\n' ' ')]"
fi

# -- the sshd jail watches the port sshd actually uses, not a hardcoded 22.
printf 'Port 2222\n' > "$t42/ssh/sshd_config"
jail_dir="$t42/jail.d"
out="$( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
        SSHD_CONFIG_ROOT="$t42/ssh"; FAIL2BAN_JAIL_DIR="$jail_dir"
        write_sshd_jail 2>&1 )"
if [ -f "$jail_dir/sshd-serverkit.conf" ] \
   && grep -q '^\[sshd\]'        "$jail_dir/sshd-serverkit.conf" \
   && grep -q '^enabled  = true' "$jail_dir/sshd-serverkit.conf" \
   && grep -q '^port     = 2222' "$jail_dir/sshd-serverkit.conf"; then
    ok "write_sshd_jail enables the sshd jail on the detected SSH port"
else
    bad "write_sshd_jail wrote the wrong jail: [$(cat "$jail_dir/sshd-serverkit.conf" 2>/dev/null | tr '\n' ' ')] [$out]"
fi

# Re-running must not append or duplicate — the file is regenerated whole.
( set -Eeuo pipefail; PATH="$t42/bin:$PATH"
  SSHD_CONFIG_ROOT="$t42/ssh"; FAIL2BAN_JAIL_DIR="$jail_dir"
  write_sshd_jail >/dev/null 2>&1 )
if [ "$(grep -c '^\[sshd\]' "$jail_dir/sshd-serverkit.conf")" = "1" ]; then
    ok "write_sshd_jail is idempotent across re-runs"
else
    bad "write_sshd_jail duplicated the jail stanza on a re-run"
fi

# -- static guard: the interlock must stay wired. bootstrap_firewall has to call
#    detect_ssh_ports, and the `ufw --force enable` line must live after it.
if awk '/^bootstrap_firewall\(\)/,/^}/' "$INSTALL_SH" | grep -q 'detect_ssh_ports'; then
    ok "bootstrap_firewall still consults detect_ssh_ports before enabling anything"
else
    bad "bootstrap_firewall no longer detects the SSH port — LOCKOUT RISK"
fi

# --------------------------------------------------------------------------
# T43 — plan 71: running behind an operator's own reverse proxy.
#
# Issue #96. Two switches, and the failure modes are silent ones: a panel that
# still 301s to HTTPS traps the proxy in a redirect loop, and a missing
# SERVERKIT_PUBLIC_URL leaves the UI loading but never live-updating because
# every websocket handshake is rejected on origin.
# --------------------------------------------------------------------------
printf '\nT43 — external reverse proxy (SERVERKIT_EXTERNAL_PROXY / SERVERKIT_CONFIG)\n'
t43="$WORK/t43"
mkdir -p "$t43"

# -- EXTERNAL_PROXY implies SKIP_SSL. Sourcing install.sh re-runs the settings
#    block, so the implication is observable without running an install.
out="$( set -Eeuo pipefail
        SERVERKIT_EXTERNAL_PROXY=1
        unset SERVERKIT_SKIP_SSL || true
        source "$INSTALL_SH" >/dev/null 2>&1
        printf '%s' "$SERVERKIT_SKIP_SSL" )"
if [ "$out" = "1" ]; then
    ok "SERVERKIT_EXTERNAL_PROXY=1 implies SERVERKIT_SKIP_SSL=1 (no HTTPS redirect to loop on)"
else
    bad "SERVERKIT_EXTERNAL_PROXY=1 left SERVERKIT_SKIP_SSL=[$out] — the panel would still 301 to HTTPS"
fi

# -- and it must NOT flip SKIP_SSL for an ordinary install.
out="$( set -Eeuo pipefail
        unset SERVERKIT_EXTERNAL_PROXY SERVERKIT_SKIP_SSL || true
        source "$INSTALL_SH" >/dev/null 2>&1
        printf '%s' "$SERVERKIT_SKIP_SSL" )"
if [ "$out" = "0" ]; then
    ok "the default install is untouched (SERVERKIT_SKIP_SSL=0)"
else
    bad "an ordinary install now skips SSL (SERVERKIT_SKIP_SSL=[$out])"
fi

# -- a trailing slash on the public URL must be stripped: a browser's Origin
#    header never carries one, so storing it raw would never match.
out="$( set -Eeuo pipefail
        SERVERKIT_EXTERNAL_PROXY=1
        SERVERKIT_PUBLIC_URL="https://panel.example.com/"
        source "$INSTALL_SH" >/dev/null 2>&1
        printf '%s' "$PANEL_PUBLIC_URL" )"
if [ "$out" = "https://panel.example.com" ]; then
    ok "a trailing slash is stripped from SERVERKIT_PUBLIC_URL"
else
    bad "SERVERKIT_PUBLIC_URL normalised to [$out]"
fi

# -- SERVERKIT_CONFIG supplies defaults...
cat > "$t43/install.conf" <<'CONF'
# ServerKit install config
SERVERKIT_EXTERNAL_PROXY=1
SERVERKIT_PUBLIC_URL="https://from-file.example.com"
SERVERKIT_PROFILE=minimal
CONF
out="$( set -Eeuo pipefail
        unset SERVERKIT_EXTERNAL_PROXY SERVERKIT_PUBLIC_URL SERVERKIT_PROFILE || true
        SERVERKIT_CONFIG="$t43/install.conf"
        source "$INSTALL_SH" >/dev/null 2>&1
        printf '%s|%s|%s' "$SERVERKIT_EXTERNAL_PROXY" "$PANEL_PUBLIC_URL" "$PROFILE" )"
if [ "$out" = "1|https://from-file.example.com|minimal" ]; then
    ok "SERVERKIT_CONFIG supplies defaults (quotes stripped, comments ignored)"
else
    bad "SERVERKIT_CONFIG produced [$out]"
fi

# -- ...but explicit environment always wins over the file.
out="$( set -Eeuo pipefail
        unset SERVERKIT_EXTERNAL_PROXY || true
        SERVERKIT_CONFIG="$t43/install.conf"
        SERVERKIT_PUBLIC_URL="https://from-env.example.com"
        SERVERKIT_PROFILE=full
        source "$INSTALL_SH" >/dev/null 2>&1
        printf '%s|%s' "$PANEL_PUBLIC_URL" "$PROFILE" )"
if [ "$out" = "https://from-env.example.com|full" ]; then
    ok "explicit environment overrides SERVERKIT_CONFIG"
else
    bad "SERVERKIT_CONFIG overrode the explicit environment: [$out]"
fi

# -- a missing config file is a hard error, not a silently-default install.
if ( set -Eeuo pipefail
     SERVERKIT_CONFIG="$t43/nope.conf"
     source "$INSTALL_SH" >/dev/null 2>&1 ) ; then
    bad "a missing SERVERKIT_CONFIG file was ignored"
else
    ok "a missing SERVERKIT_CONFIG file aborts instead of installing the wrong shape"
fi

# -- the file must be parsed, not executed: a command in it must not run.
printf 'touch %s/pwned\nSERVERKIT_PROFILE=minimal\n' "$t43" > "$t43/evil.conf"
( set -Eeuo pipefail
  unset SERVERKIT_PROFILE || true
  SERVERKIT_CONFIG="$t43/evil.conf"
  source "$INSTALL_SH" >/dev/null 2>&1 ) || true
if [ -e "$t43/pwned" ]; then
    bad "SERVERKIT_CONFIG executed a command from the file — it must be parsed, not sourced"
else
    ok "SERVERKIT_CONFIG is parsed, not sourced (a stray command does not run as root)"
fi

# -- write_config on an EXISTING .env must apply the proxy settings, because
#    "convert my install to sit behind Caddy" is a re-run and lands there.
env_dir="$t43/opt"
mkdir -p "$env_dir"
cat > "$env_dir/.env" <<'ENVEOF'
SECRET_KEY=keep-me
SERVERKIT_SSL_MODE=secure
# TRUST_PROXY_HEADERS=false
ENVEOF
( set -Eeuo pipefail
  source "$INSTALL_SH" >/dev/null 2>&1
  INSTALL_DIR="$env_dir"; SSL_MODE=insecure; PROFILE=standard
  SERVERKIT_EXTERNAL_PROXY=1; PANEL_PUBLIC_URL="https://panel.example.com"
  write_config >/dev/null 2>&1 )
if grep -q '^TRUST_PROXY_HEADERS=true' "$env_dir/.env" \
   && grep -q '^TRUSTED_PROXY_HOPS=2' "$env_dir/.env" \
   && grep -q '^SERVERKIT_PUBLIC_URL=https://panel.example.com$' "$env_dir/.env" \
   && grep -q '^SECRET_KEY=keep-me' "$env_dir/.env" \
   && [ "$(grep -c 'TRUST_PROXY_HEADERS' "$env_dir/.env")" = "1" ]; then
    ok "converting an existing install rewrites the proxy keys and keeps the secrets"
else
    bad "existing-.env conversion produced: [$(tr '\n' ' ' < "$env_dir/.env")]"
fi

# -- and SERVERKIT_SSL_MODE must flip to insecure in the .env, not only in
#    /etc/serverkit/ssl-mode. _resolve_ssl_mode() reads the ENV VAR FIRST and
#    only falls back to the file, and run.py load_dotenv()s this file — so a
#    stale SERVERKIT_SSL_MODE=secure here keeps HSTS on and tells browsers to
#    force HTTPS on a port that now only speaks plain HTTP.
if grep -q '^SERVERKIT_SSL_MODE=insecure' "$env_dir/.env" \
   && ! grep -q '^SERVERKIT_SSL_MODE=secure' "$env_dir/.env"; then
    ok "the converted .env records ssl-mode=insecure so HSTS stops being sent"
else
    bad "SERVERKIT_SSL_MODE was not flipped to insecure: [$(grep SERVERKIT_SSL_MODE "$env_dir/.env" | tr '\n' ' ')]"
fi

# -- an ordinary re-run must not strip proxy settings someone configured by hand.
( set -Eeuo pipefail
  source "$INSTALL_SH" >/dev/null 2>&1
  INSTALL_DIR="$env_dir"; SSL_MODE=insecure; PROFILE=standard
  SERVERKIT_EXTERNAL_PROXY=0
  write_config >/dev/null 2>&1 )
if grep -q '^TRUST_PROXY_HEADERS=true' "$env_dir/.env"; then
    ok "a plain re-run leaves existing proxy settings alone"
else
    bad "a plain re-run removed TRUST_PROXY_HEADERS from an existing .env"
fi

# -- only ONE default server may exist on :80.
#
#    Both site configs declare `listen 80 default_server` and then include
#    /etc/nginx/serverkit-conf.d/*.conf. install.sh writes the canonical-domain
#    redirect into that directory whenever PANEL_DOMAIN is set, and it used to
#    declare `listen 80 default_server` as well — which nginx rejects outright
#    ("a duplicate default server for 0.0.0.0:80"), in BOTH ssl modes. Since
#    launch_services starts nginx after write_config, every first install with a
#    domain produced an nginx that would not start. Verified against real nginx.
#    Directives only — the block's own comments explain this trap by naming it.
canon_block="$(awk '/canonical-domain.conf <<EOF/,/^EOF$/' "$INSTALL_SH" \
                | grep -v '^[[:space:]]*#')"
if [ -z "$canon_block" ]; then
    bad "could not find the canonical-domain.conf heredoc in install.sh"
elif printf '%s' "$canon_block" | grep -q 'default_server'; then
    bad "the canonical-domain redirect declares default_server — nginx will refuse to start on any PANEL_DOMAIN install"
elif ! printf '%s' "$canon_block" | grep -q 'server_name ~'; then
    bad "the canonical-domain redirect no longer matches a bare IP by regex server_name — the redirect will never fire"
else
    ok "the canonical-domain redirect matches a bare IP without a second default_server"
fi

# -- and the shipped sites must remain the ONLY default server on :80.
for site in serverkit.conf serverkit-insecure.conf; do
    n="$(grep -c 'listen 80 default_server' "$REPO_DIR/nginx/sites-available/$site" || true)"
    if [ "$n" = "1" ]; then
        ok "$site declares exactly one default server on :80"
    else
        bad "$site declares $n default servers on :80 (expected 1)"
    fi
done

# --------------------------------------------------------------------------
# T44 — every install must derive the REAL client IP.
#
# install.sh wrote no TRUST_PROXY_HEADERS line at all, so the config default
# (False) applied while the panel always sits behind its own nginx: per-IP rate
# limits, the login brute-force throttle and every audit-log entry keyed on
# 127.0.0.1. .env.example claimed `true`, so the two disagreed.
#
# Trusting X-Forwarded-For is only safe because nothing can reach gunicorn
# without passing through that nginx — render_service_unit binds loopback and
# the firewall never opens the backend port. Expose the port and the header
# becomes attacker-chosen, so the switch must follow the bind host.
# --------------------------------------------------------------------------
printf '\nT44 — trusted client IP on a stock install\n'
t44="$WORK/t44"; mkdir -p "$t44"

# write_config mints secrets with `openssl rand -hex 32` and a python3 one-liner.
# The distro containers this suite runs in ship NEITHER — ubuntu:24.04 and
# debian:12 have no openssl and no python3 — and under `set -e` the failed
# command substitution aborted write_config before it wrote a single line, so
# three assertions read an empty file and failed in CI while passing on a dev
# box that happens to have both. Stub them: this test is about which proxy keys
# land in .env, not about how random the secrets are. (Same philosophy as the
# rest of the suite, which stubs every external tool.)
#
# Shell functions win over PATH lookup, including for `"${PYTHON_BIN:-python3}"`,
# so no PATH juggling is needed. Defined by calling this inside each subshell.
# Literal values, no seq/head/tr: those are exactly the sort of tool a stripped
# base image turns out not to have, which is the bug being fixed here.
_t44_stub_keygen() {
    openssl() { printf '%s\n' "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"; }
    python3() { printf '%s\n' "ZmFrZS1mZXJuZXQta2V5LWZvci11bml0LXRlc3RzLTAwMDA9"; }
}

# -- the stock install: loopback-bound gunicorn behind our own nginx.
out="$( set -Eeuo pipefail
        unset SERVERKIT_BIND_HOST SERVERKIT_EXTERNAL_PROXY || true
        source "$INSTALL_SH" >/dev/null 2>&1
        _t44_stub_keygen
        rm -rf "$t44/a"; mkdir -p "$t44/a"
        INSTALL_DIR="$t44/a"; SSL_MODE=insecure; PROFILE=standard
        write_config >/dev/null 2>&1
        cat "$t44/a/.env" )"
if printf '%s' "$out" | grep -q '^TRUST_PROXY_HEADERS=true' \
   && printf '%s' "$out" | grep -q '^TRUSTED_PROXY_HOPS=1'; then
    ok "a stock install trusts its own nginx and derives the real client IP (1 hop)"
else
    bad "stock install produced: [$(printf '%s' "$out" | grep -i proxy | tr '\n' ' ')]"
fi

# -- exposing the raw backend port must turn it OFF: X-Forwarded-For is then
#    client-controlled, and a forged IP defeats the very throttles it feeds.
out="$( set -Eeuo pipefail
        unset SERVERKIT_EXTERNAL_PROXY || true
        SERVERKIT_BIND_HOST=0.0.0.0
        source "$INSTALL_SH" >/dev/null 2>&1
        _t44_stub_keygen
        rm -rf "$t44/b"; mkdir -p "$t44/b"
        INSTALL_DIR="$t44/b"; SSL_MODE=insecure; PROFILE=standard
        write_config >/dev/null 2>&1
        cat "$t44/b/.env" )"
if printf '%s' "$out" | grep -q '^TRUST_PROXY_HEADERS=false' \
   && ! printf '%s' "$out" | grep -q '^TRUST_PROXY_HEADERS=true'; then
    ok "SERVERKIT_BIND_HOST=0.0.0.0 leaves trust OFF (the header would be forgeable)"
else
    bad "an exposed backend port still trusted X-Forwarded-For: [$(printf '%s' "$out" | grep -i proxy | tr '\n' ' ')]"
fi

# -- the external-proxy switch still wins, with its own hop count.
out="$( set -Eeuo pipefail
        SERVERKIT_EXTERNAL_PROXY=1
        source "$INSTALL_SH" >/dev/null 2>&1
        _t44_stub_keygen
        rm -rf "$t44/c"; mkdir -p "$t44/c"
        INSTALL_DIR="$t44/c"; SSL_MODE=insecure; PROFILE=standard
        write_config >/dev/null 2>&1
        cat "$t44/c/.env" )"
if printf '%s' "$out" | grep -q '^TRUSTED_PROXY_HOPS=2'; then
    ok "SERVERKIT_EXTERNAL_PROXY still overrides the default with 2 hops"
else
    bad "external proxy did not set 2 hops: [$(printf '%s' "$out" | grep -i proxy | tr '\n' ' ')]"
fi

# -- backfill on a re-run: pre-existing installs are the ones actually suffering.
mkdir -p "$t44/old"
printf 'SECRET_KEY=keep-me\nSERVERKIT_SSL_MODE=insecure\n' > "$t44/old/.env"
( set -Eeuo pipefail
  unset SERVERKIT_BIND_HOST SERVERKIT_EXTERNAL_PROXY || true
  source "$INSTALL_SH" >/dev/null 2>&1
  INSTALL_DIR="$t44/old"; SSL_MODE=insecure; PROFILE=standard
  write_config >/dev/null 2>&1 )
if grep -q '^TRUST_PROXY_HEADERS=true' "$t44/old/.env" \
   && grep -q '^TRUSTED_PROXY_HOPS=1' "$t44/old/.env" \
   && grep -q '^SECRET_KEY=keep-me' "$t44/old/.env"; then
    ok "a re-run backfills the trust setting into an install that predates it"
else
    bad "re-run backfill produced: [$(tr '\n' ' ' < "$t44/old/.env")]"
fi

# -- but an explicit operator choice is never overridden. Someone who turned
#    trust off did it for a reason, and a re-run flipping it back on silently
#    would be the worst possible surprise.
mkdir -p "$t44/explicit"
printf 'SECRET_KEY=keep-me\nTRUST_PROXY_HEADERS=false\n' > "$t44/explicit/.env"
( set -Eeuo pipefail
  unset SERVERKIT_BIND_HOST SERVERKIT_EXTERNAL_PROXY || true
  source "$INSTALL_SH" >/dev/null 2>&1
  INSTALL_DIR="$t44/explicit"; SSL_MODE=insecure; PROFILE=standard
  write_config >/dev/null 2>&1 )
if grep -q '^TRUST_PROXY_HEADERS=false' "$t44/explicit/.env" \
   && ! grep -q '^TRUST_PROXY_HEADERS=true' "$t44/explicit/.env"; then
    ok "a re-run never overrides an explicit TRUST_PROXY_HEADERS=false"
else
    bad "a re-run overrode the operator's explicit setting: [$(tr '\n' ' ' < "$t44/explicit/.env")]"
fi

# -- the two consumers of the bind host must agree. The systemd unit and the
#    trust decision reading different values is exactly how this class of bug
#    comes back.
unit="$t44/unit.service"
( set -Eeuo pipefail
  unset SERVERKIT_BIND_HOST || true
  source "$INSTALL_SH" >/dev/null 2>&1
  INSTALL_DIR="$REPO_DIR"; VENV_DIR="$REPO_DIR/venv"; LOG_DIR="$t44"
  render_service_unit "$unit" >/dev/null 2>&1 ) || true
if grep -q -- '-b 127.0.0.1:5000' "$unit" 2>/dev/null; then
    ok "render_service_unit and the trust decision share one bind host (loopback)"
else
    bad "the rendered unit does not bind loopback: [$(grep ExecStart "$unit" 2>/dev/null)]"
fi

# -- apt must never be able to open an interactive prompt.
#
#    `apt-get install -y` answers apt's questions but NOT debconf's: tzdata and
#    friends still open a dialog and block. pkg_add captures output, so the
#    installer then hangs in complete silence — a real 60-minute stall on
#    ubuntu:22.04, found only once provisioning was exercised for real. Under
#    `curl | bash` it is worse: stdin is the script, so debconf can consume
#    installer source as its answers.
apt_line="$(awk '/^pkg_add\(\)/,/^}/' "$INSTALL_SH" | grep -A3 'apt)')"
if printf '%s' "$apt_line" | grep -q 'DEBIAN_FRONTEND=noninteractive'; then
    ok "pkg_add runs apt with DEBIAN_FRONTEND=noninteractive"
else
    bad "pkg_add's apt branch can block on a debconf prompt: [$apt_line]"
fi

# Issue #148: reusing an interpreter must still provision matching headers.
if ( set -Eeuo pipefail
    OS_FAMILY=debian; ID=ubuntu
    locate_python() { PYTHON_BIN=python3.14; return 0; }
    python3.14() { printf '3.14\n'; }
    ready=0
    python_build_ready() { [ "$ready" = 1 ]; }
    refresh_pkg_index() { :; }
    pkg_add() {
        [ "$*" = 'build-essential python3.14-dev' ] || exit 1
        ready=1
    }
    provision_python >/dev/null
    [ "$ready" = 1 ]
); then
    ok "existing Python 3.14 gets matching headers and build tools"
else
    bad "existing Python skips native build prerequisites"
fi

if ( set -Eeuo pipefail
    OS_FAMILY=debian; PYTHON_BIN=python3.14
    python3.14() { printf '3.14\n'; }
    python_build_ready() { return 1; }
    refresh_pkg_index() { :; }
    pkg_add() { return 0; } # warn-and-continue package failure
    halt() { return 1; }
    ensure_python_build_deps >/dev/null 2>&1
); then
    bad "missing headers were accepted after package installation failed"
else
    ok "missing build prerequisites fail before pip"
fi

if ( set -Eeuo pipefail
    OS_FAMILY=debian; ID=ubuntu; installed=0; checked=0
    locate_python() { [ "$installed" = 1 ] && { PYTHON_BIN=python3.14; return 0; }; return 1; }
    refresh_pkg_index() { :; }
    pkg_add() {
        [ "$*" = 'python3 python3-venv python3-dev' ] || exit 1
        installed=1
    }
    ensure_python_build_deps() { checked=1; }
    add-apt-repository() { exit 1; }
    provision_python >/dev/null
    [ "$installed" = 1 ] && [ "$checked" = 1 ]
); then
    ok "Ubuntu uses its supported default Python without a PPA"
else
    bad "Ubuntu did not use its default Python first"
fi

if ( set -Eeuo pipefail
    python_build_ready() { return 0; }
    pkg_add() { exit 1; }
    refresh_pkg_index() { exit 1; }
    ensure_python_build_deps
); then
    ok "working native build environment needs no package changes"
else
    bad "working native build environment tried to install packages"
fi

# -- static guard: the documented insecure site must keep shipping, since both
#    the docs and SERVERKIT_EXTERNAL_PROXY point operators straight at it.
if [ -f "$REPO_DIR/nginx/sites-available/serverkit-insecure.conf" ] \
   && ! grep -qE 'return[[:space:]]+30[12]' "$REPO_DIR/nginx/sites-available/serverkit-insecure.conf"; then
    ok "serverkit-insecure.conf ships and contains no redirect for a proxy to loop on"
else
    bad "serverkit-insecure.conf is missing or now redirects — issue #96 would recur"
fi

# --------------------------------------------------------------------------
printf '\n%d passed, %d failed, %d skipped\n\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
