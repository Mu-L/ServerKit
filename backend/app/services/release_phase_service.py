"""Pre-switch phases of a slot deploy: DB snapshot and release command (plan 87 §D).

Both slots talk to the same database, so anything that changes it runs
exactly once, BEFORE the switch, while the old release still serves:

* **Snapshot** — every managed database the app owns gets a dump through the
  backup service. It is the safety net for a migration that turns out to be
  destructive. It never blocks: a failed or skipped snapshot is a warning.
* **Release** — the Procfile ``release:`` line (or ``release`` in
  ``serverkit.yaml``, or the app's own setting) runs once in a one-off
  container of the NEW image with the app's env. A non-zero exit aborts the
  deploy with the live slot untouched.

Rolling back code never rolls back the database. :func:`restore_databases`
is the explicit second step the UI offers, with a data-loss warning.
"""
import logging
import os
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

RELEASE_TIMEOUT = 1800
MANIFEST_NAMES = ('serverkit.yaml', 'serverkit.yml')


def _log(log, line):
    logger.info(line)
    if log:
        try:
            log(line)
        except Exception:  # noqa: BLE001
            pass


def resolve_release_command(app) -> Tuple[Optional[str], Optional[str]]:
    """``(command, source)``: the app setting, then the Procfile, then serverkit.yaml."""
    from app.services import deploy_settings
    from app.services.worker_process_service import parse_procfile

    configured = (deploy_settings.get(app, 'release_command') or '').strip()
    if configured:
        return configured, 'settings'
    root = getattr(app, 'root_path', None)
    if not root:
        return None, None
    try:
        with open(os.path.join(root, 'Procfile'), encoding='utf-8') as fh:
            command = parse_procfile(fh.read()).get('release')
        if command:
            return command, 'Procfile'
    except OSError:
        pass
    for name in MANIFEST_NAMES:
        try:
            import yaml
            with open(os.path.join(root, name), encoding='utf-8') as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, ValueError, Exception):  # noqa: BLE001 - unreadable manifest = none
            continue
        if not isinstance(data, dict):
            continue
        deploy = data.get('deploy') if isinstance(data.get('deploy'), dict) else {}
        command = data.get('release') or deploy.get('release')
        if isinstance(command, str) and command.strip():
            return command.strip(), name
    return None, None


def run_release(app, image: str, env: Dict, volumes: List[str], command: str,
                log: Optional[Callable[[str], None]] = None) -> Dict:
    """Run ``command`` once in a throwaway container of ``image``."""
    from app.services.docker_service import DockerService
    from app.services.service_connection_service import SHARED_NETWORK, ServiceConnectionService

    name = f'serverkit-release-{app.id}'
    args = ['run', '--rm', '--name', name]
    for key, value in (env or {}).items():
        args.extend(['-e', f'{key}={value}'])
    for volume in volumes or []:
        args.extend(['-v', volume])
    try:
        if ServiceConnectionService.needs_shared_network(app):
            DockerService.ensure_network(SHARED_NETWORK)
            args.extend(['--network', SHARED_NETWORK])
    except Exception as exc:  # noqa: BLE001 - reach the DB the way the app does, or try anyway
        logger.warning('release network setup for app %s failed: %s', app.id, exc)
    args.extend([image, 'sh', '-c', command])
    _log(log, f'Running release command in a one-off container: {command}')
    # A leftover from a killed run would make `--name` collide.
    DockerService.run(['rm', '-f', name], timeout=30)
    result = DockerService.run(args, timeout=RELEASE_TIMEOUT)
    for line in (result.get('output') or '').splitlines()[-40:]:
        _log(log, line)
    if not result.get('success'):
        return {'success': False, 'error': result.get('error') or 'release command failed'}
    return {'success': True}


def owned_databases(app):
    from app.models.managed_database import ManagedDatabase
    return ManagedDatabase.query.filter_by(owner_application_id=app.id).all()


def snapshot_databases(app, deployment, log=None) -> List[Dict]:
    """Dump every managed DB the app owns; record them on the deployment."""
    from app import db
    from app.services import deploy_settings
    from app.services.backup_service import BackupService
    from app.services.managed_database_service import ManagedDatabaseService

    databases = owned_databases(app)
    if not databases:
        return []
    if not deploy_settings.get(app, 'snapshot_databases'):
        _log(log, 'Warning: pre-deploy database snapshot is turned off for this app.')
        return []
    taken = []
    for managed in databases:
        descriptor = ManagedDatabaseService.backup_descriptor(managed)
        _log(log, f'Snapshotting database {managed.name} before the release...')
        try:
            result = BackupService.backup_database(**descriptor)
        except Exception as exc:  # noqa: BLE001 - a snapshot never blocks
            result = {'success': False, 'error': str(exc)}
        if result.get('success'):
            taken.append({'managed_id': managed.id, 'name': managed.name,
                          'engine': managed.engine, 'path': result.get('path'),
                          'taken_at': datetime.utcnow().isoformat()})
        else:
            _log(log, f"Warning: snapshot of {managed.name} failed ({result.get('error')}); "
                      'continuing without it.')
    if taken:
        deployment.update_metadata('db_snapshots', taken)
        db.session.commit()
    return taken


def restore_databases(app, deployment) -> Dict:
    """Restore the snapshots taken before ``deployment`` ran. Destroys every
    write made since; the UI asks first."""
    from app.models.managed_database import ManagedDatabase
    from app.services.backup_service import BackupService
    from app.services.managed_database_service import ManagedDatabaseService

    snapshots = (deployment.get_metadata() or {}).get('db_snapshots') or []
    if not snapshots:
        return {'success': False, 'error': 'That deployment has no database snapshot.'}
    restored, failed = [], {}
    for snap in snapshots:
        managed = ManagedDatabase.query.get(snap.get('managed_id'))
        if managed is None or managed.owner_application_id != app.id:
            failed[snap.get('name')] = 'database no longer belongs to this app'
            continue
        d = ManagedDatabaseService.backup_descriptor(managed)
        result = BackupService.restore_database(snap.get('path'), d['db_type'], d['db_name'],
                                                user=d['user'], password=d['password'],
                                                host=d['host'])
        if result.get('success'):
            restored.append(managed.name)
        else:
            failed[managed.name] = result.get('error') or 'restore failed'
    return {'success': not failed, 'restored': restored, 'failed': failed,
            **({'error': '; '.join(f'{k}: {v}' for k, v in failed.items())} if failed else {})}


def restorable(app) -> Optional[Dict]:
    """The release whose migration may still be in the database: the newest
    deployment that ran its release command and is no longer serving (switched
    back, reverted, or failed after the release) and carries a snapshot. None
    once a later deploy has gone live on top of it."""
    from app.models.deployment import Deployment
    live = Deployment.get_current(app.id)
    newest = None
    for dep in (Deployment.query.filter(Deployment.app_id == app.id,
                                        Deployment.status.in_(('rolled_back', 'failed')))
                .order_by(Deployment.id.desc()).limit(10).all()):
        meta = dep.get_metadata() or {}
        if meta.get('release_ran') and meta.get('db_snapshots') and not meta.get('db_restored_at'):
            newest = dep
            break
    if newest is None:
        return None
    if live and live.id > newest.id and not (live.get_metadata() or {}).get('rolled_back_to'):
        return None
    snapshots = newest.get_metadata()['db_snapshots']
    return {'deployment_id': newest.id, 'version': newest.version,
            'taken_at': snapshots[0].get('taken_at'),
            'databases': [s.get('name') for s in snapshots]}

