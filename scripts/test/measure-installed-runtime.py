#!/usr/bin/env python3
"""Record a short post-install baseline, with explicit unavailable/error results.

Used by full distro installs (VMs and systemd containers), never quick/provision
checks. This is a regression observation, not a production capacity benchmark.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


SPEC = importlib.util.spec_from_file_location('measure_runtime', Path(__file__).parents[1] / 'measure-runtime.py')
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def source_identity(source, archive=None):
    if archive:
        digest = hashlib.sha256()
        with archive.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        return 'local-overlay-archive-sha256:' + digest.hexdigest()
    try:
        revision = runtime.command(['git', '-C', str(source), 'rev-parse', 'HEAD'])
        dirty = runtime.command(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=no'])
        return revision + (' (tracked files modified)' if dirty else '')
    except (OSError, subprocess.SubprocessError):
        version = source / 'VERSION'
        return 'release:' + version.read_text().strip() if version.is_file() else 'unknown'


def observe(source, duration=120, settle=60, archive=None):
    result = {'schema_version': 1, 'status': 'unavailable',
              'scenario': 'post-install-dashboard-closed',
              'started_at': datetime.now(timezone.utc).isoformat(),
              'settle_seconds': settle, 'requested_duration_seconds': duration,
              'scope_note': 'Selected service cgroups only; other services/apps are excluded. '
                            'Container tests share the host kernel and may run beside other tests.'}
    if platform.system() != 'Linux' or not (runtime.CGROUP_ROOT / 'cgroup.controllers').is_file():
        result['reason'] = 'Linux cgroup v2 unavailable; no runtime measurement taken'
        return result
    try:
        revision = source_identity(source, archive)
        targets = [runtime.resolve_target('unit', 'serverkit.service')]
        for unit in ('nginx.service', 'docker.service', 'containerd.service'):
            try:
                if runtime.command(['systemctl', 'is-active', unit]) == 'active':
                    targets.append(runtime.resolve_target('unit', unit))
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
        runtime.validate_scopes(targets)
        # This pause is after builds/health probes, before measurement or pytest.
        time.sleep(settle)
        samples, interrupted = runtime.collect(targets, duration, 5)
        summary = runtime.summarize(samples, targets)
        failed = any(row['failed_samples'] for row in summary)
        result.update(status='interrupted' if interrupted else 'failed' if failed else 'measured',
                      running_revision=revision, targets=targets, summary=summary, samples=samples,
                      os=platform.freedesktop_os_release().get('PRETTY_NAME'),
                      kernel=platform.release(), architecture=platform.machine(),
                      logical_cpus=os.cpu_count(),
                      definitions={'memory_bytes': 'memory.current including cache/kernel/descendants',
                                   'cpu_percent_one_core': '100% = one logical CPU',
                                   'sampled_max': 'largest sampled value; short spikes can be missed'})
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result.update(status='failed', reason=f'Runtime collection failed: {type(exc).__name__}')
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('/opt/serverkit'))
    parser.add_argument('--source-archive', type=Path, help='VM harness local overlay archive')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration', type=float, default=120)
    parser.add_argument('--settle', type=float, default=60)
    args = parser.parse_args(argv)
    if (not math.isfinite(args.duration) or args.duration < 5
            or not math.isfinite(args.settle) or args.settle < 0):
        parser.error('duration must be finite and >= 5; settle finite and >= 0')
    # A VM reuse explicitly produces a new observation at this harness scratch path.
    result = observe(args.source, args.duration, args.settle, args.source_archive)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'samples'}, indent=2))
    return 0 if result['status'] == 'measured' else 1


if __name__ == '__main__':
    raise SystemExit(main())
