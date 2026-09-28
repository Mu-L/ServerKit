"""Connection properties an installed service offers to other apps (plan 86 §C1).

``fromService`` used to resolve full connection details only for a
``ManagedDatabase``. Any other sibling got a generic ``host``/``port``/``url``
that was always ``http://``, so "install Redis" and "my app uses Redis" had
nothing to do with each other. This module is what an installed service
*offers*:

* an **engine** template (one with an ``engine:`` block) gets defaults from its
  ``protocol``: a ``redis://`` / ``postgresql://`` / ``mysql://`` /
  ``mongodb://`` ``url`` plus the parts;
* any template may declare, or override, properties in a top-level
  ``connection:`` block, e.g. MinIO::

      connection:
        port: 9000                 # container port (optional; else from compose)
        properties:
          endpoint: "http://{host}:{port}"
          region: us-east-1

Placeholders: ``{host}`` ``{port}`` ``{user}`` ``{password}`` ``{database}``,
``{var.NAME}`` for any install variable, and ``_url`` forms
(``{password_url}``, ``{var_url.NAME}``) that are percent-encoded for use
inside a URL.

Reachability: services run as separate compose projects with their ports bound
to 127.0.0.1, so a container cannot reach another by name or by host port.
Both sides join one external docker network, ``SHARED_NETWORK``, through the
managed compose override (``ComposeEnvService``). On it, the host is the
service's container name (templates set ``container_name: ${APP_NAME}``) and
the port is the **container** port, not the published one. It is deliberately
not the proxy stack's ``serverkit`` network: the edge proxy has no business
reaching a datastore.

Secrets come from the install variables recorded when the service was
installed, never from reading the running container.
"""
import re
from typing import Dict, Optional, Tuple
from urllib.parse import quote

SHARED_NETWORK = 'serverkit-services'

# Defaults per engine protocol. A template's `connection.properties` wins.
_URL_PROTOCOLS = {
    'redis': 'redis://{userinfo}{host}:{port}/0',
    'postgresql': 'postgresql://{userinfo}{host}:{port}/{database}',
    'mysql': 'mysql://{userinfo}{host}:{port}/{database}',
    'mongodb': 'mongodb://{userinfo}{host}:{port}/{database}',
}

_PLACEHOLDER = re.compile(r'\{(\w+)(?:\.(\w+))?\}')


def _template_for(app) -> Optional[Dict]:
    from app.services.database_engine_service import app_template_id
    from app.services.template_service import TemplateService

    template_id = app_template_id(app)
    if not template_id:
        return None
    fetched = TemplateService.get_template(template_id)
    return fetched['template'] if fetched.get('success') else None


def _install_variables(app) -> Dict[str, str]:
    from app.services.database_engine_service import _install_info
    variables = (_install_info(app) or {}).get('variables') or {}
    return {str(k): '' if v is None else str(v) for k, v in variables.items()}


def container_port(template: Dict, port_var: Optional[str] = None) -> Optional[int]:
    """The container side of the service's published port.

    Prefers the mapping whose host side names ``port_var`` (``${PORT}:6379``),
    else the first mapping of the first service that publishes one.
    """
    services = ((template.get('compose') or {}).get('services') or {})
    candidates = []
    for svc in services.values():
        for spec in (svc or {}).get('ports') or []:
            text = str(spec.get('target') if isinstance(spec, dict) else spec)
            candidates.append(text)
    if port_var:
        preferred = [c for c in candidates if '${' + port_var + '}' in c]
        candidates = preferred + [c for c in candidates if c not in preferred]
    for text in candidates:
        tail = text.split(':')[-1].split('/')[0].strip().strip('"')
        if tail.isdigit():
            return int(tail)
    return None


