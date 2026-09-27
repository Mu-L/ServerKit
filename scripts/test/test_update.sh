#!/usr/bin/env bash
#
# Unit tests for scripts/update.sh — runs in seconds, no server, no deploy.
#
# update.sh is source-able: when sourced it defines every function and then
# returns *before* the run block (the BASH_SOURCE guard). That lets us exercise
# the config-refresh + deployment-detection logic against throwaway fixtures
# instead of a real /etc and a real cloud box — which is what made this script
# so painful to get right.
#
# Each unit-under-test runs in a subshell that re-enables `set -Eeuo pipefail`,
# so a regression of the kind that bit 1.7.0 (an unguarded command silently
# aborting under set -e) is caught here as a failed assertion.
#
# Run:  bash scripts/test/test_update.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPDATE_SH="$SCRIPT_DIR/../update.sh"

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
# Stub the external commands the functions may shell out to, so the tests
# never touch the host's nginx/systemd/docker.
# --------------------------------------------------------------------------
STUB_BIN="$WORK/bin"
mkdir -p "$STUB_BIN"
make_stub_ok "$STUB_BIN" systemctl nginx npm curl
# docker stub: `docker ps ...` lists the fixture container names; anything else
# (image inspect/tag/compose/...) is a harmless no-op.
cat > "$STUB_BIN/docker" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  ps) for n in ${SERVERKIT_TEST_CONTAINERS:-}; do printf '%s\n' "$n"; done ;;
  *)  exit 0 ;;
