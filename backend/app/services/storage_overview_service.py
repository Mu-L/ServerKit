"""Where the panel host's disk goes (plan 85 §D1).

Read-only: measures, never deletes. Cleanup stays in
``disk_reclaim_service`` (its safe candidates are what the Storage page
offers next to each number).
"""
import json
import os
import re
import shutil
import time

from app.services import storage_profile_service

RETENTION_KEYS = (
    'telemetry.retention_days',
    'jobs.retention_days',
    'history.retention_days',
    'audit_log_retention_days',
    'storage.previous_slot_hours',
    'storage.docker_log_max_size',
)

_DOCKER_SIZE = re.compile(r'^\s*([\d.]+)\s*([kKMGTP]?B)\s*$')
_UNITS = {'B': 1, 'kB': 1000, 'KB': 1000, 'MB': 1000 ** 2, 'GB': 1000 ** 3,
          'TB': 1000 ** 4, 'PB': 1000 ** 5}


def parse_docker_size(text):
    """``docker system df`` prints decimal sizes ("9.308GB", "81.92kB",
    sometimes with a "(37%)" suffix on reclaimable). Unparseable → None."""
    if not text:
        return None
    match = _DOCKER_SIZE.match(str(text).split('(')[0])
    if not match:
        return None
    return int(float(match.group(1)) * _UNITS.get(match.group(2), 1))


def docker_usage():
    """``{type: {'bytes', 'reclaimable'}}`` from ``docker system df``, or {}
    when Docker is absent or not answering."""
    from app.services.disk_reclaim_service import _run
    ok, out, _err = _run(['docker', 'system', 'df', '--format', '{{json .}}'], timeout=60)
    if not ok:
        return {}
    usage = {}
    for line in (out or '').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        kind = {'Images': 'images', 'Containers': 'containers',
                'Local Volumes': 'volumes', 'Build Cache': 'build_cache'}.get(row.get('Type'))
        if kind:
            usage[kind] = {'bytes': parse_docker_size(row.get('Size')),
                           'reclaimable': parse_docker_size(row.get('Reclaimable'))}
    return usage


def _database_bytes():
    from app import db
    try:
        path = db.engine.url.database
    except Exception:
        return None
    if db.engine.dialect.name != 'sqlite' or not path or not os.path.exists(path):
        return None
    return sum(os.path.getsize(p) for p in (path, path + '-wal', path + '-shm')
               if os.path.exists(p))


def overview():
    from app import paths
    from app.services.disk_reclaim_service import _path_size
    from app.services.settings_service import SettingsService
    from app.services import slot_retention_service

    probe = paths.SERVERKIT_DIR if os.path.isdir(paths.SERVERKIT_DIR) else os.path.abspath(os.sep)
    try:
        du = shutil.disk_usage(probe)
        disk = {'path': probe, 'total': du.total, 'used': du.used, 'free': du.free,
                'percent': round(du.used / du.total * 100, 1) if du.total else None}
    except OSError:
        disk = {'path': probe, 'total': None, 'used': None, 'free': None, 'percent': None}

    docker = docker_usage()
    items = []

    def add(key, label, size, reclaimable=None):
        if size is not None:
            items.append({'key': key, 'label': label, 'bytes': size,
                          'reclaimable': reclaimable})

    add('docker_images', 'App images', (docker.get('images') or {}).get('bytes'),
        (docker.get('images') or {}).get('reclaimable'))
    add('docker_volumes', 'App volumes', (docker.get('volumes') or {}).get('bytes'),
        (docker.get('volumes') or {}).get('reclaimable'))
    add('docker_build_cache', 'Docker build cache',
        (docker.get('build_cache') or {}).get('bytes'),
        (docker.get('build_cache') or {}).get('reclaimable'))
    add('panel_database', 'Panel database', _database_bytes())
    for key, label, path in (('backups', 'Backups and upgrade snapshots', paths.SERVERKIT_BACKUP_DIR),
                             ('panel_logs', 'Panel logs', paths.SERVERKIT_LOG_DIR)):
        if os.path.isdir(path):
            add(key, label, _path_size(path))

    previous = None
    slot = slot_retention_service.previous_slot()
    if slot:
        hours = SettingsService.get('storage.previous_slot_hours', 24)
        try:
            switched = os.lstat(slot_retention_service.INSTALL_DIR).st_mtime
            removable_at = switched + int(hours) * 3600 if int(hours) > 0 else None
        except (OSError, TypeError, ValueError):
            removable_at = None
        previous = {'path': slot, 'bytes': _path_size(slot),
                    'removable_at': removable_at,
                    'removable_now': bool(removable_at and removable_at <= time.time())}
        add('previous_install', 'Previous version (rollback)', previous['bytes'])

    items.sort(key=lambda item: item['bytes'] or 0, reverse=True)
    return {
        'disk': disk,
        'profile': storage_profile_service.profile_name(disk['total']),
        'items': items,
        'previous_install': previous,
        'retention': {key: SettingsService.get(key) for key in RETENTION_KEYS},
    }