class ServiceConnectionService:

    @classmethod
    def spec(cls, app) -> Optional[Dict]:
        """What ``app`` offers: ``{'host', 'port', 'properties': {name: value}}``,
        or ``None`` when it is not a connectable service."""
        template = _template_for(app)
        if not isinstance(template, dict):
            return None
        from app.services.template_service import TemplateService
        engine = TemplateService.engine_metadata(template) or {}
        declared = template.get('connection') if isinstance(template.get('connection'), dict) else None
        if not engine and not declared:
            return None
        declared = declared or {}

        variables = _install_variables(app)
        port = declared.get('port') or container_port(template, engine.get('port_var'))
        password = variables.get(engine.get('admin_password_var') or '', '')
        user = engine.get('admin_user') or ''
        database = variables.get(engine.get('database_var') or '', '')
        ctx = {
            'host': app.name,
            'port': '' if port is None else str(port),
            'user': user,
            'password': password,
            'database': database,
        }

        properties = {}
        protocol = engine.get('protocol')
        if protocol in _URL_PROTOCOLS:
            userinfo = ''
            if password:
                # Redis AUTH has no username in the default ACL user's URL form.
                name = '' if protocol == 'redis' else quote(user, safe='')
                userinfo = f'{name}:{quote(password, safe="")}@'
            url = _URL_PROTOCOLS[protocol].format(userinfo=userinfo, **ctx)
            properties.update({'url': url, 'connectionString': url,
                               'host': ctx['host'], 'port': ctx['port'],
                               'password': password})
            if protocol != 'redis':
                properties.update({'username': user, 'database': database})
        elif engine:
            properties.update({'host': ctx['host'], 'port': ctx['port'],
                               'password': password})

        if protocol == 'postgresql' and getattr(app, 'pooler_enabled', False):
            # Through the PgBouncer sidecar (plan 86 §D1); same credentials.
            from app.services.pooler_service import container_name
            pooled = _URL_PROTOCOLS['postgresql'].format(
                userinfo=userinfo, **dict(ctx, host=container_name(app), port='5432'))
            properties.update({'pooledUrl': pooled, 'pooledConnectionString': pooled})

        for name, raw in (declared.get('properties') or {}).items():
            properties[str(name)] = cls._render(str(raw), ctx, variables)
        return {'host': ctx['host'], 'port': ctx['port'], 'properties': properties}

    @staticmethod
    def _render(raw: str, ctx: Dict[str, str], variables: Dict[str, str]) -> str:
        def sub(match):
            key, var = match.group(1), match.group(2)
            if key in ('var', 'var_url') and var:
                value = variables.get(var, '')
                return quote(value, safe='') if key == 'var_url' else value
            if key.endswith('_url') and key[:-4] in ctx:
                return quote(ctx[key[:-4]], safe='')
            return ctx.get(key, match.group(0))
        return _PLACEHOLDER.sub(sub, raw)

    @classmethod
    def resolve(cls, app, prop: str) -> Tuple[Optional[str], Optional[str]]:
        """``(value, None)`` or ``(None, error)`` for one property of ``app``."""
        spec = cls.spec(app)
        if spec is None:
            return None, 'not a connectable service'
        if prop not in spec['properties']:
            offered = ', '.join(sorted(spec['properties'])) or 'none'
            return None, f'property `{prop}` not offered by `{app.name}` (offers: {offered})'
        return spec['properties'][prop], None

    @classmethod
    def is_connectable(cls, app) -> bool:
        try:
            return cls.spec(app) is not None
        except Exception:  # noqa: BLE001 - a broken template is just "no"
            return False

    @classmethod
    def consumes_services(cls, app) -> bool:
        """True when any of ``app``'s env vars references a connectable sibling
        through ``fromService`` — the app then needs the shared network."""
        from app.models import EnvironmentVariable
        from app.services.env_reference_service import EnvReferenceResolver

        for ev in EnvironmentVariable.query.filter_by(application_id=app.id).all():
            if not ev.value_from:
                continue
            ref = ev.get_reference() or {}
            if ref.get('kind') != 'service':
                continue
            sibling = EnvReferenceResolver.find_sibling_app(app, ref.get('service'))
            if sibling is not None and cls.is_connectable(sibling):
                return True
        return False

    @classmethod
    def has_attachments(cls, app) -> bool:
        """An app with attachments reaches its services by name (a Grafana data
        source, a bucket endpoint) even when no env reference names them."""
        from app.models.app_attachment import AppAttachment
        return AppAttachment.query.filter_by(app_id=app.id).first() is not None

    @classmethod
    def needs_shared_network(cls, app) -> bool:
        return (cls.is_connectable(app) or cls.consumes_services(app)
                or cls.has_attachments(app))