esac
EOF
chmod +x "$STUB_BIN"/*
export PATH="$STUB_BIN:$PATH"

# --------------------------------------------------------------------------
# Source update.sh (functions only). Keep logging off and point the install
# dir at the sandbox so the derived DIR_A/DIR_B land under $WORK.
# --------------------------------------------------------------------------
export SERVERKIT_NO_LOG=1
export SERVERKIT_DIR="$WORK/opt/serverkit"
# shellcheck disable=SC1090
source "$UPDATE_SH"
set +e +u   # hand control back to the harness; tests re-arm set -e per subshell

printf '\nupdate.sh unit tests\n\n'

# --------------------------------------------------------------------------
# T1 — the headline regression: refresh_config must NOT die when the live
# nginx has no serverkit.conf (HTTP-only boxes). This is the exact 1.7.0
# silent-death that left the updater stuck reporting the old version.
# --------------------------------------------------------------------------
t="$WORK/t1"
mkdir -p "$t/nginx/sites-available" "$t/nginx/sites-enabled" "$t/target/nginx/sites-available"
printf 'http {\n}\n' > "$t/nginx/nginx.conf"
printf 'server { listen 80; }\n' > "$t/target/nginx/sites-available/serverkit-insecure.conf"
if (
    set -Eeuo pipefail
    NGINX_DIR="$t/nginx"; LETSENCRYPT_DIR="$t/le"; SYSTEMD_DIR="$t/sysd"; CONFIG_DIR="$t/cfg"; DRY_RUN=0
    refresh_config "$t/target"
) >/dev/null 2>&1; then
    ok "refresh_config survives a missing serverkit.conf (the 1.7.0 silent-death bug)"
else
    bad "refresh_config DIED on a missing serverkit.conf — the set -e/pipefail regression is back"
fi

# --------------------------------------------------------------------------
# T2 — refresh_config still works when a serverkit.conf with a real cert path
# is present (the grep finds a match).
# --------------------------------------------------------------------------
t="$WORK/t2"
mkdir -p "$t/nginx/sites-available" "$t/nginx/sites-enabled" "$t/target/nginx/sites-available" "$t/le/live/example.com"
printf 'http {\n}\n' > "$t/nginx/nginx.conf"
printf 'ssl_certificate %s/live/example.com/fullchain.pem;\n' "$t/le" > "$t/nginx/sites-available/serverkit.conf"
printf 'server { listen 80; }\n' > "$t/target/nginx/sites-available/serverkit-insecure.conf"
if (
    set -Eeuo pipefail
    NGINX_DIR="$t/nginx"; LETSENCRYPT_DIR="$t/le"; SYSTEMD_DIR="$t/sysd"; CONFIG_DIR="$t/cfg"; DRY_RUN=0
    refresh_config "$t/target"
) >/dev/null 2>&1; then
    ok "refresh_config handles a present serverkit.conf with a cert path"
else
    bad "refresh_config failed with a present serverkit.conf"
fi

# --------------------------------------------------------------------------
# T3 — deployment-shape detection (the bug that made 1.7.0 take the wrong path
# on an all-Docker box).
# --------------------------------------------------------------------------
t="$WORK/t3"; mkdir -p "$t/install"
touch "$t/install/docker-compose.yml"
if (
    set -Eeuo pipefail
    INSTALL_DIR="$t/install"
    export SERVERKIT_TEST_CONTAINERS="serverkit-backend serverkit-frontend"
    is_docker_deployment
); then
    ok "is_docker_deployment → docker when compose + container and no host venv"
else
    bad "is_docker_deployment should pick the docker path for an all-Docker box"
fi

mkdir -p "$t/install/venv/bin"
printf '#!/bin/sh\n' > "$t/install/venv/bin/python"; chmod +x "$t/install/venv/bin/python"
if (
    set -Eeuo pipefail
    INSTALL_DIR="$t/install"
    export SERVERKIT_TEST_CONTAINERS="serverkit-backend"
    is_docker_deployment
); then
    bad "is_docker_deployment should fall back to hybrid when a host venv exists"
else
    ok "is_docker_deployment → hybrid when a host venv exists (precedence)"
fi

# --------------------------------------------------------------------------
# T4 — blue/green slot resolution.
# --------------------------------------------------------------------------
t="$WORK/t4"; mkdir -p "$t/serverkit-a" "$t/serverkit-b"
ln -sfn "$t/serverkit-a" "$t/serverkit" 2>/dev/null || true
if [ ! -L "$t/serverkit" ]; then
    skip "active/next slot flip — symlinks unsupported here (works on Linux CI)"
else
    exp="$(readlink -f "$t/serverkit-a")|$t/serverkit-b"
    if ! res="$(
        set -Eeuo pipefail
        INSTALL_DIR="$t/serverkit"; DIR_A="$t/serverkit-a"; DIR_B="$t/serverkit-b"
        printf '%s|%s' "$(active_real_dir)" "$(next_real_dir)"
    )"; then
        bad "active/next slot resolution aborted under set -Eeuo pipefail"
    elif [ "$res" = "$exp" ]; then
        ok "active/next slot flip (A active → B is next)"
    else
        bad "active/next slot wrong: got [$res] expected [$exp]"
    fi
fi

# --------------------------------------------------------------------------
# T5 — the loud-failure reporter actually emits a labelled diagnostic.
# --------------------------------------------------------------------------
# UPDATE_LOG pinned non-empty so the "full log:" line is part of what T5
# asserts; the empty-UPDATE_LOG return-0 contract is covered by the fresh-box
# loop below (report_failure is in UPDATE_OBSERVERS).
if out="$(LAST_PHASE='Refreshing Configuration' UPDATE_LOG='/dev/null' \
          report_failure 2 42 'grep ... serverkit.conf' 2>&1)" \
   && printf '%s' "$out" | grep -q 'Update aborted'; then
    ok "report_failure emits a labelled 'Update aborted' diagnostic"
else
    bad "report_failure produced no diagnostic (or returned non-zero)"
fi

# --------------------------------------------------------------------------
# T6 — self-update bootstrap skips cleanly under each opt-out, and never
# re-execs (would replace this test process) when there is nothing to do.
# --------------------------------------------------------------------------
self_update_skips() {
    # Each guard runs in a subshell with set -e; a clean return keeps the test
    # process alive, and any stray `exec` would visibly break the harness.
    ( set -Eeuo pipefail; SERVERKIT_UPDATER_REEXECED=1; DRY_RUN=0; maybe_reexec_latest_updater ) &&
    ( set -Eeuo pipefail; SERVERKIT_NO_SELF_UPDATE=1;  DRY_RUN=0; maybe_reexec_latest_updater ) &&
    ( set -Eeuo pipefail; DRY_RUN=1;                              maybe_reexec_latest_updater ) &&
    ( set -Eeuo pipefail; DRY_RUN=0; SERVERKIT_OFFLINE_TARBALL=/x; maybe_reexec_latest_updater )
}
if self_update_skips >/dev/null 2>&1; then
    ok "self-update no-ops under re-exec/opt-out/dry-run/offline guards"
else
    bad "self-update guard returned non-zero (would block or loop the updater)"
fi

# --------------------------------------------------------------------------
# T7 — the run lock refuses a second concurrent update.
# --------------------------------------------------------------------------
if command -v flock >/dev/null 2>&1; then
    lock="$WORK/update.lock"
    ( flock -n 9 || exit 1; sleep 3 ) 9>"$lock" &   # hold the lock
    held=$!
    sleep 0.3
    if ( set -Eeuo pipefail; LOCK_FILE="$lock"; DRY_RUN=0; acquire_update_lock ) >/dev/null 2>&1; then
        bad "acquire_update_lock should refuse while the lock is held"
    else
        ok "acquire_update_lock refuses a concurrent run while locked"
    fi
    kill "$held" 2>/dev/null || true; wait "$held" 2>/dev/null || true
    if ( set -Eeuo pipefail; LOCK_FILE="$WORK/free.lock"; DRY_RUN=0; acquire_update_lock ) >/dev/null 2>&1; then
        ok "acquire_update_lock succeeds when the lock is free"
    else
        bad "acquire_update_lock failed on a free lock"
    fi
else
    skip "run-lock test — flock unavailable here (runs on Linux CI)"
fi

# --------------------------------------------------------------------------
# T8 — version comparison: versions_equal ignores a leading "v".
# --------------------------------------------------------------------------
if ( set -Eeuo pipefail; versions_equal v1.7.1 1.7.1 ) && \
   ( set -Eeuo pipefail; versions_equal 1.7.1 1.7.1 ) && \
   ! ( set -Eeuo pipefail; versions_equal 1.7.0 1.7.1 ); then
    ok "versions_equal matches across a leading 'v' and rejects mismatches"
else
    bad "versions_equal comparison is wrong"
fi

# --------------------------------------------------------------------------
# T9 — is_already_current short-circuits to "proceed" (non-zero) under --force
# and offline, without any network/git access.
# --------------------------------------------------------------------------
if ( set -Eeuo pipefail; FORCE_UPDATE=1; is_already_current ); then
    bad "is_already_current must proceed (non-zero) under --force"
else
    ok "is_already_current proceeds under --force (skips the version check)"
fi
if ( set -Eeuo pipefail; FORCE_UPDATE=0; SERVERKIT_OFFLINE_TARBALL=/x; is_already_current ); then
    bad "is_already_current must proceed (non-zero) when offline"
else
    ok "is_already_current proceeds when offline (can't compare)"
fi

# --------------------------------------------------------------------------
# T10 — the rollback-safety fix: migrate_database must run the migration
# against the NEW slot's database copy (slot-absolute path), never the
# /opt/serverkit symlink that still resolves to the live old slot. A flask
# stub captures the DATABASE_URL the migration actually used.
# --------------------------------------------------------------------------
t="$WORK/t10/serverkit-b"
mkdir -p "$t/venv/bin" "$t/backend/instance"
: > "$t/venv/bin/activate"                              # sourceable no-op
: > "$t/backend/instance/serverkit.db"                  # the slot's DB copy
printf '#!/usr/bin/env bash\necho ok\n' > "$t/venv/bin/python"  # integrity probe: healthy DB
chmod +x "$t/venv/bin/python"
printf 'DATABASE_URL=sqlite:///opt/serverkit/backend/instance/serverkit.db\n' > "$t/.env"
FLASK_CAP="$WORK/t10/flask-saw-dburl"
cat > "$STUB_BIN/flask" <<EOF
#!/usr/bin/env bash
printf '%s' "\${DATABASE_URL:-NONE}" > "$FLASK_CAP"
exit 0
EOF
chmod +x "$STUB_BIN/flask"
mig_rc=0
(
    set -Eeuo pipefail
    DRY_RUN=0
    migrate_database "$t"
) >/dev/null 2>&1 || mig_rc=$?
saw="$(tr -d '\r' < "$FLASK_CAP" 2>/dev/null || true)"
if [ "$mig_rc" -eq 0 ] && [ "$saw" = "sqlite:///$t/backend/instance/serverkit.db" ]; then
    ok "migrate_database targets the new slot's DB, leaving the old slot untouched"
else
    bad "migrate_database rc=$mig_rc, used [$saw], expected rc 0 + the slot-absolute new-slot DB path"
fi
rm -f "$STUB_BIN/flask"

# --------------------------------------------------------------------------
# T10b — the corrupt-DB fail-fast: when the integrity probe reports real
# corruption, migrate_database must halt BEFORE flask runs, with the update
# still on the old slot. A python stub simulates PRAGMA integrity_check
# returning 'database disk image is malformed'.
# --------------------------------------------------------------------------
t="$WORK/t10b/serverkit-b"
mkdir -p "$t/venv/bin" "$t/backend/instance"
: > "$t/venv/bin/activate"
: > "$t/backend/instance/serverkit.db"
printf 'DATABASE_URL=sqlite:///opt/serverkit/backend/instance/serverkit.db\n' > "$t/.env"
printf '#!/usr/bin/env bash\necho "database disk image is malformed"\n' > "$t/venv/bin/python"
chmod +x "$t/venv/bin/python"
FLASK_CAP="$WORK/t10b/flask-ran"
cat > "$STUB_BIN/flask" <<EOF
#!/usr/bin/env bash
touch "$FLASK_CAP"
exit 0
EOF
chmod +x "$STUB_BIN/flask"
mig_rc=0
(
    set -Eeuo pipefail
    DRY_RUN=0
    migrate_database "$t"
) >/dev/null 2>&1 || mig_rc=$?
if [ "$mig_rc" -ne 0 ] && [ ! -e "$FLASK_CAP" ]; then
    ok "migrate_database halts on a corrupt SQLite DB before flask runs (fail-fast)"
else
    bad "migrate_database corrupt-DB: rc=$mig_rc (want non-zero), flask-ran=$([ -e "$FLASK_CAP" ] && echo yes || echo no) (want no)"
fi
rm -f "$STUB_BIN/flask"

# --------------------------------------------------------------------------
# T10c — a probe that can't run at all (no usable venv python) is NOT proof
# of corruption: warn and proceed so flask can surface any real problem.
# --------------------------------------------------------------------------
t="$WORK/t10c/serverkit-b"
mkdir -p "$t/venv/bin" "$t/backend/instance"
: > "$t/venv/bin/activate"
: > "$t/backend/instance/serverkit.db"
printf 'DATABASE_URL=sqlite:///opt/serverkit/backend/instance/serverkit.db\n' > "$t/.env"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB_BIN/flask"
chmod +x "$STUB_BIN/flask"
mig_rc=0
(
    set -Eeuo pipefail
    DRY_RUN=0
    migrate_database "$t"
) >/dev/null 2>&1 || mig_rc=$?
if [ "$mig_rc" -eq 0 ]; then
    ok "migrate_database proceeds (with a warning) when the integrity probe can't run"
else
    bad "migrate_database probe-unavailable: rc=$mig_rc, expected rc 0 (warn + proceed)"
fi
rm -f "$STUB_BIN/flask"

# --------------------------------------------------------------------------
# T10d — copy_sqlite_db snapshots a LIVE database consistently via the SQLite
# online backup API. A plain cp of a hot DB can capture half a transaction
# and come out "database disk image is malformed" — the exact failure that
# sank the migration on a real 1.7.35 -> 1.7.65 update.
# --------------------------------------------------------------------------
t="$WORK/t10d"; mkdir -p "$t"
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sqlite3' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    skip "copy_sqlite_db snapshots a live DB consistently (no working python3 on this host)"
else
    "$PY" - "$t/live.db" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)')
c.executemany('INSERT INTO t(v) VALUES (?)', [('row%d' % i,) for i in range(1000)])
c.commit(); c.close()
PYEOF
    snap_rc=0
    (
        set -Eeuo pipefail
        DRY_RUN=0
        copy_sqlite_db "$t/live.db" "$t/snap.db"
    ) >/dev/null 2>&1 || snap_rc=$?
    snap_check="$("$PY" - "$t/snap.db" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute('PRAGMA integrity_check').fetchone()[0],
      c.execute('SELECT COUNT(*) FROM t').fetchone()[0])
PYEOF
    )"
    if [ "$snap_rc" -eq 0 ] && [ "$snap_check" = "ok 1000" ]; then
        ok "copy_sqlite_db snapshots a live DB consistently (integrity ok, all rows)"
    else
        bad "copy_sqlite_db: rc=$snap_rc, snapshot check=[$snap_check], expected 'ok 1000'"
    fi
fi

# Contract: no racy plain-cp of the LIVE database anywhere in update.sh —
# the pre-upgrade backup and both deploy paths must go through copy_sqlite_db.
if ! grep -qF 'cp "$db_file" "$backup_file"' "$UPDATE_SH" \
   && ! grep -qF 'cp "$INSTALL_DIR/backend/instance/serverkit.db"' "$UPDATE_SH"; then
    ok "live SQLite DB is never plain-cp'd (consistent snapshot only)"
else
    bad "a racy plain-cp of the live serverkit.db is still in update.sh"
fi

# --------------------------------------------------------------------------
# T10e — PostgreSQL installs get a real pre-upgrade safety net: backup_current
# must pg_dump the external DB (sanitizing SQLAlchemy driver suffixes libpq
# can't parse) and must HALT when the dump fails — updating a shared,
# migrate-in-place database with no dump is flying without a parachute.
# --------------------------------------------------------------------------
t="$WORK/t10e"; mkdir -p "$t/serverkit" "$t/backups"
printf 'DATABASE_URL=postgresql+psycopg2://user:secret@db.host:5432/serverkit\n' > "$t/serverkit/.env"
cat > "$STUB_BIN/pg_dump" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$1" > "$WORK/t10e/url.txt"
out=""; prev=""
for a in "\$@"; do [ "\$prev" = "-f" ] && out="\$a"; prev="\$a"; done
printf 'PGDMP fake contents\n' > "\$out"
exit \${PG_DUMP_RC:-0}
EOF
chmod +x "$STUB_BIN/pg_dump"
make_stub_pg_restore "$STUB_BIN"
bk_rc=0
(
    set -Eeuo pipefail
    DRY_RUN=0 INSTALL_DIR="$t/serverkit" BACKUP_DIR="$t/backups"
    backup_current
) >/dev/null 2>&1 || bk_rc=$?
dump_file="$(ls "$t/backups"/serverkit-pre-upgrade-*.dump 2>/dev/null | head -1)"
if [ "$bk_rc" -eq 0 ] && [ -n "$dump_file" ] \
   && [ "$(cat "$WORK/t10e/url.txt" 2>/dev/null)" = "postgresql://user:secret@db.host:5432/serverkit" ]; then
    ok "backup_current pg_dumps PostgreSQL installs (driver suffix sanitized)"
else
    bad "backup_current postgres: rc=$bk_rc, dump=[$dump_file], url=[$(cat "$WORK/t10e/url.txt" 2>/dev/null)]"
fi
rm -rf "$t/backups"; mkdir -p "$t/backups"
bk_rc=0
(
    set -Eeuo pipefail
    DRY_RUN=0 INSTALL_DIR="$t/serverkit" BACKUP_DIR="$t/backups"
    export PG_DUMP_RC=1
    backup_current
) >/dev/null 2>&1 || bk_rc=$?
if [ "$bk_rc" -ne 0 ]; then
    ok "backup_current halts when the PostgreSQL dump fails (no safety net, no update)"
else
    bad "backup_current proceeded despite a failed pg_dump (rc=0)"
fi

# The engine is decided by DATABASE_URL, never by which files happen to exist.
# An install migrated SQLite -> PostgreSQL keeps its old serverkit.db (every
# deploy copies it forward on file-existence alone), and backing THAT up
# instead of dumping left PRE_UPGRADE_DUMP unset — quietly disabling
# migrate_database's auto-restore, which reads it as "${PRE_UPGRADE_DUMP:-}",
# on the one engine that migrates in place and cannot roll back by switching
# slots. Needs no python3: the PostgreSQL path is pg_dump + pg_restore only.
t="$WORK/t10e2"; mkdir -p "$t/serverkit/backend/instance" "$t/backups"
printf 'DATABASE_URL=postgresql://user:secret@db.host:5432/serverkit\n' > "$t/serverkit/.env"
printf 'stale sqlite file left over from before the postgres migration\n' \
    > "$t/serverkit/backend/instance/serverkit.db"
lo_rc=0
(
    set -Eeuo pipefail
    DRY_RUN=0 INSTALL_DIR="$t/serverkit" BACKUP_DIR="$t/backups"
    backup_current
    printf '%s\n' "${PRE_UPGRADE_DUMP:-}" > "$t/dump-var.txt"
) >/dev/null 2>&1 || lo_rc=$?
lo_dump="$(ls "$t/backups"/serverkit-pre-upgrade-*.dump 2>/dev/null | head -1)"
lo_db="$(ls "$t/backups"/serverkit-pre-upgrade-*.db 2>/dev/null | head -1)"
if [ "$lo_rc" -eq 0 ] && [ -n "$lo_dump" ] && [ -z "$lo_db" ] \
   && [ "$(cat "$t/dump-var.txt" 2>/dev/null)" = "$lo_dump" ]; then
    ok "a leftover serverkit.db never diverts a PostgreSQL install away from pg_dump"
else
    bad "pg install w/ stale sqlite file: rc=$lo_rc, dump=[$lo_dump], db=[$lo_db], PRE_UPGRADE_DUMP=[$(cat "$t/dump-var.txt" 2>/dev/null)]"
fi

# An engine this updater cannot dump (MySQL) must say so. "No SQLite database"
# reads like a benign nothing-to-do while the update proceeds with no safety
# net at all. The warning names the SCHEME only — a DATABASE_URL carries a
# password, and this text lands in the update log.
t="$WORK/t10e3"; mkdir -p "$t/serverkit" "$t/backups"
printf 'DATABASE_URL=mysql://user:hunter2@db.host:3306/serverkit\n' > "$t/serverkit/.env"
my_rc=0
my_out="$(
    set -Eeuo pipefail
    DRY_RUN=0 INSTALL_DIR="$t/serverkit" BACKUP_DIR="$t/backups"
    backup_current 2>&1
)" || my_rc=$?
if [ "$my_rc" -eq 0 ] \
   && printf '%s' "$my_out" | grep -q "mysql" \
   && printf '%s' "$my_out" | grep -q "NO database backup" \
   && ! printf '%s' "$my_out" | grep -q "hunter2"; then
    ok "an undumpable engine is named in the warning, without leaking the password"
else
    bad "mysql warning: rc=$my_rc, out=[$my_out]"
fi
rm -f "$STUB_BIN/pg_dump" "$STUB_BIN/pg_restore"

# --------------------------------------------------------------------------
# T10f — backup verification helpers: a backup is only a safety net when it
# is PROVEN valid. verify_sqlite_db accepts a real DB and rejects garbage;
# verify_pg_dump rejects empty/truncated dumps.
# --------------------------------------------------------------------------
t="$WORK/t10f"; mkdir -p "$t"
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sqlite3' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    skip "verify_sqlite_db accepts valid DBs and rejects garbage (no working python3)"
else
    "$PY" - "$t/good.db" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('CREATE TABLE t(a)')
c.commit(); c.close()
PYEOF
    printf 'garbage not a database' > "$t/bad.db"
    v_rc=0
    (
        set -Eeuo pipefail
        verify_sqlite_db "$t/good.db" && ! verify_sqlite_db "$t/bad.db"
    ) >/dev/null 2>&1 || v_rc=$?
    if [ "$v_rc" -eq 0 ]; then
        ok "verify_sqlite_db accepts a valid DB and rejects a malformed one"
    else
        bad "verify_sqlite_db misclassified a fixture (rc=$v_rc)"
    fi
fi
: > "$t/empty.dump"; printf 'PGDMP\nstuff\n' > "$t/full.dump"
# A non-archive that is merely NON-EMPTY is the fixture that separates the two
# verify_pg_dump branches: the size check passes it, only the TOC listing
# catches it. Requires the stub — see make_stub_pg_restore.
printf 'ERROR: connection refused\n' > "$t/garbage.dump"
make_stub_pg_restore "$STUB_BIN"
v_rc=0
(
    set -Eeuo pipefail
    verify_pg_dump "$t/full.dump" && ! verify_pg_dump "$t/empty.dump" \
        && ! verify_pg_dump "$t/missing.dump" && ! verify_pg_dump "$t/garbage.dump"
) >/dev/null 2>&1 || v_rc=$?
if [ "$v_rc" -eq 0 ]; then
    ok "verify_pg_dump rejects empty/missing/non-archive dumps (TOC check via pg_restore)"
else
    bad "verify_pg_dump misclassified a fixture (rc=$v_rc)"
fi
rm -f "$STUB_BIN/pg_restore"

# --------------------------------------------------------------------------
# T10i — the SQLite helpers must NOT inherit the venv-builder's 3.11/3.12
# constraint. Snapshotting and integrity-checking a DB needs only an
# interpreter carrying the stdlib sqlite3 module; demanding 3.11/3.12 blocked
# updates on stock EL9 (python3.9) and Ubuntu 22.04 (python3.10) boxes for a
# reason unrelated to the data — including all-Docker installs, which return
# before preflight_check and do every real step inside the container, yet
# verified the extracted DB with the HOST's interpreter.
# --------------------------------------------------------------------------
t="$WORK/t10i"; mkdir -p "$t/bin" "$t/slot/backend/instance" "$t/slot/venv/bin"
if [ -z "$PY" ]; then
    skip "SQLite helpers work without a 3.11/3.12 interpreter (no working python3)"
else
    REAL_PY="$(command -v "$PY")"
    # An interpreter that answers a version probe with 3.9 but is otherwise a
    # real Python: makes locate_python fail exactly as it does on EL9, while
    # `import sqlite3` keeps working. Shadowing all four candidate names keeps
    # the precondition true regardless of what the host actually has.
    for n in python3.14 python3.13 python3.12 python3.11 python3 python; do
        cat > "$t/bin/$n" <<EOF
#!/usr/bin/env bash
case "\$*" in
    *version_info*) echo "3.9"; exit 0 ;;
esac
exec "$REAL_PY" "\$@"
EOF
        chmod +x "$t/bin/$n"
    done
    cp "$t/bin/python3" "$t/slot/venv/bin/python"
    "$PY" - "$t/slot/backend/instance/serverkit.db" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)')
c.executemany('INSERT INTO t(v) VALUES (?)', [('row%d' % i,) for i in range(100)])
c.commit(); c.close()
PYEOF
    el9_rc=0
    (
        set -Eeuo pipefail
        PATH="$t/bin:$PATH"
        DRY_RUN=0
        # Precondition: to the venv builder this box now looks like EL9.
        ! locate_python
        # The sqlite helpers still resolve an interpreter — and prefer the
        # install's own over anything on PATH, so they stay correct on a box
        # whose system python is unusable.
        [ "$(locate_sqlite_python "$t/slot/venv/bin/python")" = "$t/slot/venv/bin/python" ]
        copy_sqlite_db "$t/slot/backend/instance/serverkit.db" "$t/snap.db"
        verify_sqlite_db "$t/snap.db"
    ) >/dev/null 2>&1 || el9_rc=$?
    rows="$("$PY" -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('SELECT COUNT(*) FROM t').fetchone()[0])" "$t/snap.db" 2>/dev/null || true)"
    if [ "$el9_rc" -eq 0 ] && [ "$rows" = "100" ]; then
        ok "SQLite snapshot/verify need only sqlite3, not the gated Python 3.11-3.14 (EL9/22.04 boxes)"
    else
        bad "sqlite helpers still gated on locate_python: rc=$el9_rc, rows=[$rows], expected 100"
    fi
fi

# --------------------------------------------------------------------------
# T10g — a failed IN-PLACE PostgreSQL migration must trigger an automatic
# restore from the verified pre-upgrade dump: the DB is shared, so without a
# restore the half-migrated schema stays live against the old code.
# --------------------------------------------------------------------------
t="$WORK/t10g/serverkit-b"
mkdir -p "$t/venv/bin" "$t/backend/instance"
: > "$t/venv/bin/activate"
printf 'DATABASE_URL=postgresql://user:secret@db.host:5432/serverkit\n' > "$t/.env"
printf 'PGDMP fake contents\n' > "$WORK/t10g.dump"
cat > "$STUB_BIN/flask" <<'EOF'
#!/usr/bin/env bash
exit 1   # migration blows up mid-flight
EOF
cat > "$STUB_BIN/pg_restore" <<EOF
#!/usr/bin/env bash
echo "\$@" > "$WORK/t10g/restore-args.txt"
exit 0
EOF
chmod +x "$STUB_BIN/flask" "$STUB_BIN/pg_restore"
mig_rc=0
(
    set -Eeuo pipefail
    DRY_RUN=0
    PRE_UPGRADE_DUMP="$WORK/t10g.dump"
    migrate_database "$t"
) >/dev/null 2>&1 || mig_rc=$?
if [ "$mig_rc" -ne 0 ] && grep -q -- "--clean --if-exists -d postgresql://user:secret@db.host:5432/serverkit $WORK/t10g.dump" "$WORK/t10g/restore-args.txt" 2>/dev/null; then
    ok "failed in-place PostgreSQL migration auto-restores the pre-upgrade dump"
else
    bad "pg auto-restore: rc=$mig_rc, args=[$(cat "$WORK/t10g/restore-args.txt" 2>/dev/null)]"
fi
rm -f "$STUB_BIN/flask" "$STUB_BIN/pg_restore"

# --------------------------------------------------------------------------
# T10h — backup retention is capped (default 5, SERVERKIT_BACKUP_RETENTION
# overrides) across ALL backup kinds: full-size DB copies pile up fast on
# small VPSes (a real box carried 6.3G of stale backups on a 25G disk).
# --------------------------------------------------------------------------
t="$WORK/t10h"; mkdir -p "$t/backups"
for i in 1 2 3 4 5 6; do
    mkdir -p "$t/backups/serverkit-tree-2026010$i"
    : > "$t/backups/serverkit-pre-upgrade-2026010$i-00000$i.db"
    : > "$t/backups/serverkit-pre-upgrade-2026010$i-00000$i.dump"
    touch -d "2026-01-0$i 00:00" "$t/backups/serverkit-tree-2026010$i" \
        "$t/backups/serverkit-pre-upgrade-2026010$i-00000$i.db" \
        "$t/backups/serverkit-pre-upgrade-2026010$i-00000$i.dump" 2>/dev/null || true
done
ret_rc=0
(
    set -Eeuo pipefail
    DRY_RUN=0 BACKUP_DIR="$t/backups" SERVERKIT_BACKUP_RETENTION=3
    cleanup
) >/dev/null 2>&1 || ret_rc=$?
left_trees=$(find "$t/backups" -mindepth 1 -maxdepth 1 -name 'serverkit-tree-*' | wc -l)
left_dbs=$(find "$t/backups" -mindepth 1 -maxdepth 1 -name '*.db' | wc -l)
left_dumps=$(find "$t/backups" -mindepth 1 -maxdepth 1 -name '*.dump' | wc -l)
if [ "$ret_rc" -eq 0 ] && [ "$left_trees" -eq 3 ] && [ "$left_dbs" -eq 3 ] && [ "$left_dumps" -eq 3 ]; then
    ok "cleanup prunes all backup kinds to SERVERKIT_BACKUP_RETENTION (3 kept)"
else
    bad "retention: rc=$ret_rc, left trees=$left_trees db=$left_dbs dump=$left_dumps (want 3/3/3)"
fi

# --------------------------------------------------------------------------
# T11 — zero-downtime regression: reload_nginx_graceful must RELOAD a running
# nginx and must NEVER stop it. Host nginx fronts every managed app, so a stop
# during a panel update used to black out unrelated sites. A recording systemctl
# stub (PATH-prepended ahead of the global stub) captures every invocation.
# --------------------------------------------------------------------------
t="$WORK/t11"; mkdir -p "$t/bin"
CALL_LOG="$t/calls.log"; : > "$CALL_LOG"
cat > "$t/bin/systemctl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CALL_LOG"
exit 0                       # is-active --quiet nginx → running
EOF
cat > "$t/bin/nginx" <<EOF
#!/usr/bin/env bash
printf 'nginx %s\n' "\$*" >> "$CALL_LOG"
exit 0                       # nginx -t passes
EOF
chmod +x "$t/bin"/*
if (
    set -Eeuo pipefail
    export PATH="$t/bin:$PATH"
    DRY_RUN=0
    reload_nginx_graceful
) >/dev/null 2>&1; then
    if grep -q 'reload nginx' "$CALL_LOG" && ! grep -q 'stop nginx' "$CALL_LOG"; then
        ok "reload_nginx_graceful reloads a running nginx and never stops it (zero-downtime)"
    else
        bad "reload_nginx_graceful must reload (not stop) nginx; saw: $(tr '\n' ';' < "$CALL_LOG")"
    fi
else
    bad "reload_nginx_graceful returned non-zero against a healthy running nginx"
fi

# --------------------------------------------------------------------------
# T12 — when nginx is NOT running, reload_nginx_graceful starts it (instead of
# reloading a dead service) and still never issues a stop. The is-active gate
# reports inactive on its first probe, then active so wait_for_service returns
# immediately (keeps the test sub-second).
# --------------------------------------------------------------------------
t="$WORK/t12"; mkdir -p "$t/bin"
CALL_LOG="$t/calls.log"; : > "$CALL_LOG"; : > "$t/probe"
cat > "$t/bin/systemctl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CALL_LOG"
if [ "\$*" = "is-active --quiet nginx" ]; then
    n=\$(cat "$t/probe" 2>/dev/null || echo 0); echo \$((n + 1)) > "$t/probe"
    [ "\$n" -ge 1 ] && exit 0 || exit 1      # 1st probe: down → start branch; then up
fi
exit 0
EOF
chmod +x "$t/bin"/*
if (
    set -Eeuo pipefail
    export PATH="$t/bin:$PATH"
    DRY_RUN=0
    reload_nginx_graceful
) >/dev/null 2>&1; then
    if grep -q 'start nginx' "$CALL_LOG" && ! grep -q 'stop nginx' "$CALL_LOG" \
       && ! grep -q 'reload nginx' "$CALL_LOG"; then
        ok "reload_nginx_graceful starts a stopped nginx (never reloads a dead unit, never stops)"
    else
        bad "reload_nginx_graceful should start (not reload/stop) a dead nginx; saw: $(tr '\n' ';' < "$CALL_LOG")"
    fi
else
    bad "reload_nginx_graceful returned non-zero while starting a stopped nginx"
fi

# --------------------------------------------------------------------------
# T13 — guard against the old behaviour creeping back: the update.sh source must
# not contain a literal `systemctl stop nginx`. The forward and rollback paths
# both route nginx through reload_nginx_graceful now.
# --------------------------------------------------------------------------
if grep -nq 'systemctl stop nginx' "$UPDATE_SH"; then
    bad "update.sh still contains 'systemctl stop nginx' — apps would black out on update"
else
    ok "update.sh never stops nginx (no 'systemctl stop nginx' anywhere)"
fi

# --------------------------------------------------------------------------
# T14 — the panel frontend is served statically from $INSTALL_DIR/frontend/dist.
# refresh_config must repoint the shipped `root` (default /opt/serverkit) at a
# customised SERVERKIT_DIR, or a custom install dir would 404 the whole panel
# after an upgrade.
# --------------------------------------------------------------------------
t="$WORK/t14"
mkdir -p "$t/nginx/sites-available" "$t/nginx/sites-enabled" "$t/target/nginx/sites-available"
printf 'http {\n}\n' > "$t/nginx/nginx.conf"
printf 'server {\n  root /opt/serverkit/frontend/dist;\n  location / { try_files $uri /index.html; }\n}\n' \
    > "$t/target/nginx/sites-available/serverkit-insecure.conf"
t14_rc=0
(
    set -Eeuo pipefail
    NGINX_DIR="$t/nginx"; LETSENCRYPT_DIR="$t/le"; SYSTEMD_DIR="$t/sysd"; CONFIG_DIR="$t/cfg"; DRY_RUN=0
    INSTALL_DIR="$WORK/opt/serverkit"     # non-default → substitution must fire
    refresh_config "$t/target"
) >/dev/null 2>&1 || t14_rc=$?
installed="$t/nginx/sites-available/serverkit-insecure.conf"
if [ "$t14_rc" -eq 0 ] \
   && grep -q "root $WORK/opt/serverkit/frontend/dist;" "$installed" \
   && ! grep -q "root /opt/serverkit/frontend/dist;" "$installed"; then
    ok "refresh_config repoints the static-frontend root at a custom SERVERKIT_DIR"
else
    bad "refresh_config rc=$t14_rc or did not rewrite the frontend dist root: $(grep -n root "$installed" | tr '\n' ';')"
fi

# --------------------------------------------------------------------------
# T15 — the shipped nginx sites serve the SPA statically (host nginx, no
# container): each must carry a frontend/dist root + a try_files SPA fallback
# and must NOT proxy the retired frontend container on :3847.
# --------------------------------------------------------------------------
SK_ROOT="$SCRIPT_DIR/../.."
for f in serverkit.conf serverkit-insecure.conf; do
    cfg="$SK_ROOT/nginx/sites-available/$f"
    if grep -q 'frontend/dist' "$cfg" && grep -q 'try_files' "$cfg" \
       && ! grep -q '127.0.0.1:3847' "$cfg"; then
        ok "$f serves the SPA statically (dist root + try_files, no :3847 proxy)"
    else
        bad "$f is not a clean static-serve config (still proxying :3847?)"
    fi
    # The HTML entrypoint must revalidate every load (expires -1 → no-cache):
    # a cached index.html references hashed assets that vanish on the next
    # blue/green switch, bricking the panel behind any caching layer.
    if grep -q 'expires -1;' "$cfg"; then
        ok "$f marks the SPA entrypoint no-cache (expires -1)"
    else
        bad "$f lets the HTML entrypoint be cached — stale index.html breaks after a slot switch"
    fi
done

# --------------------------------------------------------------------------
# T15b — report_stale_panel_vhosts: a leftover custom vhost proxying the
# retired :3847 frontend container serves a permanently stale panel bundle
# (2026-07-03 incident: looked exactly like a browser/CDN cache problem).
# The check must WARN and name the file — and never abort, even with no
# sites-enabled dir at all (fresh box; also covered by the loop below).
# --------------------------------------------------------------------------
t="$WORK/t15b"; mkdir -p "$t/nginx/sites-enabled"
printf 'server {\n  location / {\n    proxy_pass http://127.0.0.1:3847;\n  }\n}\n' \
    > "$t/nginx/sites-enabled/legacy-panel.conf"
printf 'server {\n  root /opt/serverkit/frontend/dist;\n}\n' \
    > "$t/nginx/sites-enabled/clean.conf"
if out="$( set -Eeuo pipefail; NGINX_DIR="$t/nginx"; INSTALL_DIR="/opt/serverkit"
           report_stale_panel_vhosts 2>&1 )" \
   && printf '%s' "$out" | grep -q 'legacy-panel.conf' \
   && ! printf '%s' "$out" | grep -q 'clean.conf'; then
    ok "report_stale_panel_vhosts warns about (only) vhosts proxying :3847"
else
    bad "report_stale_panel_vhosts missed the stale vhost or flagged a clean one: [$out]"
fi
if out="$( set -Eeuo pipefail; NGINX_DIR="$t/nowhere"; INSTALL_DIR="/opt/serverkit"
           report_stale_panel_vhosts 2>&1 )" && [ -z "$out" ]; then
    ok "report_stale_panel_vhosts is silent (rc 0) with no sites-enabled dir"
else
    bad "report_stale_panel_vhosts failed or spoke on a missing dir: [$out]"
fi

# --------------------------------------------------------------------------
# T16 — app-uptime verification: discover_app_upstreams must extract the unique
# set of app container upstreams from the per-app nginx location snippets (this
# is the list the updater probes to prove apps stayed up).
# --------------------------------------------------------------------------
t="$WORK/t16"; mkdir -p "$t/loc"
printf 'location /app1 { proxy_pass http://127.0.0.1:8001; }\n' > "$t/loc/app1.conf"
printf 'location /app2  { proxy_pass http://127.0.0.1:8002/; }\nlocation /app2b { proxy_pass http://127.0.0.1:8001; }\n' > "$t/loc/app2.conf"
if ! res="$( set -Eeuo pipefail; APP_LOCATIONS_DIR="$t/loc"; discover_app_upstreams | tr '\n' ',' )"; then
    bad "discover_app_upstreams aborted on a populated app-locations directory"
elif [ "$res" = "127.0.0.1:8001,127.0.0.1:8002," ]; then
    ok "discover_app_upstreams extracts the unique app upstreams from location snippets"
else
    bad "discover_app_upstreams returned [$res], expected the two unique upstreams"
fi

# T16b — empty app-locations directory must not abort under set -euo pipefail.
mkdir -p "$t/empty"
if res="$( set -Eeuo pipefail; APP_LOCATIONS_DIR="$t/empty"; discover_app_upstreams )"; then
    if [ -z "$res" ]; then
        ok "discover_app_upstreams tolerates an empty app-locations directory"
    else
        bad "discover_app_upstreams returned [$res] for an empty directory, expected empty"
    fi
else
    bad "discover_app_upstreams aborted on an empty app-locations directory"
fi

# --------------------------------------------------------------------------
# T17 — report_app_uptime_regressions flags an app that was reachable before the
# update and is not after (and ignores one that was already down), returning
# non-zero so the operator is warned; the clean case returns success.
# --------------------------------------------------------------------------
before=$'127.0.0.1:8001 up\n127.0.0.1:8002 up\n127.0.0.1:8003 down'
after=$'127.0.0.1:8001 up\n127.0.0.1:8002 down\n127.0.0.1:8003 down'
if ( set -Eeuo pipefail; report_app_uptime_regressions "$before" "$after" ) >/dev/null 2>&1; then
    bad "report_app_uptime_regressions should flag the app that went up->down"
else
    ok "report_app_uptime_regressions flags an app that went down across the update"
fi
if ( set -Eeuo pipefail; report_app_uptime_regressions "$before" "$before" ) >/dev/null 2>&1; then
    ok "report_app_uptime_regressions passes when every app that was up is still up"
else
    bad "report_app_uptime_regressions should pass when nothing regressed"
fi

# --------------------------------------------------------------------------
# T18 — preserve_installed_plugins (#48): user-installed plugin dirs are
# carried into the new tree; dirs the new tree already ships are NOT
# overwritten; __pycache__ is skipped; and the function never dies under
# set -e (it runs inside deploy_source/deploy_release).
# --------------------------------------------------------------------------
t="$WORK/t18"
mkdir -p "$t/old/backend/app/plugins/third-party" \
         "$t/old/frontend/src/plugins/third-party" \
         "$t/old/backend/app/plugins/shipped-plugin" \
         "$t/old/backend/app/plugins/__pycache__" \
         "$t/new/backend/app/plugins/shipped-plugin"
printf 'user-code\n' > "$t/old/backend/app/plugins/third-party/blueprint.py"
printf 'user-ui\n'   > "$t/old/frontend/src/plugins/third-party/index.jsx"
printf 'old-copy\n'  > "$t/old/backend/app/plugins/shipped-plugin/__init__.py"
printf 'stale\n'     > "$t/old/backend/app/plugins/__pycache__/x.pyc"
printf 'repo-copy\n' > "$t/new/backend/app/plugins/shipped-plugin/__init__.py"
if (
    # One && chain, not sequential statements: bash suppresses set -e inside
    # an if-condition subshell, so only an explicit chain makes every
    # assertion gate the result (same fix as test_install.sh T16).
    set -Eeuo pipefail
    preserve_installed_plugins "$t/old" "$t/new" \
        && [ -f "$t/new/backend/app/plugins/third-party/blueprint.py" ] \
        && [ -f "$t/new/frontend/src/plugins/third-party/index.jsx" ] \
        && [ ! -d "$t/new/backend/app/plugins/__pycache__" ] \
        && grep -q repo-copy "$t/new/backend/app/plugins/shipped-plugin/__init__.py"
) >/dev/null 2>&1; then
    ok "preserve_installed_plugins carries user plugins forward without clobbering repo-shipped ones"
else
    bad "preserve_installed_plugins lost a user plugin, clobbered a shipped one, or died under set -e"
fi

# --------------------------------------------------------------------------
# T19 — U1: backup pruning must be a no-op on an empty/missing BACKUP_DIR.
# The old `ls -t <glob> | tail | xargs` prune exited 2 on an unmatched glob,
# aborting the updater AFTER a successful update on any box with no prior
# backups. Also prove the retention itself: newest 10 kept, oldest removed.
# --------------------------------------------------------------------------
t="$WORK/t19"; mkdir -p "$t/empty" "$t/ret"
prune_survives() {
    ( set -Eeuo pipefail; BACKUP_DIR="$t/empty"
      prune_old_backups 'serverkit-tree-*' 10 \
          && prune_old_backups 'serverkit-pre-upgrade-*.db' 10 ) &&
    ( set -Eeuo pipefail; BACKUP_DIR="$t/gone"; prune_old_backups 'serverkit-tree-*' 10 ) &&
    ( set -Eeuo pipefail; BACKUP_DIR="$t/empty"; INSTALL_DIR="$t/no-install"; DRY_RUN=0; cleanup )
}
if prune_survives >/dev/null 2>&1; then
    ok "cleanup/prune are a clean no-op on an empty or missing BACKUP_DIR (post-update abort bug)"
else
    bad "backup pruning DIED on an empty/missing BACKUP_DIR — the ls-glob abort is back"
fi

for i in 01 02 03 04 05 06 07 08 09 10 11 12; do
    mkdir -p "$t/ret/serverkit-tree-2026$i"
    touch -d "2026-01-$i 12:00:00" "$t/ret/serverkit-tree-2026$i"
done
( set -Eeuo pipefail; BACKUP_DIR="$t/ret"; prune_old_backups 'serverkit-tree-*' 10 ) >/dev/null 2>&1
remaining="$(find "$t/ret" -mindepth 1 -maxdepth 1 -name 'serverkit-tree-*' | wc -l)"
if [ "$remaining" -eq 10 ] && [ ! -d "$t/ret/serverkit-tree-202601" ] \
   && [ ! -d "$t/ret/serverkit-tree-202602" ] && [ -d "$t/ret/serverkit-tree-202612" ]; then
    ok "prune_old_backups keeps the newest 10 backups and removes the oldest"
else
    bad "prune retention wrong: $remaining left, expected the newest 10"
fi

# --------------------------------------------------------------------------
# T20 — U2: download_release's stdout is CAPTURED by its caller
# (tarball="\$(download_release ...)"), so it must print exactly one line — the
# tarball path — with every step/good/warn routed to stderr. A progress line on
# stdout used to hand tar a 4-line "filename", breaking every --release update.
# --------------------------------------------------------------------------
t="$WORK/t20"; mkdir -p "$t/bin" "$t/payload"
case "$(uname -m)" in
    x86_64|amd64)  tarch="amd64" ;;
    aarch64|arm64) tarch="arm64" ;;
    *)             tarch="" ;;
esac
if [ -z "$tarch" ] || ! command -v sha256sum >/dev/null 2>&1; then
    skip "download_release stdout purity — needs sha256sum + a known arch (runs on Linux CI)"
else
    printf 'fake-release-bytes\n' > "$t/payload/tarball"
    sha="$(sha256sum "$t/payload/tarball" | cut -d' ' -f1)"
    printf '%s  serverkit-v9.9.9-linux-%s.tar.gz\n' "$sha" "$tarch" > "$t/payload/checksums.txt"
    # curl stub that actually materialises the -o target, so the mirror path
    # runs end-to-end (download + checksum verification) without a network.
    cat > "$t/bin/curl" <<EOF
#!/usr/bin/env bash
out=""; url=""
while [ \$# -gt 0 ]; do
    case "\$1" in
        -o) out="\$2"; shift 2 ;;
        -*) shift ;;
        *)  url="\$1"; shift ;;
    esac
done
case "\$url" in
    *checksums.txt) cp "$t/payload/checksums.txt" "\$out" ;;
    *)              cp "$t/payload/tarball" "\$out" ;;
esac
EOF
    chmod +x "$t/bin/curl"
    out="$(
        set -Eeuo pipefail
        export PATH="$t/bin:$PATH"
        SERVERKIT_OFFLINE_TARBALL=""
        SERVERKIT_MIRROR_URL="http://mirror.invalid"
        download_release v9.9.9 2>"$t/stderr.txt"
    )"
    rc=$?
    if [ "$rc" -eq 0 ] && [ -n "$out" ] && [ -f "$out" ] \
       && [ "$(printf '%s' "$out" | wc -l)" -eq 0 ] \
       && grep -q 'Downloading release tarball' "$t/stderr.txt"; then
        ok "download_release captures to a single existing path (progress → stderr)"
    else
        bad "download_release polluted its captured stdout (rc=$rc): [$out]"
    fi
    off="$t/offline.tar.gz"; printf 'x' > "$off"
    if ! out2="$( set -Eeuo pipefail; SERVERKIT_OFFLINE_TARBALL="$off"; download_release v9.9.9 2>/dev/null )"; then
        bad "download_release (offline tarball) aborted under set -Eeuo pipefail"
    elif [ "$out2" = "$off" ]; then
        ok "download_release (offline tarball) returns exactly the tarball path"
    else
        bad "download_release (offline) returned [$out2], expected [$off]"
    fi
fi

# --------------------------------------------------------------------------
# T20b — deploy_release removes its download and unpack dirs. It used to leave
# the ~70 MB tarball's mktemp dir behind on every update (a 25 GB box had 11
# of them, 644 MB), and a failed download stranded its dir as well. An
# operator's offline tarball must survive; stale leftovers are swept.
# --------------------------------------------------------------------------
t="$WORK/t20b"; mkdir -p "$t/bin" "$t/tmp" "$t/src/serverkit/scripts" "$t/live" "$t/slot"
if [ -z "$tarch" ] || ! command -v sha256sum >/dev/null 2>&1; then
    skip "deploy_release scratch cleanup — needs sha256sum + a known arch (runs on Linux CI)"
else
    printf '#!/bin/sh\n' > "$t/src/serverkit/serverkit"
    tar czf "$t/release.tar.gz" -C "$t/src" serverkit
    sha="$(sha256sum "$t/release.tar.gz" | cut -d' ' -f1)"
    printf '%s  serverkit-v9.9.9-linux-%s.tar.gz\n' "$sha" "$tarch" > "$t/checksums.txt"
    cat > "$t/bin/curl" <<EOF
#!/usr/bin/env bash
out=""; url=""
while [ \$# -gt 0 ]; do
    case "\$1" in -o) out="\$2"; shift 2 ;; -*) shift ;; *) url="\$1"; shift ;; esac
done
[ -n "\${T20B_CURL_FAIL:-}" ] && exit 22
case "\$url" in
    *checksums.txt) cp "$t/checksums.txt" "\$out" ;;
    *)              cp "$t/release.tar.gz" "\$out" ;;
esac
EOF
    chmod +x "$t/bin/curl"
    # A leftover from an older updater (swept) and a fresh one (kept: it could
    # be a concurrent download).
    mkdir -p "$t/tmp/tmp.OLDLEFTOVER" "$t/tmp/tmp.FRESHDL"
    : > "$t/tmp/tmp.OLDLEFTOVER/serverkit-v1.0.0-linux-amd64.tar.gz"
    : > "$t/tmp/tmp.FRESHDL/serverkit-v1.0.1-linux-amd64.tar.gz"
    touch -d '2 hours ago' "$t/tmp/tmp.OLDLEFTOVER/serverkit-v1.0.0-linux-amd64.tar.gz"

    _t20b_deploy() {
        (
            set -Eeuo pipefail
            export PATH="$t/bin:$PATH" TMPDIR="$t/tmp"
            DRY_RUN=0; INSTALL_DIR="$t/live"; SERVERKIT_MIRROR_URL="http://mirror.invalid"
            SERVERKIT_OFFLINE_TARBALL="${1:-}"
            preserve_installed_plugins() { :; }
            trap cleanup_on_exit EXIT
            deploy_release "$t/slot/target" v9.9.9
        ) >/dev/null 2>&1
    }
    _t20b_scratch() { find "$t/tmp" -mindepth 1 -maxdepth 1 ! -name tmp.FRESHDL | wc -l; }

    if _t20b_deploy && [ -x "$t/slot/target/serverkit" ] && [ "$(_t20b_scratch)" -eq 0 ] \
       && [ -d "$t/tmp/tmp.FRESHDL" ]; then
        ok "deploy_release leaves no download/unpack dir behind and sweeps stale ones"
    else
        bad "deploy_release left scratch in TMPDIR: [$(ls "$t/tmp" | tr '\n' ' ')]"
    fi

    if _t20b_deploy "$t/release.tar.gz" && [ -f "$t/release.tar.gz" ] && [ "$(_t20b_scratch)" -eq 0 ]; then
        ok "deploy_release keeps an operator's offline tarball"
    else
        bad "deploy_release deleted the offline tarball or left scratch: [$(ls "$t/tmp" | tr '\n' ' ')]"
    fi

    if ! T20B_CURL_FAIL=1 _t20b_deploy && [ "$(_t20b_scratch)" -eq 0 ]; then
        ok "a failed download leaves no dir behind"
    else
        bad "a failed download stranded its dir: [$(ls "$t/tmp" | tr '\n' ' ')]"
    fi
fi

# --------------------------------------------------------------------------
# T21 — U5/U8: a health-check-triggered rollback halts, which fires the EXIT
# trap (cleanup_on_exit) — that trap must NOT run a second rollback on top of
# the one that just finished. And the rollback itself must never abort
# mid-flight: a failed daemon-reload still has to end in a started backend.
# --------------------------------------------------------------------------
t="$WORK/t21"; mkdir -p "$t/bin" "$t/prev"
CALL_LOG="$t/calls.log"; : > "$CALL_LOG"
cat > "$t/bin/systemctl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CALL_LOG"
[ "\$1" = "daemon-reload" ] && exit 1               # fails mid-rollback (U8)
if [ "\$1" = "is-active" ]; then
    grep -q '^start ' "$CALL_LOG" && exit 0         # active once a start was issued
    exit 1                                          # inactive before that
fi
exit 0
EOF
chmod +x "$t/bin/systemctl"
(
    set -Eeuo pipefail
    export PATH="$t/bin:$PATH"
    DRY_RUN=0
    INSTALL_DIR="$t/serverkit"
    PREVIOUS_DIR="$t/prev"
    # atomic_switch needs real symlinks (unsupported on this fs — see T4); the
    # unit under test is rollback's re-entry discipline, so stub the switch.
    atomic_switch() { printf 'switched-to %s\n' "$1" >> "$CALL_LOG"; }
    trap cleanup_on_exit EXIT
    rollback
) > "$t/out.txt" 2>&1
n="$(grep -c 'rolling back to previous slot' "$t/out.txt")"
if [ "$n" = "1" ]; then
    ok "rollback runs exactly once through the halt → EXIT-trap path (no double rollback)"
else
    bad "rollback transcript appeared $n time(s), expected exactly once"
fi
if grep -q '^daemon-reload' "$CALL_LOG" && grep -q '^start serverkit' "$CALL_LOG"; then
    ok "rollback survives a failed daemon-reload and still starts the backend (never aborts mid-flight)"
else
    bad "rollback aborted mid-flight on a failed daemon-reload; calls: $(tr '\n' ';' < "$CALL_LOG")"
fi

# --------------------------------------------------------------------------
# T22 — U4: nginx down AND refusing to start must not abort the updater
# post-switch (which would then abort AGAIN inside the rollback's EXIT trap).
# reload_nginx_graceful warns and returns 0 no matter what nginx does.
# --------------------------------------------------------------------------
t="$WORK/t22"; mkdir -p "$t/bin"
CALL_LOG="$t/calls.log"; : > "$CALL_LOG"
cat > "$t/bin/systemctl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CALL_LOG"
[ "\$1" = "start" ] && exit 1                       # nginx refuses to start
if [ "\$1" = "is-active" ]; then
    grep -q '^start ' "$CALL_LOG" && exit 0 || exit 1
fi
exit 0
EOF
chmod +x "$t/bin/systemctl"
if (
    set -Eeuo pipefail
    export PATH="$t/bin:$PATH"
    DRY_RUN=0
    reload_nginx_graceful
) >/dev/null 2>&1; then
    ok "reload_nginx_graceful never propagates a failed nginx start (post-switch abort averted)"
else
    bad "reload_nginx_graceful aborted when nginx was down and refused to start"
fi

# --------------------------------------------------------------------------
# T23 — U13: refresh_config runs `systemctl daemon-reload` pre-switch; a
# failing reload (degraded systemd, container, chroot) must warn, not abort.
# --------------------------------------------------------------------------
t="$WORK/t23"
mkdir -p "$t/nginx/sites-available" "$t/nginx/sites-enabled" "$t/target/nginx/sites-available" "$t/bin"
printf 'http {\n}\n' > "$t/nginx/nginx.conf"
printf 'server { listen 80; }\n' > "$t/target/nginx/sites-available/serverkit-insecure.conf"
cat > "$t/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
[ "$1" = "daemon-reload" ] && exit 1
exit 0
EOF
chmod +x "$t/bin/systemctl"
if (
    set -Eeuo pipefail
    export PATH="$t/bin:$PATH"
    NGINX_DIR="$t/nginx"; LETSENCRYPT_DIR="$t/le"; SYSTEMD_DIR="$t/sysd"; CONFIG_DIR="$t/cfg"; DRY_RUN=0
    refresh_config "$t/target"
) >/dev/null 2>&1; then
    ok "refresh_config warns (not aborts) when systemctl daemon-reload fails"
else
    bad "refresh_config aborted on a failing daemon-reload"
fi

# --------------------------------------------------------------------------
# T24 — U9: migrate_database used to `cd` into the new slot's backend and leak
# that cwd into the main shell for the rest of the update. It now runs the
# activation + cd in a subshell, leaving the caller's cwd untouched.
# --------------------------------------------------------------------------
t="$WORK/t24/serverkit-b"
mkdir -p "$t/venv/bin" "$t/backend/instance"
: > "$t/venv/bin/activate"
: > "$t/backend/instance/serverkit.db"
printf '#!/usr/bin/env bash\necho ok\n' > "$t/venv/bin/python"  # integrity probe: healthy DB
chmod +x "$t/venv/bin/python"
printf 'DATABASE_URL=sqlite:////opt/serverkit/backend/instance/serverkit.db\n' > "$t/.env"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB_BIN/flask"
chmod +x "$STUB_BIN/flask"
exp="$(cd "$WORK" && pwd)"
t24_rc=0
res="$(
    # && chain: this capture sits on the left of `||`, where bash suppresses
    # set -e even inside the substitution — sequential statements would let a
    # failing cd/migrate slip through ungated.
    set -Eeuo pipefail
    DRY_RUN=0
    cd "$WORK" \
        && migrate_database "$t" >/dev/null 2>&1 \
        && pwd
)" || t24_rc=$?
rm -f "$STUB_BIN/flask"
if [ "$t24_rc" -eq 0 ] && [ "$res" = "$exp" ]; then
    ok "migrate_database leaves the caller's cwd untouched (no cd leak)"
else
    bad "migrate_database rc=$t24_rc or leaked its cd: caller cwd is now [$res]"
fi

# --------------------------------------------------------------------------
# T25 — U12: the EXIT trap (cleanup_on_exit) must preserve the script's exit
# code AND return 0 — returning \$rc re-fired the ERR trap from inside the
# trap, appending a second, misleading "Update aborted" report.
# --------------------------------------------------------------------------
t="$WORK/t25"; mkdir -p "$t"
(
    set -Eeuo pipefail
    trap 'printf "ERR-REFIRED\n"' ERR
    trap cleanup_on_exit EXIT
    DRY_RUN=1
    exit 3
) > "$t/out.txt" 2>&1
rc=$?
if [ "$rc" -eq 3 ] && ! grep -q 'ERR-REFIRED' "$t/out.txt"; then
    ok "cleanup_on_exit preserves the exit code and never re-fires the ERR trap"
else
    bad "cleanup_on_exit changed rc to $rc or re-fired the ERR trap"
fi

# --------------------------------------------------------------------------
# T26 — regression guards baked into the source (same spirit as T13): the
# audited failure shapes must never creep back in.
# --------------------------------------------------------------------------
if ! grep -qE '^[^#]*ls -t "\$BACKUP_DIR"' "$UPDATE_SH"; then    # code lines only — the fix's comment cites the old idiom
    ok "update.sh never prunes backups via an ls-glob (aborts on empty BACKUP_DIR)"
else
    bad "an 'ls -t \"\$BACKUP_DIR\"' prune is back — unmatched globs abort under pipefail"
fi
if grep -q 'required+=(npm)' "$UPDATE_SH"; then
    ok "preflight requires npm for source-mode updates (no mid-update discovery)"
else
    bad "npm is missing from the source-mode preflight required-tools list"
fi
if grep -qF '${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}' "$UPDATE_SH"; then
    ok "self-update re-exec guards the empty-ORIG_ARGS expansion (set -u on bash < 4.4)"
else
    bad "the re-exec expands ORIG_ARGS unguarded — fatal with no args under set -u on old bash"
fi
if ! grep -qF 'frontend.*Up' "$UPDATE_SH"; then
    ok "health_check no longer probes the retired frontend container"
else
    bad "the vestigial 'frontend container Up' check is still in update.sh"
fi

# --------------------------------------------------------------------------
# T27 — the fresh-minimal-box loop. On 2026-07-02 a production update aborted
# because discover_app_upstreams() grep'd an EMPTY /etc/nginx/serverkit-
# locations dir (the default state of every fresh install) and set -Eeuo
# pipefail killed the bare APP_BASELINE="$(snapshot_app_reachability)"
# assignment. The function HAD a unit test — but only with a populated
# fixture. This loop runs EVERY observation/discovery/snapshot/report/probe
# function against the emptiest valid world a fresh box presents (zero apps,
# zero containers, no optional confs, dead network, empty state), under the
# script's own set -Eeuo pipefail.
#
# POLICY: every new observation/discovery/snapshot/report function added to
# scripts/update.sh MUST be appended here (add args/disposition to the case
# table when needed). Dispositions:
#   must0 — output is captured by assignment somewhere (X="$(fn)"), so a
#           non-zero exit IS the outage: the function must exit 0.
#   pred  — used only in conditional position (if fn; ...), where a clean
#           non-zero answer is legitimate: it must merely return normally
#           (no set -u crash, no signal death).
# --------------------------------------------------------------------------
FRESH="$WORK/freshbox"
make_fresh_box_fixture "$FRESH"

UPDATE_OBSERVERS=(
    local_version versions_equal remote_release_tag is_already_current
    version_gate active_real_dir next_real_dir discover_app_upstreams
    probe_app_upstreams snapshot_app_reachability report_app_uptime_regressions
    locate_python wait_for_service quick_health is_docker_deployment
    snapshot_docker_state report_failure report_stale_panel_vhosts
)

loop_fail=0
for fn in "${UPDATE_OBSERVERS[@]}"; do
    args=(); mode=must0
    case "$fn" in
        versions_equal)                args=(1.7.0 v1.7.0) ;;
        report_failure)                args=(2 42 'grep conf') ;;
        report_app_uptime_regressions) args=("" "") ;;
        wait_for_service)              args=(serverkit inactive 1) ;;
        quick_health)                  args=(1); mode=pred ;;
        is_already_current|is_docker_deployment|locate_python) mode=pred ;;
    esac
    out="$(
        {
            set -Eeuo pipefail
            export PATH="$FRESH/bin:$PATH"
            INSTALL_DIR="$FRESH/opt/serverkit"; SERVERKIT_DIR="$INSTALL_DIR"
            DIR_A="$FRESH/opt/serverkit-a"; DIR_B="$FRESH/opt/serverkit-b"
            APP_LOCATIONS_DIR="$FRESH/etc/nginx/serverkit-locations"
            NGINX_DIR="$FRESH/etc/nginx"; CONFIG_DIR="$FRESH/etc/serverkit"
            BACKUP_DIR="$FRESH/var/backups"; LOG_DIR="$FRESH/var/log"
            DRY_RUN=0; FORCE_UPDATE=0; USE_RELEASE=0
            RELEASE_VERSION=""; TARGET_BRANCH=""; SERVERKIT_OFFLINE_TARBALL=""
            "$fn" ${args[@]+"${args[@]}"}
        } </dev/null 2>&1
    )"
    rc=$?
    if [ "$mode" = "must0" ] && [ "$rc" -ne 0 ]; then
        bad "fresh-box loop: $fn exited $rc on a zero-app box: [$(printf '%s' "$out" | tail -c 160)]"
        loop_fail=1
    elif [ "$mode" = "pred" ] && { [ "$rc" -gt 128 ] || printf '%s' "$out" | grep -q 'unbound variable'; }; then
        bad "fresh-box loop: predicate $fn crashed (rc=$rc) on a zero-app box: [$(printf '%s' "$out" | tail -c 160)]"
        loop_fail=1
    fi
done
if [ "$loop_fail" = "0" ]; then
    ok "fresh-box loop: all ${#UPDATE_OBSERVERS[@]} observation/discovery functions survive a zero-app box"
fi

# --------------------------------------------------------------------------
# T28 — the best-effort contract as an executable promise: the app-uptime
# verification composed exactly the way update.sh's run block wires it —
#     APP_BASELINE="$(snapshot_app_reachability)"
#     ... stop/switch/start ...
#     phase "Verifying App Uptime"
#     APP_AFTER="$(snapshot_app_reachability)"
#     report_app_uptime_regressions "$APP_BASELINE" "$APP_AFTER" || true
# — must exit 0 under set -Eeuo pipefail on (a) a fresh zero-app box (the
# exact 2026-07-02 world) and (b) a box where an app genuinely regressed:
# the report warns loudly, but the `|| true` keeps verification from failing
# an already-healthy update. The outage commit made that promise only in
# prose; this test makes it executable.
# --------------------------------------------------------------------------
t="$WORK/t28"; mkdir -p "$t/binup" "$t/bindown" "$t/loc"
make_stub_ok          "$t/binup" curl      # every upstream answers → "up"
make_stub_curl_fail22 "$t/bindown"         # every upstream refuses → "down"

t28_rc=0
out="$(
    {
        # && chain: this capture sits on the left of `||`, where bash
        # suppresses set -e even inside the substitution. The chain gates on
        # every statement, which is exactly what set -e does to the real
        # (sequential) run block.
        set -Eeuo pipefail
        export PATH="$FRESH/bin:$PATH"
        APP_LOCATIONS_DIR="$FRESH/etc/nginx/serverkit-locations"; DRY_RUN=0
        APP_BASELINE="$(snapshot_app_reachability)" \
            && phase "Verifying App Uptime" \
            && APP_AFTER="$(snapshot_app_reachability)" \
            && { report_app_uptime_regressions "$APP_BASELINE" "$APP_AFTER" || true; }
    } 2>&1
)" || t28_rc=$?
if [ "$t28_rc" -eq 0 ] && printf '%s' "$out" | grep -q 'No managed apps'; then
    ok "contract: the run block's uptime-verification chain exits 0 on a fresh zero-app box"
else
    bad "contract: uptime verification FAILED a fresh-box update (rc=$t28_rc): [$out]"
fi

printf 'location /app { proxy_pass http://127.0.0.1:8009; }\n' > "$t/loc/app.conf"
t28_rc=0
out="$(
    {
        set -Eeuo pipefail
        APP_LOCATIONS_DIR="$t/loc"; DRY_RUN=0
        export PATH="$t/binup:$PATH"       # app reachable pre-switch
        APP_BASELINE="$(snapshot_app_reachability)" \
            && export PATH="$t/bindown:$PATH" `# app dead post-switch → regression` \
            && phase "Verifying App Uptime" \
            && APP_AFTER="$(snapshot_app_reachability)" \
            && { report_app_uptime_regressions "$APP_BASELINE" "$APP_AFTER" || true; }
    } 2>&1
)" || t28_rc=$?
if [ "$t28_rc" -eq 0 ] && printf '%s' "$out" | grep -q 'DOWN now'; then
    ok "contract: a real app regression warns loudly but never fails the healthy update"
else
    bad "contract: regression path rc=$t28_rc (must be 0, with the loud warning): [$out]"
fi

# --------------------------------------------------------------------------
# T29 — refresh_bash_completion: regenerates the completion file from the
# switched-in CLI, honours DRY_RUN, and no-ops on boxes without a
# bash-completion directory. (SERVERKIT_COMPLETION_DIR is the test seam.)
# --------------------------------------------------------------------------
t="$WORK/t29"; mkdir -p "$t/comp" "$t/comp-dry"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
t29_rc=0
out="$(
    {
        set -Eeuo pipefail
        SERVERKIT_COMPLETION_DIR="$t/comp"; INSTALL_DIR="$REPO_ROOT"; DRY_RUN=0
        refresh_bash_completion
    } 2>&1
)" || t29_rc=$?
t29b_rc=0
out_dry="$(
    {
        set -Eeuo pipefail
        SERVERKIT_COMPLETION_DIR="$t/comp-dry"; INSTALL_DIR="$REPO_ROOT"; DRY_RUN=1
        refresh_bash_completion
    } 2>&1
)" || t29b_rc=$?
t29c_rc=0
(
    set -Eeuo pipefail
    SERVERKIT_COMPLETION_DIR="$t/absent"; INSTALL_DIR="$REPO_ROOT"; DRY_RUN=0
    refresh_bash_completion
) >/dev/null 2>&1 || t29c_rc=$?
if [ "$t29_rc" -eq 0 ] && grep -q 'complete -F _serverkit serverkit' "$t/comp/serverkit" \
   && [ "$t29b_rc" -eq 0 ] && printf '%s' "$out_dry" | grep -q 'dry-run' \
   && [ ! -f "$t/comp-dry/serverkit" ] \
   && [ "$t29c_rc" -eq 0 ]; then
    ok "refresh_bash_completion writes the file, honours dry-run, skips absent dirs"
else
    bad "refresh_bash_completion broken: rc=$t29_rc/$t29b_rc/$t29c_rc out=[$out] dry=[$out_dry]"
fi

# --------------------------------------------------------------------------
# T30 — retired plugins (the serverkit-workflows update-breaker of 1.7.25→):
# the carry-forward must NOT preserve a slug in RETIRED_PLUGIN_SLUGS (its dead
# imports would sink the bundle build in the new slot) while still carrying
# real user plugins; and remove_retired_plugins strips leftovers from an
# in-place tree (docker mode).
# --------------------------------------------------------------------------
t="$WORK/t30"
mkdir -p "$t/old/frontend/src/plugins/serverkit-workflows" \
         "$t/old/frontend/src/plugins/user-plugin" \
         "$t/new" \
         "$t/tree/frontend/src/plugins/serverkit-workflows" \
         "$t/tree/backend/app/plugins/serverkit-workflows"
printf 'import "reactflow";\n' > "$t/old/frontend/src/plugins/serverkit-workflows/index.jsx"
printf 'export default 1;\n'   > "$t/old/frontend/src/plugins/user-plugin/index.jsx"
if (
    set -Eeuo pipefail
    preserve_installed_plugins "$t/old" "$t/new" \
        && [ ! -e "$t/new/frontend/src/plugins/serverkit-workflows" ] \
        && [ -f "$t/new/frontend/src/plugins/user-plugin/index.jsx" ] \
        && remove_retired_plugins "$t/tree" \
        && [ ! -e "$t/tree/frontend/src/plugins/serverkit-workflows" ] \
        && [ ! -e "$t/tree/backend/app/plugins/serverkit-workflows" ]
) >/dev/null 2>&1; then
    ok "retired plugins are neither carried forward nor left in an in-place tree"
else
    bad "a retired plugin leaked into the new tree (the workflows update-breaker)"
fi

# --------------------------------------------------------------------------
# T31 — quarantine_carried_plugins: after a failed bundle build the carried
# frontends move aside (into BACKUP_DIR) so the build can be retried; with
# nothing carried it returns 1 so the caller halts with the original error.
# --------------------------------------------------------------------------
t="$WORK/t31"
mkdir -p "$t/new/frontend/src/plugins/broken-plugin"
printf 'import "gone";\n' > "$t/new/frontend/src/plugins/broken-plugin/index.jsx"
if (
    set -Eeuo pipefail
    BACKUP_DIR="$t/backups"
    CARRIED_FRONTEND_PLUGINS=" broken-plugin"
    quarantine_carried_plugins "$t/new" \
        && [ ! -e "$t/new/frontend/src/plugins/broken-plugin" ] \
        && ls "$t/backups"/quarantined-plugins-*/broken-plugin/index.jsx >/dev/null \
        && ! quarantine_carried_plugins "$t/new"
) >/dev/null 2>&1; then
    ok "quarantine_carried_plugins sets carried plugins aside; no-op when nothing carried"
