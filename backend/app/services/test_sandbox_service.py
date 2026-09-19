"""Test Sandbox service — runs ServerKit's installer / script test suites
across Linux distro Docker containers, driven from the Test Sandbox UI.

Two modes per distro:

- ``quick`` — plain distro container running ``bash -n`` over every shipped
  shell script plus the source-able unit suites (test_update / test_install /
  test_lib / test_cli / test_agent_install / test_stage). Mirrors the
  scripts-ci cross-distro matrix; ~1-2 min per distro.
- ``full`` — privileged systemd container (geerlingguy images) running the
  real ``install.sh`` end-to-end, then probing /api/v1/system/health.
  ~5-15 min per distro.

Distro containers are named ``sk-sandbox-<run_id>-<distro>`` so a cancel (or a
stale-run sweep) can find them. Per-distro logs live under
``<instance>/sandbox-runs/<run_id>/<distro>.log``.

This is a Docker-based complement to the Multipass/Vagrant E2E harness in
scripts/test/ — it trades VM fidelity (real cloud images, Hyper-V) for
convenience (runs on any host where the panel + Docker already are).
"""
import json
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import current_app

from app import db
from app.utils.system import run_checked
from app.models.sandbox_run import SandboxRun

# backend/app/services/test_sandbox_service.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

QUICK_TIMEOUT_S = 900    # 15 min ceiling for the unit suites
FULL_TIMEOUT_S = 2400    # 40 min ceiling for a full install + health probe

CATALOG_PATH = Path(__file__).resolve().parents[1] / 'data' / 'distro-catalog.json'


def _load_distros():
    """Flatten the shared 25-target catalog for the Test Sandbox picker."""
    catalog = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
    distros = {}
    for target in catalog['targets']:
        for probe in target.get('probes', []):
            distros[probe['key']] = {
                'label': probe['label'],
                'family': target['family'],
                'target': target['id'],
                'fidelity': probe['fidelity'],
                'quick_image': probe['image'],
                'full_image': probe.get('full_image'),
            }
    # Keep explicit legacy probes runnable without counting them among the
    # published version claims in the compatibility report.
    for probe in catalog.get('legacy_probes', []):
        distros[probe['key']] = {
            'label': probe['label'],
            'family': probe['family'],
            'target': probe['target_id'],
            'fidelity': probe['fidelity'],
            'quick_image': probe['image'],
            'full_image': probe.get('full_image'),
        }
    return distros


# quick_image: plain distro container (unit suites).
# full_image: systemd-capable container for a real install (None = unavailable).
DISTROS = _load_distros()

# Mirrors the cross-distro job in .github/workflows/scripts-ci.yml.
QUICK_RUNNER = r"""#!/bin/sh
set -u
cd /src || exit 2
if ! command -v bash >/dev/null 2>&1; then
  { command -v apt-get >/dev/null 2>&1 && apt-get update -qq && apt-get install -y -qq bash; } ||
  { command -v dnf     >/dev/null 2>&1 && dnf install -y -q bash; } ||
  { command -v yum     >/dev/null 2>&1 && yum install -y -q bash; } ||
  { command -v zypper  >/dev/null 2>&1 && zypper --non-interactive install bash; } ||
  { command -v pacman  >/dev/null 2>&1 && pacman -Sy --noconfirm bash; } ||
  { command -v apk     >/dev/null 2>&1 && apk add --no-cache bash; } ||
  { command -v emerge  >/dev/null 2>&1 && emerge --oneshot app-shells/bash; } || true
fi
echo "bash $(bash --version | head -1)"
command -v bash >/dev/null 2>&1 || exit 127
exec bash scripts/test/run-distro-quick.sh
"""

# run_id -> list of container names (for cancel / stale sweeps)
_active_containers = {}
_active_lock = threading.Lock()


