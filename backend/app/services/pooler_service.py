"""Opt-in PgBouncer beside an installed PostgreSQL (plan 86 §D1).

Turning pooling on adds a ``pgbouncer`` service to the PostgreSQL app's
managed compose override: same project, container ``<app>-pooler``, on the
shared service network, no host port. Apps then choose per connection:
``fromService: <pg>.connectionString`` stays direct, and ``pooledUrl`` /
``pooledConnectionString`` go through the pooler.

Transaction pooling is the mode: many app connections share few server
connections. It breaks session state: session-level prepared statements in
some drivers, ``LISTEN``/``NOTIFY``, advisory locks and ``SET`` without
``LOCAL``. So nothing switches to the pooled URL on its own, and the UI says
so where the choice is made.
"""
from typing import Dict, Optional

POOLER_IMAGE = 'edoburu/pgbouncer:v1.24.1-p1'
POOLER_SERVICE = 'pgbouncer'
POOL_MODE = 'transaction'
MAX_CLIENT_CONN = 500
DEFAULT_POOL_SIZE = 20

TRADEOFF = ('Transaction pooling breaks session features: prepared statements in some '
            'drivers, LISTEN/NOTIFY, advisory locks and session SET. Apps keep the '
            'direct connection unless they switch to the pooled URL.')


def container_name(app) -> str:
    return f'{app.name}-pooler'


def is_postgres_engine(app) -> bool:
    from app.services.service_connection_service import _template_for
    from app.services.template_service import TemplateService
    engine = TemplateService.engine_metadata(_template_for(app) or {}) or {}
    return engine.get('protocol') == 'postgresql'


def sidecar(app, base_services: Dict) -> Optional[Dict]:
    """The compose service to add to ``app``'s override, or None."""
    if not getattr(app, 'pooler_enabled', False) or not base_services:
        return None
    if not is_postgres_engine(app):
        return None
    from app.services.service_connection_service import _install_variables, _template_for
    from app.services.template_service import TemplateService
    engine = TemplateService.engine_metadata(_template_for(app) or {}) or {}
    variables = _install_variables(app)
    password = variables.get(engine.get('admin_password_var') or '', '')
    if not password:
        return None
    primary = next(iter(base_services))
    return {
        'image': POOLER_IMAGE,
        'container_name': container_name(app),
        'restart': 'unless-stopped',
        'depends_on': [primary],
        'environment': {
            'DB_HOST': primary,
            'DB_PORT': '5432',
            'DB_USER': engine.get('admin_user') or 'postgres',
            'DB_PASSWORD': password,
            'POOL_MODE': POOL_MODE,
            'AUTH_TYPE': 'scram-sha-256',
            'MAX_CLIENT_CONN': str(MAX_CLIENT_CONN),
            'DEFAULT_POOL_SIZE': str(DEFAULT_POOL_SIZE),
            'LISTEN_PORT': '5432',
        },
    }


def set_enabled(app, enabled: bool) -> Dict:
    """Save the flag and apply it: `compose up` adds or removes the sidecar
    (the PostgreSQL container itself is unchanged, so it keeps running)."""
    from app import db
    from app.services.docker_service import DockerService

    app.pooler_enabled = bool(enabled)
    db.session.commit()
    applied = None
    if app.root_path and not app.server_id:
        args = {'compose_file': app.compose_file} if app.compose_file else {}
        up = DockerService.compose_up(app.root_path, detach=True, **args)
        applied = bool(up.get('success'))
        if applied and not enabled:
            # `up` leaves a service dropped from the override running.
            DockerService.run(['rm', '-f', container_name(app)], timeout=60)
    return {'enabled': bool(app.pooler_enabled), 'applied': applied,
            'host': container_name(app), 'tradeoff': TRADEOFF}