else
    bad "quarantine_carried_plugins failed to move a carried plugin aside (or false-positived)"
fi

# --------------------------------------------------------------------------
# T32 — _node_build_ok gates on vite 8's Node floor (^20.19.0 || >=22.12.0) so a
# source-mode update on an old-Node box fails fast with an actionable message
# instead of a cryptic rolldown MODULE_NOT_FOUND mid-build (plan 47 / vite 8).
# --------------------------------------------------------------------------
t="$WORK/t32"; mkdir -p "$t/bin"
_mk_node_stub() { printf '#!/usr/bin/env bash\n[ "$1" = "--version" ] && echo "v%s"\nexit 0\n' "$1" > "$t/bin/node"; chmod +x "$t/bin/node"; }
node_gate_ok=1
for v in 20.19.0 22.12.0 24.1.0; do
    _mk_node_stub "$v"
    ( set -Eeuo pipefail; PATH="$t/bin:$PATH"; _node_build_ok ) || node_gate_ok=0
done
for v in 18.20.0 20.17.0 20.18.9 21.7.0 22.11.0; do
    _mk_node_stub "$v"
    ( set -Eeuo pipefail; PATH="$t/bin:$PATH"; _node_build_ok ) && node_gate_ok=0
done
if [ "$node_gate_ok" = 1 ]; then
    ok "_node_build_ok accepts 20.19+/22.12+ and rejects older (incl. 21.x, 22.11)"
