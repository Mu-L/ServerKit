#!/usr/bin/env bash
#
# Does install.sh actually obtain a working Python ON THIS DISTRO?
#
# Every other suite here runs against stubs, which is the right trade for logic
# but means the one thing that talks to the distro's real package repos was
# never exercised. Issue #99: Debian 13 ships only python3.13, the version gate
# accepted 3.11-3.12, so both `apt-get install python3.11` and `python3.12`
# failed and the installer fell through to a source build that cannot work
# (nothing in install.sh ever installs a compiler). Seven distro images in CI
# and none of them caught it, because none of them ran this code for real.
#
# So: run identify_system + choose_pkg_manager + provision_python and the
# local-build prerequisites against real repos, then prove the result is
# usable. No systemd, no nginx, no network
# services — this fits in the plain distro containers the matrix already pulls.
#
# Destructive: installs packages. Intended for a container or a throwaway VM.
# Refuses to run on a box that looks like somebody's machine unless forced.
#
# Run:  bash scripts/test/test_provision_python.sh
#       SERVERKIT_ALLOW_DESTRUCTIVE=1 bash scripts/test/test_provision_python.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALL_SH="$REPO_DIR/install.sh"

PASS=0
FAIL=0
SKIP=0
ok()   { PASS=$((PASS + 1)); printf '  \033[32m✔\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  \033[31m✘\033[0m %s\n' "$1"; }
skip() { SKIP=$((SKIP + 1)); printf '  \033[33m∼\033[0m %s (skipped)\n' "$1"; }

# --------------------------------------------------------------------------
# Guard rails. This one really does apt/dnf install.
# --------------------------------------------------------------------------
if [ "${SERVERKIT_ALLOW_DESTRUCTIVE:-0}" != "1" ] \
   && [ ! -f /.dockerenv ] && [ -z "${CI:-}" ]; then
    printf 'Refusing to run: this installs packages system-wide.\n' >&2
    printf 'Run it in a container, or set SERVERKIT_ALLOW_DESTRUCTIVE=1.\n' >&2
    exit 2
fi
if [ "$(id -u)" != "0" ]; then
    printf 'Refusing to run: package installation needs root.\n' >&2
    exit 2
fi

printf '\nprovision_python against real package repos\n'

# shellcheck source=/dev/null
source "$INSTALL_SH" >/dev/null 2>&1
# install.sh runs `set -euo pipefail` at the top, and sourcing applies that to
# THIS shell. Under -e the first probe that returns non-zero (which is the
# normal way half of these functions report "not found") would kill the run
# before printing a single assertion. Put it back the way a test harness needs.
set +e

# The installer's own detection, not a guess — provision_python branches on
# OS_FAMILY and ID, and choose_pkg_manager is what makes pkg_add do anything.
identify_system  >/dev/null 2>&1 || true
choose_pkg_manager >/dev/null 2>&1 || true

# main()'s order is identify_system -> choose_pkg_manager -> ensure_bootstrap_tools
# -> ... -> provision_python, and this MUST match it.
#
# ensure_bootstrap_tools is what refreshes the package index (its own comment
# says minimal images "ship with empty package indexes"). Omitting it made this
# test fail on ubuntu:22.04 for a reason the real installer does not have: a
# fresh container has empty apt lists, so the very first `apt-get install` finds
# no candidate for anything — including software-properties-common, which
# killed the deadsnakes fallback and dumped provision_python into the source
# build. The installer was never broken there; the test was skipping a step.
#
# The lesson generalises: a test that calls production functions out of order
# invents failures, and "fixing" install.sh to satisfy it would have made the
# real thing worse.
ensure_bootstrap_tools >/dev/null 2>&1 || true

printf '  distro: %s (ID=%s, family=%s, pkg=%s)\n' \
    "${PRETTY_NAME:-unknown}" "${ID:-?}" "${OS_FAMILY:-?}" "${PKG_MGR:-?}"
printf '  gate:   Python %s-%s\n' "$PYTHON_MIN" "$PYTHON_MAX"

if [ "${PKG_MGR:-}" = "" ]; then
    skip "no package manager detected — nothing to provision with"
    printf '\n%d passed, %d failed, %d skipped\n\n' "$PASS" "$FAIL" "$SKIP"
    [ "$FAIL" -eq 0 ]
    exit $?
fi

