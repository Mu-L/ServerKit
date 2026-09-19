# ServerKit E2E Test Harness

One-click full-stack test that spins up fresh Linux VMs on your Windows box,
installs ServerKit from your **local working tree** (uncommitted changes
included), and runs an API harness against the live panel. Aggregates
everything into a single HTML report.

Designed for the "I'm about to merge a huge PR and want to be sure the
installer actually works on multiple distros" use case.

## Prerequisites (one-time)

1. **Multipass** — Ubuntu 22.04 + 24.04 VMs
   ```powershell
   winget install Canonical.Multipass
   ```
2. **Vagrant** — Debian 12, Fedora, Rocky 9 VMs (uses Hyper-V provider, no VirtualBox)
   ```powershell
   winget install Hashicorp.Vagrant
   ```
   **Note:** Vagrant + Hyper-V requires running the script from an
   **elevated** PowerShell (right-click → Run as Administrator). If you
   run as a normal user, the script will tell you and exit cleanly.
   Multipass-only runs (Ubuntu) don't need admin.
3. **Python 3** on PATH (for the HTML report generator; tests run inside the VMs).
4. **Hyper-V** enabled (both Multipass and Vagrant use it on Windows).

You can install only Multipass if Ubuntu coverage is enough — the harness
auto-detects which backends are available and skips distros it can't run.

## Usage

From the repo root, in PowerShell:

```powershell
.\scripts\test\full-stack-test.ps1
```

That's it. Goes and makes coffee. ~1-2 hours on first run (cloud images
download), 15-30 min after that. When it finishes, your browser opens to the
HTML report.

### Options

```powershell
# Full default suite: ubuntu22, ubuntu24, debian12, rocky9
.\scripts\test\full-stack-test.ps1

# Subset
.\scripts\test\full-stack-test.ps1 -Only "ubuntu24,debian12"

# Opt-in extras (fedora40 box is community-pinned; alma9/debian11/opensuse15
# widen homelab coverage)
.\scripts\test\full-stack-test.ps1 -Only "fedora,alma9,debian11,opensuse15"

# Keep VMs running so you can shell in and poke around
.\scripts\test\full-stack-test.ps1 -Keep

# Bigger VMs (default 2 CPU / 4 GB RAM / 15 GB disk)
.\scripts\test\full-stack-test.ps1 -Cpus 4 -MemoryGB 8

# Re-run harness against an already-installed Multipass VM (fast iteration)
.\scripts\test\full-stack-test.ps1 -ReuseVm sk-test-ubuntu24-<id>
```

### Distro coverage

| Distro | Backend | Box / Image | In default suite? |
|---|---|---|---|
| Ubuntu 22.04 | Multipass | `22.04` | ✅ |
| Ubuntu 24.04 | Multipass | `24.04` | ✅ |
| Debian 12    | Vagrant   | `generic/debian12` | ✅ |
| Rocky 9      | Vagrant   | `generic/rocky9` | ✅ |
| Fedora 40    | Vagrant   | `generic/fedora40` | opt-in (`-Only fedora`) |
| AlmaLinux 9  | Vagrant   | `generic/alma9` | opt-in |
| Debian 11    | Vagrant   | `generic/debian11` | opt-in (legacy homelab) |
| openSUSE Leap 15 | Vagrant | `generic/opensuse15` | opt-in |

### Agent pairing test (optional)

After running with `-Keep`:

```powershell
.\scripts\test\agent-test.ps1
```

Exercises the agent <-> panel pairing API end-to-end (enroll → claim).

## What it does

1. Tar your local repo (excluding `.git`, `node_modules`, venvs, `dist/`).
2. `multipass launch` three VMs in parallel: Ubuntu 22.04, Ubuntu 24.04, Debian 12.
3. On each VM, in parallel:
   - Upload the tarball + `vm-install.sh`.
   - Run `install.sh` (real public installer — clones from GitHub, installs
     Python, Node, Docker, nginx, builds frontend, starts systemd unit).
   - Overlay your local working tree on top of `/opt/serverkit`, rebuild
     frontend, restart `serverkit` systemd unit. This is what makes us test
     **your code**, not what's on `main`.
   - Wait for `/api/v1/system/health` to return 200.
   - Push the pytest harness and run it against the live panel.
   - Capture install log + journalctl regardless of outcome.