else
    bad "_node_build_ok mis-gated a Node version against the vite 8 floor"
fi

# --------------------------------------------------------------------------
# T33 — build_frontend_bundle refuses (returns 1, never touches npm) on a
# too-old Node, so the caller rolls back instead of a half-built bundle.
# --------------------------------------------------------------------------
t="$WORK/t33"; mkdir -p "$t/bin" "$t/tree/frontend"
printf '#!/usr/bin/env bash\n[ "$1" = "--version" ] && echo "v20.17.0"\nexit 0\n' > "$t/bin/node"; chmod +x "$t/bin/node"
printf '#!/usr/bin/env bash\necho ran >> "%s/npmlog"\nexit 0\n' "$t" > "$t/bin/npm"; chmod +x "$t/bin/npm"
if (
    set -Eeuo pipefail
    PATH="$t/bin:$PATH"
    ! build_frontend_bundle "$t/tree"
) >/dev/null 2>&1 && [ ! -e "$t/npmlog" ]; then
    ok "build_frontend_bundle refuses on too-old Node without touching npm"
else
    bad "build_frontend_bundle ran npm (or returned 0) on a too-old Node"
fi

# --------------------------------------------------------------------------
# T34 — refresh_config renders the systemd unit with @BIND_HOST@ substituted
# (loopback), not left as a literal placeholder that would break gunicorn's bind
# after an update (parity with install.sh; the gap the operator caught).
# --------------------------------------------------------------------------
t="$WORK/t34"
mkdir -p "$t/nginx/sites-available" "$t/nginx/sites-enabled" \
         "$t/target/nginx/sites-available" "$t/target/templates" "$t/sysd"
