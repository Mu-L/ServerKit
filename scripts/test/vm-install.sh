#!/bin/bash
# Runs inside the test VM.
# Strategy:
#   1. Run install.sh from the uploaded local source (so it bootstraps deps).
#   2. After install, replace /opt/serverkit with the uploaded local working
#      tree (overlay), rebuild frontend, reinstall Python deps, restart.
#   3. Probe health endpoint and exit non-zero if anything failed.
#
# This way we test the EXACT local code, including uncommitted changes,
# instead of whatever is on origin/main.
set -u  # NB: not -e — we want to capture failures and still emit a report

SRC=/opt/serverkit-src
INSTALL_DIR=/opt/serverkit
LOG=/var/log/serverkit-test-install.log

log() { echo "[vm-install] $*" | tee -a "$LOG"; }
fail() { echo "[vm-install] FAIL: $*" | tee -a "$LOG"; exit 1; }

mkdir -p "$(dirname "$LOG")"
: > "$LOG"

# Capture sshd/firewall/network state at named checkpoints so post-mortems
# work even when install.sh leaves SSH unreachable (Rocky 9 has done this).
# Output goes to $LOG which is later copied back to the host by the harness.
snapshot() {
  local label="$1"
  {
    echo
    echo "===== SNAPSHOT: $label ($(date -Iseconds)) ====="
    echo "--- systemctl status sshd ---"
    systemctl status sshd --no-pager 2>&1 | head -30
    echo "--- ss -tlnp (listeners) ---"
    ss -tlnp 2>&1 | head -30
    echo "--- firewall-cmd state ---"
    if command -v firewall-cmd >/dev/null 2>&1; then
      systemctl is-active firewalld 2>&1
      firewall-cmd --state 2>&1
      firewall-cmd --get-default-zone 2>&1
      firewall-cmd --list-all 2>&1
    else
      echo "firewall-cmd not present"
    fi
    echo "--- iptables -L INPUT ---"
    iptables -L INPUT -n 2>&1 | head -40 || true
    echo "--- ip addr ---"
    ip -4 addr 2>&1
    echo "===== END SNAPSHOT: $label ====="
    echo
  } >> "$LOG" 2>&1
}

# Snapshot system state on every exit path. Snapshot only — we deliberately
# do NOT `systemctl restart sshd` here. On Rocky 9 the running sshd is
# linked against an older libssl than what `dnf` ends up with on disk, so
# a restart spawns a fresh sshd that segfaults on OpenSSL version mismatch
# and the host can't reconnect for the pytest phase. The running sshd
# parent keeps working with its mapped-in libssl, so leaving it alone is
# the right move.
cleanup() {
  local rc=$?
  echo "[vm-install] cleanup: exit rc=$rc" >> "$LOG" 2>&1
  snapshot "cleanup (rc=$rc)"
  return $rc
}
trap cleanup EXIT

[ -d "$SRC" ] || fail "source dir $SRC missing — multipass transfer broken"
[ -f "$SRC/install.sh" ] || fail "install.sh not found in source"

snapshot "pre-install"

log "Step 1/4: running install.sh (this clones origin/main, installs deps)"
# install.sh clones from GitHub — we let it, then overlay our local code.
bash "$SRC/install.sh" >> "$LOG" 2>&1
INSTALL_RC=$?
log "install.sh exit=$INSTALL_RC"
snapshot "post-install.sh"

if [ ! -d "$INSTALL_DIR" ]; then
  fail "install.sh did not create $INSTALL_DIR (rc=$INSTALL_RC)"
fi