4. Generate one self-contained `report.html` with green/red per VM, per test,
   plus full logs inline.
5. Tear down VMs (unless `-Keep`).

## Output

```
scripts/test/output/<run-id>/
  report.html                     <- open this
  serverkit-src.tar.gz
  sk-test-ubuntu22-<id>/
    install.log
    vm-install.log
    journalctl.log
    install-status                ("OK" or "FAIL")
    pytest.log
    pytest-report.json
    runtime.json                   (post-install resource observation)
  sk-test-ubuntu24-<id>/ ...
  sk-test-debian12-<id>/ ...
```

## Update & uninstall coverage

Full VM installs now wait 60 seconds after the health check, then collect a
120-second passive runtime baseline before pytest. The HTML report shows median
and sampled-peak cgroup memory, and median/p95 CPU per measured service. Raw data
is in `runtime.json`; unavailable/failed measurements are shown explicitly and
do not turn a successful install into a failed install. Test Sandbox full mode
uses the same sampler and saves `<distro>.runtime.json` beside its log.

These short observations help identify regressions, but parallel VM/container
work can affect them. Quick/provision checks do not run the panel and cannot
measure its runtime overhead. See [measurement definitions and longer baseline
commands](../../docs/METRICS.md#passive-linux-runtime-sampler).

Alongside `vm-install.sh`, two more "run inside the VM" scripts exercise the
other lifecycle stages end-to-end (same contract: log to
`/var/log/serverkit-test-*.log`, write `OK`/`FAIL` to `/tmp/serverkit-*-status`):

- **`vm-update.sh`** — installs ServerKit, runs the local `scripts/update.sh`
  with `--force` (full blue/green deploy → migrate → switch → health), then
  re-runs without `--force` to confirm the "Already up to date" version-gate.
- **`vm-uninstall.sh`** — installs + seeds sample data, runs the default
  uninstall and asserts user data is preserved (DB snapshot in
  `/var/backups/serverkit`, `/var/lib/serverkit` survives), then reinstalls and
  asserts `--purge` removes the data dirs.

Run them by uploading to a `-Keep` VM and `sudo bash /tmp/vm-update.sh`, or wire
them into the orchestrator the same way `vm-install.sh` is invoked.

## Fast, serverless unit tests

Most regressions are caught long before a VM is needed. The source-able
`scripts/test/test_{update,install,lib}.sh` suites exercise the updater,
installer, and the `scripts/lib/*` abstractions against fixtures and PATH stubs
— no server, seconds to run, and gated in CI (`.github/workflows/scripts-ci.yml`)
across Ubuntu 22.04/24.04, Debian 12, Rocky 9, AlmaLinux 9, Fedora 40, and
openSUSE Leap 15.5 containers.

```bash
bash scripts/test/test_update.sh
bash scripts/test/test_install.sh
bash scripts/test/test_lib.sh
```

## All-25 distro compatibility discovery (Windows-friendly)

`distro-compatibility.py` turns the published 25-target list into a repeatable
evidence report. It runs directly from Windows against Docker Desktop, so it
does not modify the installed WSL distributions. The shared
`backend/app/data/distro-catalog.json` records exact images, vendor userland proxies, base-only
proxies, and targets that require a separate VM/hardware harness.

```powershell
# Validate and list all 25 targets without running Docker
python .\scripts\test\distro-compatibility.py --list

# Run source-level suites plus real Python provisioning across every image
python .\scripts\test\distro-compatibility.py --mode both --jobs 4

# A smaller investigation or a CI-gating run
python .\scripts\test\distro-compatibility.py --only ubuntu22,debian13,rocky10
python .\scripts\test\distro-compatibility.py --mode provision --strict
```

Each run writes `report.md`, `report.json`, image-pull logs, and one log per
probe/mode under `scripts/test/output/distro-compatibility/<timestamp>/`.
Failures remain failures by default, but discovery runs exit zero so one known
incompatibility does not erase the rest of the matrix. Pass `--strict` when the
selected matrix is intended to gate CI.

The two modes deliberately make narrower claims than a full install:

- **quick** — shell syntax plus the source-level installer/update/lib/CLI suites.
- **provision** — real repositories through `install.sh`, including supported
  Python, venv, pip, SSL, SQLite, and ctypes.

Neither mode proves systemd, nginx, firewall behavior, Docker-in-Docker,
kernel-specific behavior, LXC nesting, a hypervisor, or Raspberry Pi hardware.
Use the Windows VM harness above and the per-target path in the generated
report for that evidence.

## Probe environment matrix (plan 75 §D)

`scripts/test/probe-matrix.sh` runs `backend/tests/test_probe_env_integration.py`
— mock-free probe-honesty tests (firewall detect/status paths only, no
provisioning) — in docker containers shaped like the install environments the
plan 74 sbin fix could not verify on its single Ubuntu 22.04 root box. One
line on why each leg matters:

- **ubuntu-root-sbinless** — the plan 74 shape itself: pytest runs with
  `PATH=/usr/local/bin:/usr/bin:/bin` (the panel unit's PATH), proving
  detection never reports "not installed" because $PATH lacked sbin.
- **ubuntu-nonroot-sudo** — non-root install: `_needs_sudo()` is True and
  probes route through sudo's secure_path, so the sbin bug can't reproduce;
  a root-only verification proves nothing about it.
- **rocky-root** — RHEL family: firewalld instead of ufw, rpm/dnf instead of
  dpkg/apt, usrmerge paths (firewall-cmd lives in /usr/bin).
- **ubuntu-capdrop** — unprivileged-LXC shape: `--cap-drop=NET_ADMIN`. ufw
  exists and resolves, but `ufw status` fails on permissions — the failure
  must surface as error/unknown, never a silent "not installed" or a
  fabricated "inactive".

```bash
scripts/test/probe-matrix.sh            # all four legs, summary table
scripts/test/probe-matrix.sh rocky      # single leg (substring match)
scripts/test/probe-matrix.sh --list     # leg names
```

The repo is mounted read-only; each leg installs python3+pytest+the firewall
package and exits non-zero if any leg fails. The tests auto-detect their
environment and skip with explicit reasons when an invariant doesn't hold, so
the same file also runs cleanly on a dev box (WSL included).

## Extending the harness

Add new tests under `harness/test_*.py` — they're plain pytest using a
session-scoped `admin_token` fixture and a `base_url` fixture. The orchestrator
auto-copies every file in `harness/` to each VM, so just dropping in a new
`test_05_whatever.py` is enough.

Currently covered:
- `test_01_health.py` — backend health, frontend reachable
- `test_02_auth.py` — setup-status, register, login, JWT-authed request
- `test_03_plugins.py` — list plugins (install-from-URL test is `@skipif`'d
  until a stable test plugin repo exists; flip when ready)
- `test_04_smoke.py` — sample of authed endpoints must not return 5xx

## Limitations

- Doesn't test bare-metal-only stuff (hardware drivers, real partitioning).
- Agent ARM64 MSI installer isn't exercised (no ARM hardware).
- UI smoke (Playwright) is not wired up yet — easy to add as a follow-up if
  the API surface stops catching everything.
- Fedora isn't in the default distro list (the generic/fedora40 Hyper-V box
  is community-pinned); opt in with `-Only fedora`. alma9 / debian11 /
  opensuse15 are also opt-in extras.
- ARM (Raspberry Pi etc.) can't be virtualized here — Hyper-V on Windows is
  x86_64 only. ARM64 coverage comes from the CI container matrix on the
  `ubuntu-24.04-arm` runner (see `.github/workflows/scripts-ci.yml`), which
  approximates 64-bit Raspberry Pi OS / Ubuntu ARM homelabs; real-hardware
  validation still needs a physical Pi.

## Troubleshooting

**"multipass: command not found"** — install Multipass and restart PowerShell.

**Launch fails with timeout** — first launch downloads ~600 MB per distro
image; let it run. If it times out repeatedly, increase Multipass timeout:
`multipass set local.driver=hyperv`.

**Health check never passes** — open `report.html`, expand "Install log" and
"journalctl" for that VM. Most common: missing system package on a distro the
installer doesn't handle, or `frontend build` OOM (bump `-MemoryGB`).

**Want to debug a failing VM** — re-run with `-Keep`, then:
```powershell
multipass shell sk-test-ubuntu24-<id>
sudo journalctl -u serverkit -f
```