printf 'http {\n}\n' > "$t/nginx/nginx.conf"
printf 'server { listen 80; }\n' > "$t/target/nginx/sites-available/serverkit-insecure.conf"
printf 'ExecStart=gunicorn -b @BIND_HOST@:@PORT@ run:app\n' \
    > "$t/target/templates/serverkit-backend.service.in"
(
    set -Eeuo pipefail
    NGINX_DIR="$t/nginx"; LETSENCRYPT_DIR="$t/le"; SYSTEMD_DIR="$t/sysd"; CONFIG_DIR="$t/cfg"
    INSTALL_DIR="$t/opt"; VENV_DIR="$t/venv"; LOG_DIR="$t/log"; DRY_RUN=0
    refresh_config "$t/target"
) >/dev/null 2>&1
if [ -f "$t/sysd/serverkit.service" ] \
   && ! grep -q '@BIND_HOST@' "$t/sysd/serverkit.service" \
   && grep -q '127.0.0.1:' "$t/sysd/serverkit.service"; then
    ok "refresh_config substitutes @BIND_HOST@ in the systemd unit (loopback bind)"
else
    bad "refresh_config left a literal @BIND_HOST@ (gunicorn would fail to bind after update)"
fi

# --------------------------------------------------------------------------
# T35 — updates default to release-mode (pre-built, Node-free) so the server
# never needs Node/npm; --source, --branch and BUILD_FROM_SOURCE=1 opt into an
# on-box source rebuild. Re-source update.sh with args and read the resolved
# (USE_RELEASE|TARGET_BRANCH) the deploy dispatch keys off (plan 47 / vite 8).
# --------------------------------------------------------------------------
_mode() { ( SERVERKIT_NO_SELF_UPDATE=1 source "$UPDATE_SH" "$@" >/dev/null 2>&1
            printf '%s|%s' "$USE_RELEASE" "$TARGET_BRANCH" ); }