class TestSandboxService:
    # ------------------------------------------------------------------ API
    @classmethod
    def list_distros(cls):
        return [
            {
                'key': key,
                'label': spec['label'],
                'family': spec['family'],
                'target': spec['target'],
                'fidelity': spec['fidelity'],
                'quick': bool(spec['quick_image']),
                'full': bool(spec['full_image']),
            }
            for key, spec in DISTROS.items()
        ]

    @classmethod
    def docker_available(cls):
        try:
            return run_checked(['docker', 'info', '--format', '{{.ServerVersion}}'],
                               timeout=15)['success']
        except Exception:  # noqa: BLE001 - availability probe; absence is the answer
            return False

    @classmethod
    def start_run(cls, distros, mode, user_id=None):
        if mode not in ('quick', 'full'):
            raise ValueError("mode must be 'quick' or 'full'")
        if not distros:
            raise ValueError('pick at least one distro')
        unknown = [d for d in distros if d not in DISTROS]
        if unknown:
            raise ValueError(f"unknown distro(s): {', '.join(unknown)}")
        if mode == 'full':
            unsupported = [d for d in distros if not DISTROS[d]['full_image']]
            if unsupported:
                raise ValueError(
                    'full install not supported for: ' + ', '.join(unsupported))
        if not cls.docker_available():
            raise RuntimeError('Docker is not available on this host')
        running = SandboxRun.query.filter_by(status='running').count()
        if running:
            raise RuntimeError('a sandbox run is already in progress')

        run = SandboxRun(
            mode=mode,
            distros=list(distros),
            status='running',
            results={d: {'status': 'queued', 'detail': '', 'duration_s': None}
                     for d in distros},
            user_id=user_id,
        )
        db.session.add(run)
        db.session.commit()

        app = current_app._get_current_object()
        thread = threading.Thread(
            target=cls._execute, args=(app, run.id), daemon=True,
            name=f'sandbox-run-{run.id}')
        thread.start()
        return run

    @classmethod
    def list_runs(cls, limit=20):
        runs = (SandboxRun.query.order_by(SandboxRun.id.desc())
                .limit(limit).all())
        return [r.to_dict() for r in runs]

    @classmethod
    def get_run(cls, run_id):
        return SandboxRun.query.get(run_id)

    @classmethod
    def cancel_run(cls, run_id):
        run = SandboxRun.query.get(run_id)
        if not run or run.status != 'running':
            return False
        with _active_lock:
            names = list(_active_containers.get(run_id, []))
        for name in names:
            run_checked(['docker', 'rm', '-f', name], timeout=30)
        run.status = 'cancelled'
        run.finished_at = datetime.utcnow()
        db.session.commit()
        return True

    @classmethod
    def get_log(cls, run_id, distro):
        run = SandboxRun.query.get(run_id)
        if not run or distro not in (run.distros or []):
            return None
        log_path = cls._log_dir(run_id) / f'{distro}.log'
        if not log_path.exists():
            return ''
        return log_path.read_text(errors='replace')

    # ------------------------------------------------------------- internals
    @staticmethod
    def _log_dir(run_id):
        path = Path(current_app.instance_path) / 'sandbox-runs' / str(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def _execute(cls, app, run_id):
        with app.app_context():
            run = SandboxRun.query.get(run_id)
            work_dir = cls._log_dir(run_id)
            runner = work_dir / 'run-quick.sh'
            # newline='\n': Python on Windows would otherwise translate \n to
            # \r\n and dash chokes ("set: Illegal option -").
            runner.write_text(QUICK_RUNNER, newline='\n')

            threads = []
            for distro in (run.distros or []):
                t = threading.Thread(
                    target=cls._run_distro,
                    args=(app, run_id, distro, run.mode, work_dir),
                    daemon=True, name=f'sandbox-{run_id}-{distro}')
                t.start()
                threads.append(t)
            for t in threads:
                t.join()

            db.session.refresh(run)
            if run.status == 'running':  # not cancelled mid-flight
                run.status = 'done'
                run.finished_at = datetime.utcnow()
                db.session.commit()
            with _active_lock:
                _active_containers.pop(run_id, None)

    @classmethod
    def _run_distro(cls, app, run_id, distro, mode, work_dir):
        started = time.monotonic()
        container = f'sk-sandbox-{run_id}-{distro}'
        with _active_lock:
            _active_containers.setdefault(run_id, []).append(container)

        def update(status, detail):
            with app.app_context():
                run = SandboxRun.query.get(run_id)
                if not run:
                    return
                results = dict(run.results or {})
                results[distro] = {
                    'status': status,
                    'detail': detail,
                    'duration_s': round(time.monotonic() - started, 1),
                }
                runtime_path = work_dir / f'{distro}.runtime.json'
                if runtime_path.is_file():
                    try:
                        measurement = json.loads(runtime_path.read_text())
                        # Raw samples stay in the per-run artifact; compact evidence
                        # travels with the existing results API and log summary.
                        results[distro]['runtime_measurement'] = {
                            key: value for key, value in measurement.items() if key != 'samples'
                        }
                    except (OSError, ValueError):
                        results[distro]['runtime_measurement'] = {
                            'status': 'failed', 'reason': 'runtime artifact unreadable',
                        }
                run.results = results
                db.session.commit()

        update('running', '')
        log_path = work_dir / f'{distro}.log'
        try:
            with open(log_path, 'w', errors='replace') as log:
                if mode == 'quick':
                    ok, detail = cls._quick_container(container, distro, work_dir, log)
                else:
                    ok, detail = cls._full_container(container, distro, log)
            update('passed' if ok else 'failed', detail)
        except Exception as exc:  # noqa: BLE001 — record, don't kill the run
            update('failed', f'sandbox error: {exc}')
        finally:
            run_checked(['docker', 'rm', '-f', container], timeout=30)

    @classmethod
    def _quick_container(cls, container, distro, work_dir, log):
        image = DISTROS[distro]['quick_image']
        cmd = [
            'docker', 'run', '--rm', '--name', container,
            '-v', f'{REPO_ROOT}:/src',
            '-v', f'{work_dir}:/work',
            image, 'sh', '/work/run-quick.sh',
        ]
        proc = run_checked(cmd, stdout=log, stderr=subprocess.STDOUT,
                           timeout=QUICK_TIMEOUT_S)
        if proc['returncode'] is None:
            # Never produced an exit code: timed out, or docker is not there.
            return False, proc['error']
        return proc['success'], f"exit {proc['returncode']}"

    @classmethod
    def _full_container(cls, container, distro, log):
        image = DISTROS[distro]['full_image']
        run_cmd = [
            'docker', 'run', '-d', '--name', container,
            '--privileged', '--cgroupns=host',
            '-v', '/sys/fs/cgroup:/sys/fs/cgroup:rw',
            '-v', f'{REPO_ROOT}:/src:ro',
            image,
        ]
        proc = run_checked(run_cmd, stdout=log, stderr=subprocess.STDOUT, timeout=120)
        if not proc['success']:
            return False, f"container failed to start ({proc['error']})"

        # Wait for systemd to come up before exec'ing into the container.
        booted = False
        for _ in range(60):
            probe = run_checked(
                ['docker', 'exec', container, 'systemctl', 'is-system-running'],
                timeout=15)
            state = probe['output'].strip()
            if state in ('running', 'degraded'):
                booted = True
                break
            time.sleep(2)
        if not booted:
            return False, 'systemd never came up in the container'

        # systemd-networkd/resolved can still be bouncing the interface right
        # after boot — install.sh's first apt-get update would hit that window
        # and cache empty package lists. Wait for real DNS+HTTP first.
        # NB: probe with python3 (guaranteed in the geerlingguy ansible
        # images) — curl is NOT preinstalled there.
        net_ready = False
        for _ in range(45):
            probe = run_checked(
                ['docker', 'exec', container,
                 'python3', '-c',
                 "import urllib.request; urllib.request.urlopen('https://github.com', timeout=8)"],
                timeout=15)
            if probe['success']:
                net_ready = True
                break
            time.sleep(2)
        if not net_ready:
            return False, 'container networking never came up'

        log.write('\n===== install.sh =====\n')
        log.flush()
        install = run_checked(
            ['docker', 'exec', container, 'bash', '/src/install.sh'],
            stdout=log, stderr=subprocess.STDOUT, timeout=FULL_TIMEOUT_S)
        if not install['success']:
            return False, f"install.sh failed ({install['error']})"

        log.write('\n===== health probe =====\n')
        log.flush()
        for _ in range(60):
            probe = run_checked(
                ['docker', 'exec', container,
                 'python3', '-c',
                 "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/v1/system/health', timeout=8)"],
                timeout=15)
            if probe['success']:
                log.write('healthy\n')
                detail = cls._measure_runtime(container, log)
                return True, f'install + health OK; runtime {detail}'
            time.sleep(2)
        return False, 'install OK but health endpoint never came up'

    @staticmethod
    def _measure_runtime(container, log):
        """Observe after install; measurement failure is distinct from install failure."""
        log.write('\n===== post-install runtime baseline (60s settle + 120s sample) =====\n')
        log.flush()
        measured = run_checked(
            ['docker', 'exec', container, '/opt/serverkit/venv/bin/python',
             '/src/scripts/test/measure-installed-runtime.py',
             '--output', '/tmp/serverkit-runtime.json'],
            stdout=log, stderr=subprocess.STDOUT, timeout=240)
        artifact = Path(log.name).with_suffix('.runtime.json')
        copied = run_checked(
            ['docker', 'cp', f'{container}:/tmp/serverkit-runtime.json', str(artifact)],
            timeout=30)
        if not copied['success']:
            artifact.write_text(json.dumps({
                'status': 'failed', 'reason': 'runtime artifact could not be collected',
            }))
        if measured['success'] and copied['success']:
            try:
                if json.loads(artifact.read_text()).get('status') == 'measured':
                    return 'measured (see log/artifact)'
            except (OSError, ValueError):
                pass
        return 'unavailable/failed (see log/artifact)'
