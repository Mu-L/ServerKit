"""Curated database config tuner — vetted engine settings with RAM-aware
suggestions, applied to Dockerised MySQL/MariaDB/PostgreSQL containers and
rolled back cleanly.

Design notes:
- The catalog (``CURATED_SETTINGS``) is deliberately tiny: only settings with
  well-known rules of thumb and a safe min/max envelope. Nothing outside the
  catalog can ever be written.
- **Never auto-applies.** ``inspect()`` only reads and computes suggestions;
  ``apply()`` requires an explicit settings payload from the operator.
- MySQL/MariaDB: writes a ServerKit-owned drop-in
  (``/etc/mysql/conf.d/serverkit-tuner.cnf``) into the container via
  ``docker cp``. PostgreSQL: uses ``ALTER SYSTEM`` (persists to
  ``postgresql.auto.conf``), with a file-level backup of that conf for
  rollback even if the engine fails to boot.
- Every previous state is kept as a timestamped backup under
  ``STATE_DIR/<engine>/<container>/`` on the panel host, so ``rollback()``
  works after a bad apply — including the "engine never came back" case.
- All shell/docker/SQL goes through small choke points (``_docker``,
  ``_exec_sql``, ``_restart``, ``_host_ram_mb``, ``_sleep``,
  ``_linux_supported``) so tests can stub them.
- Linux/Docker only, like the rest of the service layer.
"""
import logging
import json
import os
import sys
import time
from datetime import datetime

from app.exceptions import (
    DependencyUnavailableError,
    NotFoundError,
    ValidationError,
)
from app import paths
from app.services import db_exec

logger = logging.getLogger(__name__)

# Where the MySQL drop-in lives inside the container (the official mysql and
# mariadb images both include this conf.d directory by default).
MYSQL_DROPIN_PATH = '/etc/mysql/conf.d/serverkit-tuner.cnf'

DROPIN_HEADER = '# Managed by ServerKit config tuner. Do not edit by hand.'

# Engines the tuner understands. mariadb/percona are the mysql dialect.
_ENGINE_ALIASES = {'mysql': 'mysql', 'mariadb': 'mysql', 'percona': 'mysql',
                   'postgres': 'postgresql', 'postgresql': 'postgresql'}


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