d="$(_mode)"; s="$(_mode --source)"; b="$(_mode --branch dev)"; r="$(_mode --release)"
bfs="$(BUILD_FROM_SOURCE=1 _mode)"
if [ "$d" = "1|" ] && [ "$s" = "0|" ] && [ "$b" = "0|dev" ] \
   && [ "$r" = "1|" ] && [ "$bfs" = "0|" ]; then
    ok "update defaults to release (Node-free); --source/--branch/BUILD_FROM_SOURCE opt into on-box rebuild"
else
    bad "update mode mapping wrong: default=$d source=$s branch=$b release=$r build_from_source=$bfs"
fi

# --------------------------------------------------------------------------
# Install profiles: the minimal profile ships without Docker on purpose, so
# preflight must not demand it — otherwise every minimal box is installable but
# permanently un-updatable.
# --------------------------------------------------------------------------
pt="$WORK/profile"; mkdir -p "$pt/slot"
mkenv() { printf 'DATABASE_URL=sqlite:///x\n%s\n' "$1" > "$pt/slot/.env"; }

read_profile() {  # read_profile — echo install_profile() under a fixture slot
    ( set -Eeuo pipefail
      INSTALL_DIR="$pt/slot"; DIR_A="$pt/slot"; DIR_B="$pt/other"
      install_profile )
}