log "Step 2/4: overlaying local working tree onto $INSTALL_DIR"
# --delete: the install base (release tarball or origin/main clone) can
# contain files our local tree doesn't have — e.g. release v1.7.40 shipped
# frontend/src/plugins/serverkit-tramo, which survived a plain overlay and
# broke `vite build` with an unresolvable "tramo/react" import. Mirror the
# tree exactly instead. Excluded paths are protected from deletion by rsync,
# so .env / instance / ssl / node_modules and the install-time root venv
# ($INSTALL_DIR/venv — NOT backend/venv) survive.
rsync -a --delete \
  --exclude='.env' \
  --exclude='backend/instance/' \
  --exclude='nginx/ssl/' \
  --exclude='venv/' \
  --exclude='logs/' \
  --exclude='backend/venv/' \
  --exclude='backend/.venv/' \
  --exclude='backend/.venv-wsl/' \
  --exclude='frontend/node_modules/' \
  --exclude='frontend/dist/' \
  --exclude='.git/' \
  "$SRC/" "$INSTALL_DIR/" >> "$LOG" 2>&1 || fail "rsync overlay failed"

log "Step 3/4: rebuild + restart with local code"
cd "$INSTALL_DIR" || fail "cd $INSTALL_DIR"

# Reinstall Python deps in case requirements.txt changed
if [ -d "$INSTALL_DIR/venv" ]; then
  "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/backend/requirements.txt" >> "$LOG" 2>&1 \
    || log "WARN: pip install had non-zero exit"
fi

# Reinstall + rebuild frontend (package.json may have changed in the overlay)
cd "$INSTALL_DIR/frontend" || fail "cd frontend"

# The release install path skips Node.js entirely ("release ships a pre-built
# frontend"), but the overlay rebuild below needs npm. Bootstrap Node 22 the
# same way install.sh does when it's missing.
if ! command -v npm >/dev/null 2>&1; then
  log "npm not found (release install skips Node.js) — installing Node 22 for the overlay rebuild"
  if command -v apt-get >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >> "$LOG" 2>&1 || true
    apt-get install -y nodejs >> "$LOG" 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    curl -fsSL https://rpm.nodesource.com/setup_22.x | bash - >> "$LOG" 2>&1 || true
    dnf install -y nodejs >> "$LOG" 2>&1 || true
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install nodejs22 npm22 >> "$LOG" 2>&1 || \
      zypper --non-interactive install nodejs npm >> "$LOG" 2>&1 || true
  fi
fi
command -v npm >/dev/null 2>&1 || fail "npm unavailable and Node.js bootstrap failed"

npm ci --prefer-offline >> "$LOG" 2>&1 || fail "npm ci failed"
NODE_OPTIONS="--max-old-space-size=1024" npm run build >> "$LOG" 2>&1 \
  || fail "frontend build failed"

cd "$INSTALL_DIR" || true
docker compose build >> "$LOG" 2>&1 || log "WARN: docker compose build had non-zero exit"
docker compose up -d >> "$LOG" 2>&1 || log "WARN: docker compose up had non-zero exit"

systemctl restart serverkit >> "$LOG" 2>&1 || fail "systemctl restart serverkit failed"
systemctl restart nginx >> "$LOG" 2>&1 || log "WARN: nginx restart had non-zero exit"

log "Step 4/4: waiting for health endpoint"
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:5000/api/v1/system/health > /dev/null 2>&1; then
    log "Backend healthy after ${i}s"
    log "Measuring post-install runtime (60s settle + 120s observation)"
    "$INSTALL_DIR/venv/bin/python" "$SRC/scripts/test/measure-installed-runtime.py" \
      --source "$INSTALL_DIR" --source-archive /tmp/serverkit-src.tar.gz \
      --output /tmp/serverkit-runtime.json >> "$LOG" 2>&1 \
      || log "WARN: runtime baseline unavailable/failed; inspect runtime report"
    echo "OK" > /tmp/serverkit-install-status
    exit 0
  fi
  sleep 1
done

log "Backend never became healthy"
systemctl status serverkit --no-pager >> "$LOG" 2>&1 || true
journalctl -u serverkit --no-pager -n 100 >> "$LOG" 2>&1 || true
echo "FAIL" > /tmp/serverkit-install-status
exit 1