# ── Curated catalog ──────────────────────────────────────────────────────────
# Each entry: description (one operator-friendly sentence), unit, safe min/max
# (canonical unit), type (how values parse/render), and suggest(total_ram_mb,
# is_dedicated) — standard rules of thumb, later clamped into [min, max].
#
# types: 'size_mb'  memory size, canonical MB
#        'int'      plain integer (e.g. connections)
#        'bool'     0/1 toggle
#        'float_s'  seconds as a float
#        'int_ms'   integer milliseconds
CURATED_SETTINGS = {
    'mysql': {
        'innodb_buffer_pool_size': {
            'description': 'Main InnoDB data/index cache; the single most impactful MySQL memory setting.',
            'unit': 'MB', 'type': 'size_mb', 'min': 128, 'max': 262144,
            'suggest': lambda ram_mb, dedicated=False: ram_mb * (0.5 if dedicated else 0.25),
        },
        'max_connections': {
            'description': 'Maximum simultaneous client connections; each connection costs roughly 12 MB of RAM.',
            'unit': 'connections', 'type': 'int', 'min': 25, 'max': 5000,
            'suggest': lambda ram_mb, dedicated=False: _clamp(ram_mb // 12, 100, 1000),
        },
        'slow_query_log': {
            'description': 'Log queries slower than long_query_time so slow spots can actually be found.',
            'unit': 'on/off', 'type': 'bool', 'min': 0, 'max': 1,
            'suggest': lambda ram_mb, dedicated=False: 1,
        },
        'long_query_time': {
            'description': 'Seconds a query must run before it counts as slow and gets logged.',
            'unit': 'seconds', 'type': 'float_s', 'min': 0.1, 'max': 60,
            'suggest': lambda ram_mb, dedicated=False: 1.0,
        },
        'tmp_table_size': {
            'description': 'Largest in-memory temporary table before MySQL spills it to disk.',
            'unit': 'MB', 'type': 'size_mb', 'min': 16, 'max': 1024,
            'suggest': lambda ram_mb, dedicated=False: _clamp(ram_mb * 0.02, 32, 512),
        },
        'max_heap_table_size': {
            'description': 'Cap for MEMORY tables; keep it equal to tmp_table_size (the lower of the two wins).',
            'unit': 'MB', 'type': 'size_mb', 'min': 16, 'max': 1024,
            'suggest': lambda ram_mb, dedicated=False: _clamp(ram_mb * 0.02, 32, 512),
        },
        'innodb_log_file_size': {
            'description': 'Redo log size; roughly a quarter of the buffer pool smooths out write bursts.',
            'unit': 'MB', 'type': 'size_mb', 'min': 64, 'max': 8192,
            'suggest': lambda ram_mb, dedicated=False: ram_mb * ((0.5 if dedicated else 0.25) / 4),
        },
    },
    'postgresql': {
        'shared_buffers': {
            'description': "PostgreSQL's own page cache; about a quarter of RAM is the standard starting point.",
            'unit': 'MB', 'type': 'size_mb', 'min': 128, 'max': 16384,
            'suggest': lambda ram_mb, dedicated=False: ram_mb * 0.25,
        },
        'work_mem': {
            'description': 'Memory each sort/hash operation may use — every connection can use several at once.',
            'unit': 'MB', 'type': 'size_mb', 'min': 4, 'max': 256,
            'suggest': lambda ram_mb, dedicated=False: _clamp(ram_mb // 64, 4, 256),
        },
        'maintenance_work_mem': {
            'description': 'Memory for VACUUM, CREATE INDEX and other maintenance; ~5% of RAM speeds these up a lot.',
            'unit': 'MB', 'type': 'size_mb', 'min': 64, 'max': 2048,
            'suggest': lambda ram_mb, dedicated=False: _clamp(ram_mb * 0.05, 64, 2048),
        },
        'max_connections': {
            'description': 'Maximum simultaneous client connections; prefer a pooler over a huge number here.',
            'unit': 'connections', 'type': 'int', 'min': 25, 'max': 5000,
            'suggest': lambda ram_mb, dedicated=False: 200 if dedicated else 100,
        },
        'effective_cache_size': {
            'description': 'Planner hint for total OS + database cache; not an allocation, just an estimate.',
            'unit': 'MB', 'type': 'size_mb', 'min': 256, 'max': 786432,
            'suggest': lambda ram_mb, dedicated=False: ram_mb * (0.75 if dedicated else 0.5),
        },
        'log_min_duration_statement': {
            'description': 'Log any statement running longer than this many milliseconds (the slow-query log).',
            'unit': 'ms', 'type': 'int_ms', 'min': 100, 'max': 600000,
            'suggest': lambda ram_mb, dedicated=False: 1000,
        },
    },
}


class DbConfigTunerService:
    """Inspect / apply / rollback curated engine settings on DB containers."""

    # Panel-host directory holding drop-ins + timestamped rollback backups.
    STATE_DIR = os.path.join(paths.SERVERKIT_DIR, 'db-tuner')

    # ── choke points (stub these in tests) ──────────────────────────────────

    @classmethod
    def _linux_supported(cls):
        return sys.platform.startswith('linux')

    @classmethod
    def _docker(cls, args, timeout=60):
        """Run a docker CLI command — via DockerService.run (§G3).

        The named seam stays (tests stub it). The private copy collapsed a
        missing docker and a timed-out docker into the same ``str(e)``, so a
        caller could not tell "not installed" from "too slow".
        """
        from app.services.docker_service import DockerService
        return DockerService.run(args, timeout=timeout)

    @classmethod
    def _exec_sql(cls, target, sql):
        """Run SQL against the target's engine — via ``db_exec`` (§G5).

        ``target``: {'container', 'engine', 'user', 'password'}.
        Returns {'success', 'output', 'error'} with machine-readable output
        (tab-separated, no headers/decorations) — the reason this used to be a
        private copy of the whole dispatcher rather than a call into it.
        """
        return db_exec.exec_sql(target, sql, machine_readable=True, timeout=30)

    @classmethod
    def _restart(cls, container):
        """Restart the container via the existing DockerService path."""
        from app.services.docker_service import DockerService
        return DockerService.restart_container(container)

    @classmethod
    def _host_ram_mb(cls):
        import psutil
        return int(psutil.virtual_memory().total / (1024 * 1024))

    @classmethod
    def _sleep(cls, seconds):
        time.sleep(seconds)

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def normalize_engine(engine):
        return _ENGINE_ALIASES.get((engine or '').strip().lower())

    @classmethod
    def _guard(cls, target):
        """Common precondition check; raises ValidationError."""
        if not cls._linux_supported():
            raise ValidationError('The config tuner is only available on Linux hosts running Docker')
        if not target.get('container'):
            raise ValidationError('A Docker container target is required')
        if target.get('engine') not in CURATED_SETTINGS:
            raise ValidationError('engine must be mysql or postgresql')

    @classmethod
    def _ram_mb(cls, container):
        """Container memory limit if set, else host RAM. → (mb, source)."""
        res = cls._docker(['inspect', '--format', '{{.HostConfig.Memory}}', container],
                          timeout=15)
        if res['success']:
            try:
                limit = int(res['output'].strip())
                if limit > 0:
                    return int(limit / (1024 * 1024)), 'container_limit'
            except ValueError:
                pass
        return cls._host_ram_mb(), 'host_total'

    @classmethod
    def suggest_value(cls, engine, key, total_ram_mb, is_dedicated=False):
        """Suggested value for a catalog key, clamped into its safe range."""
        entry = CURATED_SETTINGS[engine][key]
        value = _clamp(entry['suggest'](total_ram_mb, is_dedicated),
                       entry['min'], entry['max'])
        if entry['type'] == 'float_s':
            return round(float(value), 2)
        return int(round(value))

    @classmethod
    def validate_settings(cls, engine, settings):
        """Only curated keys, only values inside the safe envelope.
        → error string or None; also coerces values in place."""
        if not isinstance(settings, dict) or not settings:
            return 'settings must be a non-empty object of {key: value}'
        catalog = CURATED_SETTINGS[engine]
        for key, value in list(settings.items()):
            entry = catalog.get(key)
            if entry is None:
                return f"'{key}' is not a tunable setting for {engine}"
            try:
                value = float(value)
            except (TypeError, ValueError):
                return f"'{key}' must be a number"
            if value < entry['min'] or value > entry['max']:
                return (f"'{key}' must be between {entry['min']} and "
                        f"{entry['max']} {entry['unit']}")
            settings[key] = round(value, 2) if entry['type'] == 'float_s' else int(value)
        return None

    # ── current-value parsing ────────────────────────────────────────────────

    @staticmethod
    def _mysql_to_canonical(entry, raw):
        raw = (raw or '').strip()
        if entry['type'] == 'size_mb':
            return round(float(raw) / (1024 * 1024), 2)   # SHOW VARIABLES → bytes
        if entry['type'] == 'bool':
            return 1 if raw.upper() in ('ON', '1', 'TRUE') else 0
        if entry['type'] == 'float_s':
            return round(float(raw), 2)
        return int(float(raw))

    @staticmethod
    def _pg_to_canonical(entry, setting, unit):
        value = float(setting)
        unit = (unit or '').strip()
        if entry['type'] == 'size_mb':
            factor = {'8kB': 8.0 / 1024, 'kB': 1.0 / 1024, 'MB': 1.0,
                      'GB': 1024.0, 'B': 1.0 / (1024 * 1024)}.get(unit, 1.0)
            return round(value * factor, 2)
        if entry['type'] == 'int_ms':
            factor = {'s': 1000, 'min': 60000}.get(unit, 1)
            return int(value * factor)
        return int(value)

    @classmethod
    def _read_current(cls, target):
        """Live current values from the engine. → (dict|None, error|None)."""
        engine = target['engine']
        keys = sorted(CURATED_SETTINGS[engine])
        quoted = ', '.join("'%s'" % k for k in keys)
        if engine == 'mysql':
            sql = f'SHOW VARIABLES WHERE Variable_name IN ({quoted});'
        else:
            sql = f'SELECT name, setting, unit FROM pg_settings WHERE name IN ({quoted});'
        res = cls._exec_sql(target, sql)
        if not res['success']:
            return None, res.get('error') or 'Could not query the database engine'

        current = {}
        for line in (res['output'] or '').strip().splitlines():
            parts = line.rstrip('\n').split('\t')
            if not parts or parts[0] in ('Variable_name', 'name'):
                continue  # tolerate a header row
            key = parts[0].strip()
            entry = CURATED_SETTINGS[engine].get(key)
            if entry is None:
                continue
            try:
                if engine == 'mysql':
                    current[key] = cls._mysql_to_canonical(entry, parts[1])
                else:
                    unit = parts[2] if len(parts) > 2 else ''
                    current[key] = cls._pg_to_canonical(entry, parts[1], unit)
            except (IndexError, ValueError):
                logger.debug('Unparseable current value for %s: %r', key, line)
        return current, None

    # ── rendering ────────────────────────────────────────────────────────────

    @classmethod
    def render_mysql_dropin(cls, settings):
        """The serverkit-tuner.cnf content for a validated settings dict."""
        lines = [DROPIN_HEADER, '[mysqld]']
        catalog = CURATED_SETTINGS['mysql']
        for key in sorted(settings):
            entry = catalog[key]
            value = settings[key]
            if entry['type'] == 'size_mb':
                rendered = f'{int(value)}M'
            elif entry['type'] == 'bool':
                rendered = str(1 if value else 0)
            elif entry['type'] == 'float_s':
                rendered = str(round(float(value), 2))
            else:
                rendered = str(int(value))
            lines.append(f'{key} = {rendered}')
        return '\n'.join(lines) + '\n'

    @staticmethod
    def _pg_literal(entry, value):
        if entry['type'] == 'size_mb':
            return f"'{int(value)}MB'"
        if entry['type'] == 'bool':
            return 'on' if value else 'off'
        if entry['type'] == 'float_s':
            return str(round(float(value), 2))
        return str(int(value))

    # ── backup bookkeeping (panel host) ─────────────────────────────────────

    @classmethod
    def _state_dir(cls, target, create=False):
        d = os.path.join(cls.STATE_DIR, target['engine'], target['container'])
        if create:
            os.makedirs(d, exist_ok=True)
        return d

    @classmethod
    def _latest_backup(cls, target):
        """Newest previous-* backup file path, or None."""
        d = cls._state_dir(target)
        if not os.path.isdir(d):
            return None
        backups = sorted(f for f in os.listdir(d) if f.startswith('previous-'))
        return os.path.join(d, backups[-1]) if backups else None

    @classmethod
    def _snapshot_container_file(cls, target, container_path, suffix):
        """Copy a file out of the container as a timestamped backup. If the
        file doesn't exist yet, record an ``.absent`` marker so rollback knows
        to delete rather than restore. → backup path."""
        d = cls._state_dir(target, create=True)
        stamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
        backup = os.path.join(d, f'previous-{stamp}{suffix}')
        res = cls._docker(['cp', f"{target['container']}:{container_path}", backup],
                          timeout=30)
        if not res['success']:
            backup = os.path.join(d, f'previous-{stamp}.absent')
            with open(backup, 'w') as fh:
                fh.write(container_path + '\n')
        return backup

    @classmethod
    def _restore_container_file(cls, target, backup, container_path):
        """Put a backup back into the container (or delete the file if the
        backup is an absence marker). → {'success', 'error'}."""
        if backup.endswith('.absent'):
            return cls._docker(['exec', target['container'], 'rm', '-f', container_path],
                               timeout=30)
        return cls._docker(['cp', backup, f"{target['container']}:{container_path}"],
                           timeout=30)

    # ── engine liveness ──────────────────────────────────────────────────────

    @classmethod
    def _wait_ready(cls, target, attempts=10, delay=2):
        """Simple ping loop (SELECT 1) after a restart."""
        for i in range(attempts):
            if i:
                cls._sleep(delay)
            if cls._exec_sql(target, 'SELECT 1;')['success']:
                return True
        return False

    # ── public API ───────────────────────────────────────────────────────────

    @classmethod
    def inspect(cls, target, is_dedicated=False):
        """Current vs suggested for every curated setting. Read-only —
        suggestions are NEVER applied automatically."""
        cls._guard(target)

        current, err = cls._read_current(target)
        if err:
            raise DependencyUnavailableError(err)

        engine = target['engine']
        ram_mb, ram_source = cls._ram_mb(target['container'])

        settings, diff = [], []
        for key in sorted(CURATED_SETTINGS[engine]):
            entry = CURATED_SETTINGS[engine][key]
            suggested = cls.suggest_value(engine, key, ram_mb, is_dedicated)
            cur = current.get(key)
            differs = cur is not None and float(cur) != float(suggested)
            if differs:
                diff.append(key)
            settings.append({
                'key': key,
                'description': entry['description'],
                'unit': entry['unit'],
                'min': entry['min'],
                'max': entry['max'],
                'current': cur,
                'suggested': suggested,
                'differs': differs,
            })

        return {
            'engine': engine,
            'container': target['container'],
            'ram_mb': ram_mb,
            'ram_source': ram_source,
            'is_dedicated': bool(is_dedicated),
            'settings': settings,
            'diff': diff,
            'can_rollback': cls._latest_backup(target) is not None,
        }

    @classmethod
    def apply(cls, target, settings):
        """Apply an explicit, operator-chosen settings dict. Backs up the
        previous state, restarts the container, verifies the engine came back,
        and rolls back automatically if it didn't."""
        cls._guard(target)
        settings = dict(settings or {})
        err = cls.validate_settings(target['engine'], settings)
        if err:
            raise ValidationError(err)

        if target['engine'] == 'mysql':
            return cls._apply_mysql(target, settings)
        return cls._apply_postgresql(target, settings)

    @classmethod
    def _apply_mysql(cls, target, settings):
        container = target['container']
        backup = cls._snapshot_container_file(target, MYSQL_DROPIN_PATH, '.cnf')

        # Stage the new drop-in on the panel host, then copy it in.
        d = cls._state_dir(target, create=True)
        staged = os.path.join(d, 'serverkit-tuner.cnf')
        with open(staged, 'w') as fh:
            fh.write(cls.render_mysql_dropin(settings))
        res = cls._docker(['cp', staged, f'{container}:{MYSQL_DROPIN_PATH}'], timeout=30)
        if not res['success']:
            raise DependencyUnavailableError(
                f"Could not write the drop-in into the container: {res['error']}")

        return cls._restart_and_verify(target, backup, MYSQL_DROPIN_PATH, settings)

    @classmethod
    def _apply_postgresql(cls, target, settings):
        # Find postgresql.auto.conf (what ALTER SYSTEM writes) for file-level
        # rollback even when the engine won't boot back up.
        res = cls._exec_sql(target, 'SHOW data_directory;')
        if not res['success']:
            raise DependencyUnavailableError(
                res.get('error') or 'Could not determine the data directory')
        data_dir = (res['output'] or '').strip().splitlines()
        data_dir = data_dir[0].strip() if data_dir else ''
        if not data_dir:
            raise DependencyUnavailableError('Could not determine the data directory')
        auto_conf = data_dir.rstrip('/') + '/postgresql.auto.conf'

        backup = cls._snapshot_container_file(target, auto_conf, '.auto.conf')

        catalog = CURATED_SETTINGS['postgresql']
        for key in sorted(settings):
            literal = cls._pg_literal(catalog[key], settings[key])
            res = cls._exec_sql(target, f'ALTER SYSTEM SET {key} = {literal};')
            if not res['success']:
                # Nothing took effect yet (needs a restart) — restore the conf
                # so no partial ALTERs linger, and bail.
                cls._restore_container_file(target, backup, auto_conf)
                raise DependencyUnavailableError(
                    f"ALTER SYSTEM failed for '{key}': {res['error']}")

        # Remember where auto.conf lives so rollback works without SQL.
        with open(os.path.join(cls._state_dir(target, create=True), 'applied.json'), 'w') as fh:
            json.dump({'auto_conf': auto_conf, 'settings': settings}, fh)

        return cls._restart_and_verify(target, backup, auto_conf, settings)

    @classmethod
    def enable_pg_stat_statements(cls, target):
        """Preload pg_stat_statements, restart, and create the extension
        (plan 86 §A3), through the same backup/restart/verify path as
        :meth:`apply`, so :meth:`rollback` undoes it.

        ``shared_preload_libraries`` is a list: the current value is kept and
        the library appended, never replaced.
        """
        cls._guard(target)
        if target['engine'] != 'postgresql':
            raise ValidationError('pg_stat_statements is a PostgreSQL extension')
        res = cls._exec_sql(target, 'SHOW shared_preload_libraries;')
        if not res['success']:
            raise DependencyUnavailableError(res.get('error') or 'Could not read preload libraries')
        first_line = ((res['output'] or '').strip().splitlines() or [''])[0]
        current = [lib.strip().strip('"') for lib in first_line.split(',') if lib.strip().strip('"')]
        if 'pg_stat_statements' in current:
            created = cls._exec_sql(target, 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements;')
            if not created['success']:
                raise DependencyUnavailableError(created.get('error') or 'CREATE EXTENSION failed')
            return {'success': True, 'restarted': False, 'already_preloaded': True}

        res = cls._exec_sql(target, 'SHOW data_directory;')
        data_dir = ((res.get('output') or '').strip().splitlines() or [''])[0].strip()
        if not res['success'] or not data_dir:
            raise DependencyUnavailableError(res.get('error') or 'Could not determine the data directory')
        auto_conf = data_dir.rstrip('/') + '/postgresql.auto.conf'
        backup = cls._snapshot_container_file(target, auto_conf, '.auto.conf')

        libraries = ', '.join(current + ['pg_stat_statements'])
        literal = "'" + libraries.replace("'", "''") + "'"
        res = cls._exec_sql(target, f'ALTER SYSTEM SET shared_preload_libraries = {literal};')
        if not res['success']:
            cls._restore_container_file(target, backup, auto_conf)
            raise DependencyUnavailableError(f"ALTER SYSTEM failed: {res['error']}")
        with open(os.path.join(cls._state_dir(target, create=True), 'applied.json'), 'w') as fh:
            json.dump({'auto_conf': auto_conf,
                       'settings': {'shared_preload_libraries': libraries}}, fh)

        result = cls._restart_and_verify(target, backup, auto_conf,
                                         {'shared_preload_libraries': libraries})
        created = cls._exec_sql(target, 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements;')
        if not created['success']:
            raise DependencyUnavailableError(
                f"Preloaded, but CREATE EXTENSION failed: {created.get('error')}")
        return result

    @classmethod
    def _restart_and_verify(cls, target, backup, container_path, settings):
        """Shared apply tail: restart, ping, roll back on failure."""
        container = target['container']
        res = cls._restart(container)
        if not res.get('success'):
            cls._restore_container_file(target, backup, container_path)
            raise DependencyUnavailableError(
                f"Container restart failed: {res.get('error')}; previous config restored")

        if not cls._wait_ready(target):
            cls._restore_container_file(target, backup, container_path)
            cls._restart(container)
            cls._wait_ready(target)
            raise DependencyUnavailableError(
                'Engine did not come back after the restart; '
                'previous config restored and container restarted again')

        return {'success': True, 'applied': settings, 'restarted': True,
                'backup': os.path.basename(backup)}

    @classmethod
    def rollback(cls, target):
        """Restore the most recent pre-apply config and restart the engine."""
        cls._guard(target)
        backup = cls._latest_backup(target)
        if not backup:
            raise NotFoundError('No previous configuration to roll back to')

        if target['engine'] == 'mysql':
            container_path = MYSQL_DROPIN_PATH
        else:
            applied = os.path.join(cls._state_dir(target), 'applied.json')
            try:
                with open(applied) as fh:
                    container_path = json.load(fh)['auto_conf']
            except (OSError, ValueError, KeyError):
                raise NotFoundError(
                    'Rollback metadata is missing; cannot locate postgresql.auto.conf')

        res = cls._restore_container_file(target, backup, container_path)
        if not res['success']:
            raise DependencyUnavailableError(
                f"Could not restore the previous config: {res['error']}")

        restart = cls._restart(target['container'])
        if not restart.get('success'):
            raise DependencyUnavailableError(
                f"Config restored but the restart failed: {restart.get('error')}")
        if not cls._wait_ready(target):
            raise DependencyUnavailableError(
                'Config restored but the engine did not come back; check the container logs')

        # Consume the backup so can_rollback reflects reality.
        try:
            os.remove(backup)
        except OSError:
            pass
        return {'success': True, 'restored': os.path.basename(backup), 'restarted': True}
