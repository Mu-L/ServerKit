"""Resolve manifest env references at injection time (plan 17, Phase 3).

`fromSecret` -> a vault secret's value; `fromService` -> a sibling service's
connection property. The resolved value never lands in the env-var row — it is
computed here whenever the effective env is built, so rotating a vault secret or
changing a db password propagates on the next deploy/restart.
"""

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# fromService properties by kind
_DB_PROPERTIES = ('connectionString', 'host', 'port', 'database', 'username', 'password')
_APP_PROPERTIES = ('host', 'port', 'url')


class EnvReferenceResolver:

    @classmethod
    def resolve(cls, app, ref: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """Resolve a reference for ``app``. Returns (value, error)."""
        if not isinstance(ref, dict):
            return None, 'invalid reference'
        kind = ref.get('kind')
        if kind == 'secret':
            return cls._resolve_secret(ref.get('secret'))
        if kind == 'service':
            return cls._resolve_service(app, ref.get('service'), ref.get('property'))
        if kind == 'server':
            return cls._resolve_server(app, ref.get('property'))
        return None, f'unknown reference kind: {kind}'

    # -- fromServer (appliance tier, plan 35) -------------------------------

    @classmethod
    def _resolve_server(cls, app, prop: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """The service's own advertised identity — the WebRTC/NAT need. Resolves
        against the app's target server, or the panel host when unassigned."""
        prop = prop or 'publicIp'
        server_id = getattr(app, 'server_id', None)
        if server_id:
            from app.models.server import Server
            srv = Server.query.get(server_id)
            if not srv:
                return None, 'target server not found'
            if prop == 'publicIp':
                return (srv.ip_address, None) if srv.ip_address \
                    else (None, 'target server has no recorded IP')
            if prop == 'hostname':
                return (getattr(srv, 'hostname', None) or srv.name or None), None
            return None, f'unknown server property `{prop}`'
        # panel host
        if prop == 'publicIp':
            from app.services.site_domain_service import SiteDomainService
            ip = SiteDomainService.server_ip()
            return (ip, None) if ip else (None, 'panel host public IP is not configured')
        if prop == 'hostname':
            import socket
            return socket.gethostname(), None
        return None, f'unknown server property `{prop}`'

    # -- fromSecret ---------------------------------------------------------

    @classmethod
    def _resolve_secret(cls, name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        if not name:
            return None, 'missing secret name'
        from app.models.secret_vault import Secret
        # a bare name resolves against any vault; a "vault/name" form scopes it
        vault_slug = None
        secret_name = name
        if '/' in name:
            vault_slug, secret_name = name.split('/', 1)
        q = Secret.query.filter_by(name=secret_name)
        if vault_slug:
            from app.models.secret_vault import SecretVault
            vault = SecretVault.query.filter_by(slug=vault_slug).first()
            if not vault:
                return None, f'vault `{vault_slug}` not found'
            q = q.filter_by(vault_id=vault.id)
        secret = q.order_by(Secret.id).first()
        if not secret:
            return None, f'secret `{name}` not found'
        try:
            return secret.value, None
        except Exception as exc:  # decryption failure
            return None, f'secret `{name}` unreadable: {exc}'

    @classmethod
    def secret_exists(cls, name: Optional[str]) -> bool:
        value, error = cls._resolve_secret(name)
        return error is None

    # -- fromService --------------------------------------------------------

    @classmethod
    def _resolve_service(cls, app, service_name: Optional[str],
                         prop: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        if not service_name or not prop:
            return None, 'missing service/property'

        # db sibling first (ManagedDatabase by name within the workspace)
        from app.models.managed_database import ManagedDatabase
        managed = ManagedDatabase.query_active().filter_by(name=service_name).first()
        if managed is not None:
            return cls._db_property(managed, prop)

        sibling = cls.find_sibling_app(app, service_name)
        if sibling is not None:
            # An installed engine / connection-declaring template offers real
            # connection properties (redis:// url, S3 endpoint...) reachable
            # over the shared network (plan 86 §C1); a plain app keeps the
            # generic host/port/url.
            from app.services.service_connection_service import ServiceConnectionService
            if ServiceConnectionService.is_connectable(sibling):
                return ServiceConnectionService.resolve(sibling, prop)
            return cls._app_property(sibling, service_name, prop)

        return None, f'service `{service_name}` not found'

    @classmethod
    def find_sibling_app(cls, app, service_name: Optional[str]):
        """The live Application ``service_name`` names: in ``app``'s project
        first, then anywhere. Name-keyed, so live rows only — a tombstone
        sharing the name would shadow the live app and the wrong app's
        host/port/secrets would get baked into a live container's env."""
        if not service_name:
            return None
        from app.models.application import Application
        project_id = getattr(app, 'project_id', None)
        sibling = None
        if project_id is not None:
            sibling = Application.query_active().filter_by(project_id=project_id, name=service_name).first()
        if sibling is None:
            sibling = Application.query_active().filter_by(name=service_name).first()
        return sibling

    @classmethod
    def _db_property(cls, managed, prop: str) -> Tuple[Optional[str], Optional[str]]:
        from app.services.managed_database_service import ManagedDatabaseService
        if prop == 'connectionString':
            return ManagedDatabaseService.build_connection_uri(managed, reveal=True), None
        if prop == 'host':
            return managed.host, None
        if prop == 'port':
            return str(managed.effective_port()), None
        if prop == 'database':
            return managed.name, None
        if prop == 'username':
            return managed.admin_username or '', None
        if prop == 'password':
            return (managed.admin_secret or ''), None
        return None, f'property `{prop}` not valid for a database service'

    @classmethod
    def _app_property(cls, sibling, service_name: str, prop: str) -> Tuple[Optional[str], Optional[str]]:
        # in-cluster addressing: the service name resolves on the compose/docker network
        host = service_name
        port = sibling.port
        if prop == 'host':
            return host, None
        if prop == 'port':
            return str(port) if port else '', None
        if prop == 'url':
            return f'http://{host}:{port}' if port else f'http://{host}', None
        return None, f'property `{prop}` not valid for an app service'