mkenv 'SERVERKIT_PROFILE=minimal'
if [ "$(read_profile)" = "minimal" ]; then
    ok "install_profile reads SERVERKIT_PROFILE from the active slot's .env"
else
    bad "install_profile misread a minimal .env: got [$(read_profile)]"
fi

mkenv 'SERVERKIT_PROFILE=banana'
if [ "$(read_profile)" = "standard" ]; then
    ok "install_profile falls back to standard on a bogus value"
else
    bad "install_profile accepted a bogus profile: got [$(read_profile)]"
fi

# Every install predating profiles has no such key and is effectively standard.
mkenv 'PORT=80'
if [ "$(read_profile)" = "standard" ]; then
    ok "install_profile treats a pre-profile .env as standard"
else
    bad "install_profile mishandled a pre-profile .env: got [$(read_profile)]"
fi

# No .env at all (fresh base dir) must not abort under pipefail.
if res="$( set -Eeuo pipefail; INSTALL_DIR="$pt/missing"; DIR_A="$pt/missing"
           DIR_B="$pt/other"; install_profile )"; then
    ok "install_profile survives a missing .env (got [$res])"
else
    bad "install_profile aborted with no .env under set -Eeuo pipefail"
fi

# The actual regression: minimal + no docker binary → Docker is not required.
# have_docker() is the seam; hiding docker by trimming PATH would also hide the
# coreutils install_profile parses with, and the assertion would then pass for
# the wrong reason.
mkenv 'SERVERKIT_PROFILE=minimal'
if ( set -Eeuo pipefail
     INSTALL_DIR="$pt/slot"; DIR_A="$pt/slot"; DIR_B="$pt/other"
     have_docker() { return 1; }
     update_needs_docker ); then
    bad "update_needs_docker still demands Docker on a minimal Dockerless box (update would halt)"
