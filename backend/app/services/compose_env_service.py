"""Inject a managed app's effective environment into its Compose services.

Single-container apps get their env via ``docker run -e`` (see
``EnvService.get_effective_env`` + ``DeploymentService._deploy_docker``). Compose
apps instead get container env from the compose file's ``environment:`` block, so
to make shared variable groups + the app's own local env vars reach a compose
app's containers we write a **managed override compose file**
(``docker-compose.serverkit.yml``) next to the app's base compose. The override
adds an ``environment:`` overlay to every service; Docker Compose merges it on top
of the base (override wins on key collisions), so the effective env is
authoritative — matching the single-container behaviour. The override also
carries per-app resource limits and a ``restart: unless-stopped`` default for
services that don't set their own restart policy, so app containers survive a
reboot / daemon restart.

This is non-destructive: ServerKit owns only the override file and never edits the
user's / template's base compose. The override is regenerated on every
``compose up`` / ``compose restart`` and removed when the app has no effective env,
so it never goes stale. Everything here is best-effort and guarded — a failure to
build the overlay must never block a deploy; the caller simply falls back to the
plain base compose.
"""
import logging
import os

import yaml

from app.services.service_connection_service import SHARED_NETWORK

logger = logging.getLogger(__name__)


class ComposeEnvService:
    """Writes/owns the ServerKit env override for a managed compose app."""

    OVERRIDE_NAME = 'docker-compose.serverkit.yml'
    # Conventional base compose filenames, in the order Docker Compose itself
    # prefers them during auto-discovery.
    BASE_CANDIDATES = (
        'docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml',
    )

    @classmethod
    def find_base_compose(cls, project_path, compose_file=None):
        """Return the base compose file to use (a name/path), or None.

        When ``compose_file`` is given it's authoritative; otherwise discover a
        conventional base file in ``project_path``.
        """
        if compose_file:
            return compose_file
        if not project_path:
            return None
        for name in cls.BASE_CANDIDATES:
            if os.path.exists(os.path.join(project_path, name)):
                return name
        return None

    @classmethod
    def override_path(cls, project_path):
        return os.path.join(project_path, cls.OVERRIDE_NAME)

    @classmethod
    def list_services(cls, app):
        """Service names declared in a managed compose app's base compose.

        Used by the UI to offer a per-service targeting choice. Returns [] for
        non-compose apps or when the base compose can't be read.
        """
        try:
            root = getattr(app, 'root_path', None)
            if not root or not os.path.isdir(root):
                return []
            base = cls.find_base_compose(root, getattr(app, 'compose_file', None))
            if not base:
                return []
            base_path = base if os.path.isabs(base) else os.path.join(root, base)
            return cls._service_names(base_path)
        except Exception:  # pragma: no cover - defensive
            return []

    @staticmethod
    def _app_for_project(project_path):
        """The managed Application whose root_path is this project dir, if any."""
        try:
            from app.models.application import Application
            # Path-keyed: a tombstone whose root_path a new app reused would
            # shadow the live app and the overlay would be written for the wrong one.
            return Application.query_active().filter_by(root_path=project_path).first()
        except Exception as e:  # pragma: no cover - DB guard
            logger.debug('compose overlay app lookup failed for %s: %s', project_path, e)
            return None

    @staticmethod
    def _services(base_compose_path):
        """Services declared in the base compose as {name: service-dict}
        (empty dict on any error)."""
        try:
            with open(base_compose_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            services = data.get('services') or {}
            if isinstance(services, dict):
                return services
        except Exception as e:
            logger.debug('could not read services from %s: %s', base_compose_path, e)
        return {}

    @classmethod
    def _service_names(cls, base_compose_path):
        """Service names declared in the base compose (empty list on any error)."""
        return list(cls._services(base_compose_path).keys())

    @staticmethod
    def _limits_block(app):
        """Compose keys for the app's resource limits, or {} when unset/invalid."""
        block = {}
        try:
            cpu = getattr(app, 'cpu_limit', None)
            if cpu:
                block['cpus'] = float(cpu)
            mem = getattr(app, 'memory_limit', None)
            if mem:
                block['mem_limit'] = str(mem)
        except (TypeError, ValueError):
            return {}
        return block

    @staticmethod
    def _escape(value):
        """Escape a value for a compose ``environment:`` entry.

        Compose performs ``${VAR}`` interpolation on environment values, so a
        literal ``$`` must be doubled to survive intact.
        """
        return str(value).replace('$', '$$')

    @classmethod
    def render_override(cls, project_path, compose_file=None):
        """Compute the override that *should* exist for a project dir, without
        touching disk. Extracted from :meth:`refresh_for_project` so drift
        detection can render the expected file content in-memory.

        Returns ``{'applies': bool, 'path': str|None, 'content': str|None}``:
        ``applies=False`` when this dir isn't a managed compose app (leave it
        alone); ``content=None`` (with ``applies=True``) when no override should
        exist. May raise — :meth:`refresh_for_project` wraps it.
        """
        if not project_path or not os.path.isdir(project_path):
            return {'applies': False, 'path': None, 'content': None}

        app = cls._app_for_project(project_path)
        if app is None:
            # Not a managed app dir (e.g. a proxy stack) — leave it alone.
            return {'applies': False, 'path': None, 'content': None}

        base = cls.find_base_compose(project_path, compose_file)
        if not base:
            return {'applies': False, 'path': None, 'content': None}
        override_path = cls.override_path(project_path)
        base_path = base if os.path.isabs(base) else os.path.join(project_path, base)
        services = cls._services(base_path)
        service_names = list(services.keys())
        if not service_names:
            return {'applies': True, 'path': override_path, 'content': None}

        from app.services.env_service import EnvService
        # Per-service effective env: a variable lands on every service unless
        # it targets a specific one (EnvironmentVariable/SharedVariable
        # .target_service). Local env vars override shared variable groups.
        per_service = EnvService.get_effective_env_for_services(app.id, service_names)

        services_block = {}
        for name in service_names:
            svc_env = (per_service or {}).get(name) or {}
            if not svc_env:
                continue  # nothing targeted at this service
            services_block[name] = {
                'environment': {k: cls._escape(v) for k, v in svc_env.items() if k}
            }

        # Per-app resource limits (task #23): cap the app's primary
        # (first-declared) compose service. mem_limit/cpus apply with plain
        # (non-swarm) docker compose.
        limits = cls._limits_block(app)
        if limits:
            services_block.setdefault(service_names[0], {}).update(limits)

        # Restart policy: services without an explicit `restart:` get
        # `unless-stopped` so an app's containers come back after a reboot or
        # Docker daemon restart (a service that sets its own policy — even
        # `restart: "no"` — is left alone).
        for name, svc in services.items():
            if not (isinstance(svc, dict) and svc.get('restart')):
                services_block.setdefault(name, {})['restart'] = 'unless-stopped'

        # Shared service network (plan 86 §C1): an app that offers connection
        # properties (Redis, MinIO...) or consumes one through `fromService`
        # joins SHARED_NETWORK so the two can reach each other by container
        # name. `default` is listed too — naming any network on a service
        # otherwise drops its implicit default one. Services with a
        # `network_mode` can't join networks and are left alone.
        joined = cls._shared_network_services(app, services)
        for name in joined:
            services_block.setdefault(name, {})['networks'] = {
                'default': {}, SHARED_NETWORK: {}}

        # Opt-in PgBouncer beside a PostgreSQL engine (plan 86 §D1): a whole
        # extra service, reachable from apps over the shared network only.
        from app.services import pooler_service
        pooler = pooler_service.sidecar(app, services)
        if pooler:
            pooler['networks'] = {'default': {}, SHARED_NETWORK: {}}
            pooler['environment'] = {k: cls._escape(v) for k, v in pooler['environment'].items()}
            services_block[pooler_service.POOLER_SERVICE] = pooler
            joined = joined or [pooler_service.POOLER_SERVICE]

        if not services_block:
            # Nothing to inject → no override should exist.
            return {'applies': True, 'path': override_path, 'content': None}
        override = {'services': services_block}
        if joined:
            override['networks'] = {
                SHARED_NETWORK: {'external': True, 'name': SHARED_NETWORK}}

        header = (
            '# Managed by ServerKit — do not edit.\n'
            '# Injects the app\'s effective environment (shared variable groups\n'
            '# under the app\'s own local env vars) into every compose service,\n'
            '# plus any per-app resource limits on the primary service, a\n'
            '# `restart: unless-stopped` default for services without one, and\n'
            '# the shared service network when the app offers or uses one.\n'
            '# Regenerated on every deploy; delete it and it will be recreated.\n'
        )
        content = header + yaml.safe_dump(override, default_flow_style=False, sort_keys=True)
        return {'applies': True, 'path': override_path, 'content': content}

    @staticmethod
    def _shared_network_services(app, services):
        """Service names that join SHARED_NETWORK, or [] when the app neither
        offers nor consumes a connectable service."""
        try:
            from app.services.service_connection_service import ServiceConnectionService
            if not ServiceConnectionService.needs_shared_network(app):
                return []
        except Exception as e:  # noqa: BLE001 - never block the overlay
            logger.debug('shared-network check failed for app %s: %s', app.id, e)
            return []
        return [name for name, svc in services.items()
                if not (isinstance(svc, dict) and svc.get('network_mode'))]

    @classmethod
    def refresh_for_project(cls, project_path, compose_file=None):
        """Regenerate (or remove) the env override for a project dir.

        Returns the absolute override path when one was written, else None.
        Best-effort and fully guarded — never raises.
        """
        try:
            spec = cls.render_override(project_path, compose_file)
            if not spec['applies']:
                return None
            if spec['content'] is None:
                # Nothing to inject → make sure no stale override lingers.
                cls._remove_override(project_path)
                return None
            with open(spec['path'], 'w', encoding='utf-8') as f:
                f.write(spec['content'])
            if SHARED_NETWORK in spec['content']:
                # External networks must exist before `compose up`.
                from app.services.docker_service import DockerService
                DockerService.ensure_network(SHARED_NETWORK)
            return spec['path']
        except Exception as e:  # pragma: no cover - defensive
            logger.warning('compose env overlay refresh failed for %s: %s', project_path, e)
            return None

    @classmethod
    def _remove_override(cls, project_path):
        try:
            path = cls.override_path(project_path)
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:  # pragma: no cover - defensive
            logger.debug('could not remove stale compose override: %s', e)
