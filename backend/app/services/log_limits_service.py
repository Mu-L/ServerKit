"""Bound the log files ServerKit writes or causes (plan 85 A5).

Four files grew without limit:

- the panel's own ``access.log`` / ``error.log`` / ``alerts.log`` /
  ``security_alerts.log`` (appended forever — no logrotate file existed);
- one ``update-<ts>.log`` per update, never pruned;
- per-app build logs under ``builds/<app>/``, never pruned;
- managed containers' json-file logs (no size cap anywhere).

``converge()`` runs at panel start on a Linux host; the prunes also run on the
6-hour retention tick. Everything is best-effort: a host without logrotate or
Docker simply skips that part.
"""
import glob
import json
import logging
import os
import posixpath

logger = logging.getLogger(__name__)

LOGROTATE_PATH = '/etc/logrotate.d/serverkit'
DOCKER_DAEMON_JSON = '/etc/docker/daemon.json'
ROTATED_LOGS = ('access.log', 'error.log', 'alerts.log', 'security_alerts.log')
KEEP_UPDATE_LOGS = 10
KEEP_BUILD_LOGS_PER_APP = 10
DOCKER_LOG_MAX_FILE = '3'


def logrotate_config(log_dir):
    # Always a Linux path: this file is only ever read by logrotate.
    paths = ' '.join(posixpath.join(log_dir, name) for name in ROTATED_LOGS)
    # copytruncate: gunicorn holds access/error.log open and is never told to
    # reopen them, so moving the file away would keep it writing to the old one.
    return (
        '# Managed by ServerKit (plan 85 A5) - rewritten at panel start.\n'
        f'{paths} {{\n'
        '    weekly\n'
        '    maxsize 50M\n'
        '    rotate 4\n'
        '    missingok\n'
        '    notifempty\n'
        '    compress\n'
        '    delaycompress\n'
        '    copytruncate\n'
        '}\n'
    )


def ensure_logrotate(log_dir, path=LOGROTATE_PATH):
    """Write the logrotate file when logrotate is installed. Returns True when
    the file was written or changed."""
    if not os.path.isdir(os.path.dirname(path)):
        return False
    content = logrotate_config(log_dir)
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == content:
                return False
    except OSError:
        pass
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return True


def ensure_docker_log_opts(max_size, path=DOCKER_DAEMON_JSON):
    """Give Docker a default json-file size cap, merged into an existing
    daemon.json. Never restarts Docker (that would restart every app), so it
    applies to containers created after Docker's next restart.

    Left alone: no Docker on the host, a daemon.json that does not parse (we
    will not rewrite a file we cannot read), a non-json-file log driver, or a
    max-size the operator already chose. Returns True when the file changed.
    """
    if not os.path.isdir(os.path.dirname(path)):
        return False
    config = {}
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                config = json.load(f)
        except (OSError, ValueError):
            logger.warning('Not setting Docker log limits: %s does not parse', path)
            return False
        if not isinstance(config, dict):
            return False
    if config.get('log-driver', 'json-file') != 'json-file':
        return False
    opts = config.get('log-opts') or {}
    if not isinstance(opts, dict) or 'max-size' in opts:
        return False
    opts = {**opts, 'max-size': str(max_size)}
    opts.setdefault('max-file', DOCKER_LOG_MAX_FILE)
    config['log-opts'] = opts
    tmp = path + '.serverkit-tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)
    return True


def _remove_all_but_newest(paths, keep):
    doomed = sorted(paths, key=lambda p: os.path.getmtime(p), reverse=True)[keep:]
    removed = 0
    for path in doomed:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def prune_update_logs(log_dir, keep=KEEP_UPDATE_LOGS):
    return _remove_all_but_newest(glob.glob(os.path.join(log_dir, 'update-*.log')), keep)


def prune_build_logs(build_log_dir, keep=KEEP_BUILD_LOGS_PER_APP):
    removed = 0
    for app_dir in glob.glob(os.path.join(build_log_dir, '*')):
        if os.path.isdir(app_dir):
            removed += _remove_all_but_newest(
                glob.glob(os.path.join(app_dir, 'build-*.json')), keep)
    return removed


def prune_files():
    """The retention-tick half: old update and build logs."""
    from app import paths
    return {
        'update_logs': prune_update_logs(paths.SERVERKIT_LOG_DIR),
        'build_logs': prune_build_logs(paths.BUILD_LOG_DIR),
    }


def converge():
    """Panel-start half: logrotate + Docker defaults, then the file prunes.
    Linux hosts only; every step is independent and best-effort."""
    if os.name == 'nt':
        return {}
    from app import paths
    from app.services import storage_profile_service
    from app.services.settings_service import SettingsService
    result = {}
    steps = (
        ('logrotate', lambda: ensure_logrotate(paths.SERVERKIT_LOG_DIR)),
        ('docker_log_opts', lambda: ensure_docker_log_opts(
            SettingsService.get('storage.docker_log_max_size')
            or storage_profile_service.default_for('storage.docker_log_max_size'))),
        ('files', prune_files),
    )
    for name, step in steps:
        try:
            result[name] = step()
        except Exception as e:
            logger.warning('Log limits: %s skipped: %s', name, e)
    return result