else
    ok "update_needs_docker: minimal + no docker → Docker not required"
fi

# ...but if the operator later installed Docker, keep validating it.
if ( set -Eeuo pipefail
     INSTALL_DIR="$pt/slot"; DIR_A="$pt/slot"; DIR_B="$pt/other"
     have_docker() { return 0; }
     update_needs_docker ); then
    ok "update_needs_docker: minimal + docker present → still validated"
else
    bad "update_needs_docker skipped a Docker that is actually installed"
fi

# A standard install without Docker is real drift and must still halt.
mkenv 'SERVERKIT_PROFILE=standard'
if ( set -Eeuo pipefail
     INSTALL_DIR="$pt/slot"; DIR_A="$pt/slot"; DIR_B="$pt/other"
     have_docker() { return 1; }
     update_needs_docker ); then
    ok "update_needs_docker: standard profile still requires Docker"
else
    bad "update_needs_docker let a standard install skip Docker"
fi

# An all-Docker deployment needs Docker no matter what the profile says.
mkenv 'SERVERKIT_PROFILE=minimal'
if ( set -Eeuo pipefail
     INSTALL_DIR="$pt/slot"; DIR_A="$pt/slot"; DIR_B="$pt/other"
     have_docker() { return 1; }
     is_docker_deployment() { return 0; }
     update_needs_docker ); then
    ok "update_needs_docker: all-Docker deployment overrides the minimal profile"
else
    bad "update_needs_docker skipped Docker for an all-Docker deployment"
fi

# --------------------------------------------------------------------------
# T10i — retention runs BEFORE the new backup is written, not only in
# cleanup(). cleanup() is the LAST phase, so it never ran on an update that
# died earlier — including one that died because the disk was full. A box was
# found carrying six snapshots under a cap of five for exactly that reason.
# --------------------------------------------------------------------------
t="$WORK/t10i"; mkdir -p "$t/backups"
for i in 1 2 3 4 5; do
    mkdir -p "$t/backups/serverkit-tree-2026010$i"
    : > "$t/backups/serverkit-pre-upgrade-2026010$i-00000$i.db"
    touch -d "2026-01-0$i 00:00" "$t/backups/serverkit-tree-2026010$i" \
        "$t/backups/serverkit-pre-upgrade-2026010$i-00000$i.db" 2>/dev/null || true
done
pre_rc=0
(
    set -Eeuo pipefail
    DRY_RUN=0 BACKUP_DIR="$t/backups" SERVERKIT_BACKUP_RETENTION=2
    trim_backups
) >/dev/null 2>&1 || pre_rc=$?
pre_trees=$(find "$t/backups" -mindepth 1 -maxdepth 1 -name 'serverkit-tree-*' | wc -l)
pre_dbs=$(find "$t/backups" -mindepth 1 -maxdepth 1 -name '*.db' | wc -l)
if [ "$pre_rc" -eq 0 ] && [ "$pre_trees" -eq 2 ] && [ "$pre_dbs" -eq 2 ]; then
    ok "trim_backups caps every kind and is callable before the backup is written"
else
    bad "trim_backups: rc=$pre_rc trees=$pre_trees dbs=$pre_dbs (want 2/2)"
fi

if grep -A16 '^backup_current()' "$UPDATE_SH" | grep -q 'trim_backups'; then
    ok "backup_current trims BEFORE writing the new snapshot (survives a failed run)"
else
    bad "backup_current does not trim before writing — a failed update leaves backups over cap"
fi

# --------------------------------------------------------------------------
# T10j — the size budget drops oldest-first until backups fit their share of
# the disk. A count cap alone is not a disk guarantee: five snapshots of an
# 800 MB database is 8 GB, a third of a 25 GB droplet.
# --------------------------------------------------------------------------
t="$WORK/t10j"; mkdir -p "$t/backups"
for i in 1 2 3; do
    dd if=/dev/zero of="$t/backups/serverkit-pre-upgrade-2026010$i-00000$i.db" \
        bs=1024 count=200 2>/dev/null
    touch -d "2026-01-0$i 00:00" "$t/backups/serverkit-pre-upgrade-2026010$i-00000$i.db" 2>/dev/null || true
done
bud_rc=0
(
    set -Eeuo pipefail
    DRY_RUN=0 BACKUP_DIR="$t/backups"
    SERVERKIT_BACKUP_MAX_PERCENT=0
    enforce_backup_budget
) >/dev/null 2>&1 || bud_rc=$?
left=$(find "$t/backups" -mindepth 1 -maxdepth 1 -name 'serverkit-pre-upgrade-*' | wc -l)
newest_left=$(find "$t/backups" -name 'serverkit-pre-upgrade-20260103-*' | wc -l)
if [ "$bud_rc" -eq 0 ] && [ "$left" -eq 1 ] && [ "$newest_left" -eq 1 ]; then
    ok "enforce_backup_budget trims oldest-first and always keeps one restore point"
else
    bad "budget: rc=$bud_rc left=$left newest_kept=$newest_left (want 1/1)"
fi

t="$WORK/t10k"; mkdir -p "$t/backups"
: > "$t/backups/serverkit-pre-upgrade-20260101-000001.db"
: > "$t/backups/serverkit-pre-upgrade-20260102-000002.db"
(
    set -Eeuo pipefail
    DRY_RUN=0 BACKUP_DIR="$t/backups" SERVERKIT_BACKUP_MAX_PERCENT=99
    enforce_backup_budget
) >/dev/null 2>&1 || true
if [ "$(find "$t/backups" -name '*.db' | wc -l)" -eq 2 ]; then
    ok "enforce_backup_budget is a no-op when backups already fit the budget"
else
    bad "enforce_backup_budget deleted backups that were within budget"
fi

# --------------------------------------------------------------------------
# T10l — the disk preflight sizes its requirement to what the update will
# actually write (2 GiB + TWO copies of the database), not a flat 2 GiB. With
# an 800 MB DB the old check passed at 2.1 GiB free and then filled the disk.
# --------------------------------------------------------------------------
if grep -q 'db_kb \* 2' "$UPDATE_SH"; then
    ok "preflight sizes the disk requirement to two database copies"
else
    bad "preflight still uses a flat disk floor — an 800 MB DB overruns it"
fi
if grep -q 'trimming old backups' "$UPDATE_SH"; then
    ok "preflight reclaims stale snapshots before halting on low disk"
else
    bad "preflight halts on low disk without first trimming reclaimable backups"
fi

# --------------------------------------------------------------------------
if (
    set -Eeuo pipefail
    source_tree="$WORK/snapshot-source"
    target_tree="$WORK/snapshot-target"
    mkdir -p "$source_tree/backend/instance" "$source_tree/backend/venv" "$source_tree/node_modules"
    printf 'code' > "$source_tree/app.py"
    printf 'settings' > "$source_tree/backend/instance/settings.json"
    touch "$source_tree/backend/instance/serverkit.db" \
        "$source_tree/backend/instance/serverkit.db-wal" \
        "$source_tree/backend/instance/serverkit.db-shm" \
        "$source_tree/backend/venv/dependency" "$source_tree/node_modules/dependency"
    rsync() { return 127; }
    snapshot_install_tree "$source_tree" "$target_tree"
    [ "$(cat "$target_tree/app.py")" = code ]
    [ "$(cat "$target_tree/backend/instance/settings.json")" = settings ]
    [ ! -e "$target_tree/backend/instance/serverkit.db" ]
    [ ! -e "$target_tree/backend/instance/serverkit.db-wal" ]
    [ ! -e "$target_tree/backend/instance/serverkit.db-shm" ]
    [ ! -e "$target_tree/backend/venv" ]
    [ ! -e "$target_tree/node_modules" ]
); then
    ok "tree snapshot fallback preserves code and excludes database copies and dependencies"
else
    bad "tree snapshot fallback loses files or duplicates excluded data"
fi

printf '\n%d passed, %d failed, %d skipped\n\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