# --------------------------------------------------------------------------
# 1. A supported interpreter must be obtainable from the distro's own repos.
#
# The source-build fallback is deliberately NOT accepted as a pass here. It
# takes 20+ minutes with --enable-optimizations, needs a toolchain the
# installer never installs, and swallows its own errors through `| tail -1`.
# A distro that reaches it is a distro we have not really added support for.
# --------------------------------------------------------------------------
before_build_from_source=0
build_python_from_source() {
    before_build_from_source=1
    return 1
}

# provision_python's last line is `... || halt "Could not install a supported
# Python"`, and halt exits. Unstubbed it would take this harness down with it
# before a single assertion printed, turning a real failure into silence.
# Returning 1 instead lets provision_python report failure normally.
halted=""
halt() {
    halted="$*"
    return 1
}

if { provision_python && ensure_python_build_deps; } >/tmp/pp.log 2>&1; then
    prov_rc=0
else
    prov_rc=$?
fi
sed 's/^/    | /' /tmp/pp.log | tail -25

if [ "$before_build_from_source" = "1" ]; then
    bad "no packaged Python in $PYTHON_MIN-$PYTHON_MAX on ${ID:-this distro} — the installer resorts to compiling one"
    [ -n "$halted" ] && printf '    installer would abort with: %s\n' "$halted"
elif [ "$prov_rc" != "0" ]; then
    bad "provision_python failed (exit $prov_rc)${halted:+ — $halted}"
else
    ok "provision_python obtained a supported Python from the distro's packages"
fi

# --------------------------------------------------------------------------
# 2. Whatever it picked has to actually be usable.
#    A python that cannot build a venv or run pip fails the install later, at a
#    much more confusing moment.
# --------------------------------------------------------------------------
if [ -z "${PYTHON_BIN:-}" ] || ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    bad "PYTHON_BIN is unset or not on PATH after provision_python"
else
    pyver="$("$PYTHON_BIN" -c 'import sys;print(".".join(map(str,sys.version_info[:2])))' 2>/dev/null || echo '?')"
    if ver_in_range "$pyver"; then
        ok "$PYTHON_BIN reports $pyver, inside the supported range"
    else
        bad "$PYTHON_BIN reports $pyver, outside $PYTHON_MIN-$PYTHON_MAX"
    fi

    if "$PYTHON_BIN" -m venv /tmp/pp-venv >/dev/null 2>&1 \
       && [ -x /tmp/pp-venv/bin/python ]; then
        ok "it can create a virtualenv"
        if /tmp/pp-venv/bin/python -m pip --version >/dev/null 2>&1; then
            ok "the virtualenv has a working pip"
        else
            bad "the virtualenv has no working pip (missing ensurepip?)"
        fi
        # ssl and sqlite3 are the two stdlib modules a hand-built or
        # stripped-down Python most often lacks, and both fail far away from
        # here: no ssl means every HTTPS fetch dies, no sqlite3 means the
        # database never opens.
        for mod in ssl sqlite3 ctypes; do
            if /tmp/pp-venv/bin/python -c "import $mod" >/dev/null 2>&1; then
                ok "stdlib $mod is present"
            else
                bad "stdlib $mod is MISSING — the panel will fail later, not here"
            fi
        done
    else
        bad "it cannot create a virtualenv"
    fi
    # Force a source build: a cached/platform wheel would hide missing Python.h
    # and compilers, the exact gap behind issue #148. Use the production pin.
    native_requirement=$(grep '^gevent==' "$REPO_DIR/backend/requirements.txt")
    if /tmp/pp-venv/bin/python -m pip install --no-cache-dir --no-binary=gevent \
        "$native_requirement" >/tmp/pp-native.log 2>&1 \
        && /tmp/pp-venv/bin/python -c 'import gevent' >/dev/null 2>&1; then
        ok "the pinned gevent dependency builds from source and imports"
    else
        bad "native dependency build failed"
        tail -40 /tmp/pp-native.log
    fi
    # Ubuntu 26.04 is the regression target; validate all backend pins there.
    if [ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 26.04 ]; then
        if /tmp/pp-venv/bin/python -m pip install \
            -r "$REPO_DIR/backend/requirements.txt" >/tmp/pp-requirements.log 2>&1 \
            && /tmp/pp-venv/bin/python -m pip check >>/tmp/pp-requirements.log 2>&1; then
            ok "all backend requirements install with consistent dependencies"
        else
            bad "backend requirements failed"
            tail -40 /tmp/pp-requirements.log
        fi
    fi
    rm -rf /tmp/pp-venv
fi

printf '\n%d passed, %d failed, %d skipped\n\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
