#!/usr/bin/env python3
"""Passively sample explicit Linux cgroup-v2 services/containers; stdlib only.

Run on the host, outside the measured groups. No restarts, config changes, HTTP
requests, environment inspection, or load generation. Memory includes cache and
descendants; CPU 100% means one fully occupied logical CPU. Missing data is null.
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


CGROUP_ROOT = Path('/sys/fs/cgroup')


def command(args):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=15).stdout.strip()


def group_path(group, root=CGROUP_ROOT):
    relative = Path(group.lstrip('/'))
    if not group.startswith('/') or '..' in relative.parts or not relative.parts:
        raise ValueError('target must be a non-root cgroup')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError('target is outside the cgroup mount')
    return path


def resolve_target(kind, name):
    if not name or name.startswith('-'):
        raise ValueError('invalid target name')
    metadata = {'kind': kind, 'name': name}
    if kind == 'unit':
        group = command(['systemctl', 'show', '--property=ControlGroup', '--value', name])
    else:
        # Inspect selected fields only: a full Docker inspect exposes environment secrets.
        details = command(['docker', 'inspect', '--type=container', '--format',
                           '{{.State.Pid}} {{.Image}} {{.State.StartedAt}}', name]).split()
        pid = int(details[0])
        if pid <= 0:
            raise ValueError('container is not running')
        metadata.update(image_id=details[1], container_started_at=details[2])
        entries = Path(f'/proc/{pid}/cgroup').read_text().splitlines()
        group = next((line[3:] for line in entries if line.startswith('0::')), '')
    path = group_path(group)
    metadata.update(cgroup=str(path), inode=path.stat().st_ino)
    metadata['limits'] = {}
    for filename in ('memory.max', 'memory.swap.max', 'cpu.max', 'cpuset.cpus.effective'):
        try:
            metadata['limits'][filename] = (path / filename).read_text().strip()
        except OSError:
            metadata['limits'][filename] = None
    return metadata


def validate_scopes(targets):
    paths = [Path(target['cgroup']) for target in targets]
    for index, path in enumerate(paths):
        for other in paths[:index]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ValueError('targets overlap; select disjoint service/container cgroups')
    # Avoid attributing the sampler's own Python interpreter to ServerKit.
    entries = Path('/proc/self/cgroup').read_text().splitlines()
    own_group = next((line[3:] for line in entries if line.startswith('0::')), '')
    if own_group and own_group != '/':
        own_path = group_path(own_group)
        if any(own_path.is_relative_to(path) for path in paths):
            raise ValueError('run the sampler outside the measured service/container')


def key_values(text):
    return {key: int(value) for key, value in (line.split() for line in text.splitlines())}


def io_counters(text):
    return {parts[0]: {key: int(value) for key, value in
                      (item.split('=', 1) for item in parts[1:])}
            for parts in (line.split() for line in text.splitlines()) if parts}


def read_target(target):
    path = Path(target['cgroup'])
    result = {'unavailable': [], 'error': None}
    files = {
        'memory_bytes': ('memory.current', int),
        'swap_bytes': ('memory.swap.current', int),
        'memory_stat': ('memory.stat', key_values),
        'memory_events': ('memory.events', key_values),
        'cpu_stat': ('cpu.stat', key_values),
        'io_by_device': ('io.stat', io_counters),
        'tasks': ('pids.current', int),
    }
    for field, (filename, parse) in files.items():
        try:
            result[field] = parse((path / filename).read_text())
        except (OSError, ValueError):
            result[field] = None
            result['unavailable'].append(filename)
    try:
        if path.stat().st_ino != target['inode']:
            result['error'] = 'cgroup_replaced'
    except OSError:
        result['error'] = 'cgroup_unavailable'
    if result['memory_bytes'] is None or 'usage_usec' not in (result['cpu_stat'] or {}):
        result['error'] = result['error'] or 'required_counters_unavailable'
    # perf_counter: monotonic() has ~15.6 ms ticks on Windows, collapsing short intervals.
    result['monotonic_seconds'] = time.perf_counter()
    return result


def cpu_percent(previous, current):
    if not previous or previous['error'] or current['error']:
        return None
    elapsed = current['monotonic_seconds'] - previous['monotonic_seconds']
    delta = current['cpu_stat']['usage_usec'] - previous['cpu_stat']['usage_usec']
    if elapsed <= 0 or delta < 0:
        current['error'] = 'cpu_counter_reset_or_invalid_interval'
        return None
    return delta / (elapsed * 1_000_000) * 100


def host_memory():
    values = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
            values[key] = int(value.split()[0]) * 1024
    return values


def distribution(values):
    values = sorted(value for value in values if value is not None)
    def percentile(fraction):
        position = (len(values) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)
    return {'count': len(values), 'min': min(values) if values else None,
            'p50': percentile(.5) if values else None,
            'p95': percentile(.95) if values else None,
            'sampled_max': max(values) if values else None}


def summarize(samples, targets):
    summaries = []
    for index, target in enumerate(targets):
        rows = [sample['targets'][index] for sample in samples]
        valid = [row for row in rows if not row['error']]
        summaries.append({
            'name': target['name'], 'kind': target['kind'],
            'failed_samples': sum(bool(row['error']) for row in rows),
            'unavailable_files': sorted({name for row in rows for name in row['unavailable']}),
            **{field: distribution([row[field] for row in valid])
               for field in ('memory_bytes', 'swap_bytes', 'tasks', 'cpu_percent_one_core')},
        })
    return summaries


def collect(targets, duration, interval):
    samples, previous, interrupted = [], [None] * len(targets), False
    started = time.perf_counter()
    deadline = started + duration
    try:
        while True:
            sample = {'elapsed_seconds': time.perf_counter() - started,
                      'host_memory_bytes': host_memory(), 'targets': []}
            for index, target in enumerate(targets):
                row = read_target(target)
                row['cpu_percent_one_core'] = cpu_percent(previous[index], row)
                sample['targets'].append(row)
                previous[index] = row
            samples.append(sample)
            now = time.perf_counter()
            if now >= deadline:
                break
            time.sleep(min(interval, deadline - now))
    except KeyboardInterrupt:
        interrupted = True
    return samples, interrupted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit', action='append', default=[], help='systemd unit; repeatable')
    parser.add_argument('--container', action='append', default=[], help='Docker container; repeatable')
    parser.add_argument('--duration', type=float, default=600, help='seconds, default 600')
    parser.add_argument('--interval', type=float, default=5, help='seconds, default 5')
    parser.add_argument('--scenario', required=True, help='e.g. idle-closed, dashboard-open, deploy')
    parser.add_argument('--revision', required=True, help='revision actually running, not sampler revision')
    parser.add_argument('--notes', default='', help='workload, enabled extensions, DB and warmup; no secrets')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.unit and not args.container:
        parser.error('select at least one --unit or --container')
    if (not all(math.isfinite(value) and value > 0 for value in (args.duration, args.interval))
            or args.interval > args.duration):
        parser.error('require finite duration >= interval > 0')
    if platform.system() != 'Linux' or not (CGROUP_ROOT / 'cgroup.controllers').is_file():
        parser.error('requires a Linux host with cgroup v2; cgroup v1 is not supported')
    try:
        targets = [resolve_target(kind, name) for kind, names in
                   (('unit', args.unit), ('container', args.container)) for name in names]
        validate_scopes(targets)
        for target in targets:
            if read_target(target)['error']:
                raise ValueError(f"required counters unavailable for {target['name']}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(f'target discovery failed ({type(exc).__name__}); check names, permissions and cgroup v2')
    # Exclusive create prevents accidentally overwriting a before/after baseline.
    with args.output.open('x', encoding='utf-8') as output:
        report = {
            'schema_version': 1, 'started_at': datetime.now(timezone.utc).isoformat(),
            'scenario': args.scenario, 'running_revision': args.revision, 'notes': args.notes,
            'host': {'os': platform.freedesktop_os_release().get('PRETTY_NAME'),
                     'kernel': platform.release(), 'architecture': platform.machine(),
                     'logical_cpus': os.cpu_count(), 'python': platform.python_version(),
                     'uptime_seconds': float(Path('/proc/uptime').read_text().split()[0])},
            'requested_duration_seconds': args.duration, 'interval_seconds': args.interval,
            'targets': targets,
            'accounting': {
                'memory': 'cgroup memory.current; includes cache, kernel and descendants; not process RSS',
                'cpu': 'delta usage_usec / actual elapsed time; 100% = one logical CPU',
                'io': 'cumulative per-device counters; do not sum stacked devices blindly',
                'scope': 'explicit targets only; excludes other services and separately created app containers',
                'peaks': 'sampled maxima only; short spikes between samples can be missed',
            },
        }
        print('Sampling explicit targets; Ctrl+C saves a partial report.', file=sys.stderr, flush=True)
        samples, interrupted = collect(targets, args.duration, args.interval)
        report.update(samples=samples, interrupted=interrupted,
                      finished_at=datetime.now(timezone.utc).isoformat(),
                      summary=summarize(samples, targets))
        json.dump(report, output, indent=2, allow_nan=False)
        output.write('\n')
    print(json.dumps(report['summary'], indent=2))
    return 130 if interrupted else int(any(row['failed_samples'] for row in report['summary']))


if __name__ == '__main__':
    raise SystemExit(main())
