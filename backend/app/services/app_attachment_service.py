"""Attach a service an app uses: storage, cache or queue (plan 86 §C2/§C4).

Cache and queue attachments are env references only: the app gets the
service's C1 ``url`` through ``fromService`` under the names its libraries
read (``REDIS_URL``, ``AMQP_URL``/``BROKER_URL``). Storage is the one kind that
provisions something, described below.

"Attach storage" gives an app its own bucket on an installed Garage and a key
that can read and write that bucket only (never the admin token, never another
bucket). The app receives it through the same env-reference doors everything
else uses:

  * ``S3_ENDPOINT`` / ``S3_REGION`` → ``fromService`` (C1 properties), reached
    over the shared service network;
  * ``S3_BUCKET`` / ``S3_ACCESS_KEY_ID`` → plain values;
  * ``S3_SECRET_ACCESS_KEY`` → ``fromSecret``, stored encrypted in an internal
    vault. The ``AWS_*`` names most S3 SDKs read resolve the same way.

Nothing is redeployed here; the caller decides (the new env reaches the app on
its next deploy or restart). Detaching deletes the key, its secret and the env
vars the attach wrote, and keeps the bucket and its objects. Data is never
deleted by a detach.
"""
import re
from typing import Dict, Tuple

from app import db

VAULT_SLUG = 'serverkit-attachments'
VAULT_NAME = 'Service attachments'


class AttachmentError(Exception):
    pass


def _bucket_name(app_name: str) -> str:
    """S3 bucket naming: 3-63 chars of [a-z0-9-], starting and ending alnum."""
    name = re.sub(r'[^a-z0-9-]+', '-', (app_name or '').lower()).strip('-')
    name = re.sub(r'-{2,}', '-', name)[:63].strip('-')
    return name if len(name) >= 3 else f'app-{name}'.strip('-')


def _storage_env(service_name: str, bucket: str, access_key_id: str,
                 secret_ref: str) -> Dict[str, Tuple[str, object]]:
    """key -> ('ref', reference) | ('value', text)."""
    endpoint = ('ref', {'kind': 'service', 'service': service_name, 'property': 'endpoint'})
    region = ('ref', {'kind': 'service', 'service': service_name, 'property': 'region'})
    secret = ('ref', {'kind': 'secret', 'secret': secret_ref})
    return {
        'S3_ENDPOINT': endpoint, 'AWS_ENDPOINT_URL': endpoint,
        'S3_REGION': region, 'AWS_REGION': region,
        'S3_BUCKET': ('value', bucket),
        'S3_ACCESS_KEY_ID': ('value', access_key_id),
        'AWS_ACCESS_KEY_ID': ('value', access_key_id),
        'S3_SECRET_ACCESS_KEY': secret, 'AWS_SECRET_ACCESS_KEY': secret,
    }


# kind -> [(offers(template, engine) -> bool, {env key: source})]. A source is
# the service property to reference, or '=literal' for a fixed value. The env
# depends on what was attached: a Redis used as a queue is still only a
# BROKER_URL, never an AMQP_URL.
def _is_redis(template, engine):
    return engine.get('protocol') == 'redis'


def _is_amqp(template, engine):
    return template.get('id') == 'rabbitmq'


def _is_otel_collector(template, engine):
    return template.get('id') == 'otel-collector'


def _is_jaeger(template, engine):
    return template.get('id') == 'jaeger'


def _is_prometheus(template, engine):
    return template.get('id') == 'prometheus'


def _is_loki(template, engine):
    return template.get('id') == 'loki'


# Kinds that provision a data source on a Grafana consumer (plan 86 §A4)
# instead of writing env; which service types each accepts.
GRAFANA_KINDS = {'metrics': _is_prometheus, 'logs': _is_loki}


CONNECTION_KINDS = {
    'cache': [(_is_redis, {'REDIS_URL': 'url'})],
    'queue': [(_is_amqp, {'AMQP_URL': 'url', 'BROKER_URL': 'url'}),
              (_is_redis, {'BROKER_URL': 'url'})],
    # The standard OpenTelemetry SDK variables (plan 86 §A4).
    'tracing': [(_is_otel_collector, {'OTEL_EXPORTER_OTLP_ENDPOINT': 'otlpEndpoint',
                                      'OTEL_EXPORTER_OTLP_PROTOCOL': '=http/protobuf'})],
    # Collector -> trace store; read by the collector template's config.
    'traces': [(_is_jaeger, {'TRACES_OTLP_ENDPOINT': 'otlpGrpc'})],
}

# Kinds only one consumer template can take.
CONSUMER_ONLY = {'traces': 'otel-collector'}

# One line per kind, shown where the operator decides (plan 86 §C4).
FAILURE_MODES = {
    'storage': 'Objects outlive the app: detaching keeps the bucket and its data.',
    'cache': 'Cached data can be stale; invalidate on write, and never keep the only copy in a cache.',
    'queue': 'A job can be delivered twice; make jobs safe to run again.',
    'metrics': 'Grafana shows what Prometheus kept: past its retention, history is gone.',
    'logs': 'Grafana shows what Loki kept: past its retention, logs are gone.',
    'tracing': 'Spans are sampled and batched: a trace can be partial, and a crash can lose the last batch.',
    'traces': 'Jaeger keeps traces on its own disk; losing that volume loses the history.',
}


class AppAttachmentService:

    @staticmethod
    def live_app(app_id):
        from app.models.application import Application
        return Application.query_active().filter_by(id=app_id).first()

    @staticmethod
    def list_for_app(app):
        from app.models.app_attachment import AppAttachment
        return AppAttachment.query.filter_by(app_id=app.id).order_by(AppAttachment.id).all()

    @staticmethod
    def get(app, attachment_id):
        from app.models.app_attachment import AppAttachment
        return AppAttachment.query.filter_by(id=attachment_id, app_id=app.id).first()

    @staticmethod
    def _provisioner(service_app):
        from app.services.service_connection_service import _template_for
        template = _template_for(service_app) or {}
        connection = template.get('connection') if isinstance(template.get('connection'), dict) else {}
        return connection.get('provisioner')

    @classmethod
    def storage_services(cls):
        """Installed services that can hand out per-app buckets."""
        from app.models.application import Application
        return [a for a in Application.query_active().order_by(Application.name).all()
                if cls._provisioner(a) == 'garage']

    @staticmethod
    def _connection_env(service_app, kind):
        """``{env key: source}`` ``service_app`` fills for ``kind``, or None
        when it does not offer that kind."""
        from app.services.service_connection_service import _template_for
        from app.services.template_service import TemplateService
        template = _template_for(service_app) or {}
        engine = TemplateService.engine_metadata(template) or {}
        for offers, env in CONNECTION_KINDS.get(kind, []):
            if offers(template, engine):
                return env
        return None

    @staticmethod
    def _template_and_engine(app_row):
        from app.services.service_connection_service import _template_for
        from app.services.template_service import TemplateService
        template = _template_for(app_row) or {}
        return template, TemplateService.engine_metadata(template) or {}

    @classmethod
    def kinds_for(cls, app_row):
        """What ``app_row`` can attach: every app takes cache / storage / queue;
        a Grafana install also takes metrics and logs data sources."""
        kinds = ['cache', 'storage', 'queue', 'tracing']
        template_id = cls._template_and_engine(app_row)[0].get('id')
        if template_id == 'grafana':
            kinds += list(GRAFANA_KINDS)
        kinds += [k for k, only in CONSUMER_ONLY.items() if only == template_id]
        return kinds

    @classmethod
    def is_grafana(cls, app_row):
        return cls._template_and_engine(app_row)[0].get('id') == 'grafana'

    @classmethod
    def services_for(cls, kind):
        """Installed services an app can attach as ``kind``."""
        if kind == 'storage':
            return cls.storage_services()
        if kind in GRAFANA_KINDS:
            from app.models.application import Application
            return [a for a in Application.query_active().order_by(Application.name).all()
                    if GRAFANA_KINDS[kind](*cls._template_and_engine(a))]
        from app.models.application import Application
        return [a for a in Application.query_active().order_by(Application.name).all()
                if cls._connection_env(a, kind)]

    @classmethod
    def attach(cls, app, service_app, kind, user_id=None):
        if kind == 'storage':
            return cls.attach_storage(app, service_app, user_id=user_id)
        if kind in GRAFANA_KINDS:
            return cls.attach_grafana_source(app, service_app, kind)
        if kind not in CONNECTION_KINDS:
            raise AttachmentError(f'Unknown attachment kind: {kind}')
        return cls.attach_connection(app, service_app, kind, user_id=user_id)

    @classmethod
    def attach_connection(cls, app, service_app, kind, user_id=None):
        """Point the app's env at ``service_app``'s URL for ``kind``."""
        from app.models.app_attachment import AppAttachment
        from app.services.env_service import EnvService

        if service_app.id == app.id:
            raise AttachmentError('A service cannot be attached to itself')
        only = CONSUMER_ONLY.get(kind)
        if only and cls._template_and_engine(app)[0].get('id') != only:
            raise AttachmentError(f'Only a {only} install can attach {kind}')
        env = cls._connection_env(service_app, kind)
        if not env:
            raise AttachmentError(f'{service_app.name} cannot be used as a {kind}')
        existing = AppAttachment.query.filter_by(app_id=app.id, kind=kind).first()
        if existing is not None:
            if existing.service_app_id != service_app.id:
                raise AttachmentError(f'This app already has a {kind} attached; detach it first')
            return existing, False
        for env_key, source in env.items():
            if source.startswith('='):
                _v, _c, error = EnvService.set_env_var(app.id, env_key, source[1:], user_id=user_id)
            else:
                ref = {'kind': 'service', 'service': service_app.name, 'property': source}
                _v, _c, error = EnvService.set_env_reference(app.id, env_key, ref, user_id=user_id)
            if error:
                raise AttachmentError(f'Could not set {env_key}: {error}')
        row = AppAttachment(app_id=app.id, service_app_id=service_app.id, kind=kind)
        row.details = {'env_keys': list(env)}
        db.session.add(row)
        db.session.commit()
        return row, True

    # -- storage ---------------------------------------------------------------

    @classmethod
    def attach_storage(cls, app, service_app, user_id=None, admin=None):
        """Bucket + scoped key for ``app`` on ``service_app``. Idempotent: an
        app already attached to the same service gets its existing row back."""
        from app.models.app_attachment import AppAttachment
        from app.services.env_service import EnvService
        from app.services.garage_admin import GarageAdmin, GarageError

        if service_app.id == app.id:
            raise AttachmentError('A service cannot be attached to itself')
        if cls._provisioner(service_app) != 'garage':
            raise AttachmentError(f'{service_app.name} is not an object storage service')
        existing = AppAttachment.query.filter_by(app_id=app.id, kind='storage').first()
        if existing is not None:
            if existing.service_app_id != service_app.id:
                raise AttachmentError('This app already has storage attached; detach it first')
            return existing, False

        admin = admin or GarageAdmin(service_app.name)
        bucket = _bucket_name(app.name)
        try:
            admin.ensure_layout()
            bucket_id = admin.ensure_bucket(bucket)
            key = admin.create_key(f'serverkit-{app.name}')
            admin.allow(bucket_id, key['access_key_id'])
        except GarageError as exc:
            raise AttachmentError(f'Storage setup failed: {exc}') from exc

        secret_name = f'{app.name}.s3-secret-key'
        cls._store_secret(secret_name, key['secret_access_key'], user_id)

        env = _storage_env(service_app.name, bucket, key['access_key_id'],
                           f'{VAULT_SLUG}/{secret_name}')
        for env_key, (mode, value) in env.items():
            if mode == 'ref':
                _v, _c, error = EnvService.set_env_reference(app.id, env_key, value, user_id=user_id)
            else:
                _v, _c, error = EnvService.set_env_var(app.id, env_key, value, user_id=user_id)
            if error:
                raise AttachmentError(f'Could not set {env_key}: {error}')

        row = AppAttachment(app_id=app.id, service_app_id=service_app.id, kind='storage')
        row.details = {'bucket': bucket, 'bucket_id': bucket_id,
                       'access_key_id': key['access_key_id'],
                       'secret_name': secret_name, 'env_keys': sorted(env)}
        db.session.add(row)
        db.session.commit()
        return row, True

    @classmethod
    def attach_grafana_source(cls, app, service_app, kind, grafana=None):
        """Create ``service_app`` as a data source on the Grafana ``app``."""
        from app.models.app_attachment import AppAttachment
        from app.services.grafana_provisioner import GrafanaError, GrafanaProvisioner
        from app.services.service_connection_service import ServiceConnectionService

        if not cls.is_grafana(app):
            raise AttachmentError(f'Only a Grafana install can attach {kind}')
        if not GRAFANA_KINDS[kind](*cls._template_and_engine(service_app)):
            raise AttachmentError(f'{service_app.name} cannot be used as {kind}')
        existing = AppAttachment.query.filter_by(app_id=app.id, kind=kind).first()
        if existing is not None:
            if existing.service_app_id != service_app.id:
                raise AttachmentError(f'This Grafana already has {kind} attached; detach it first')
            return existing, False
        url, error = ServiceConnectionService.resolve(service_app, 'url')
        if error:
            raise AttachmentError(error)
        try:
            grafana = grafana or GrafanaProvisioner.for_app(app)
            uid = grafana.upsert_datasource(kind, service_app.name, url)
            if kind == 'metrics':
                grafana.import_dashboard(uid)
        except GrafanaError as exc:
            raise AttachmentError(f'Grafana setup failed: {exc}') from exc
        row = AppAttachment(app_id=app.id, service_app_id=service_app.id, kind=kind)
        row.details = {'datasource_uid': uid, 'env_keys': []}
        db.session.add(row)
        db.session.commit()
        return row, True

    @classmethod
    def detach(cls, attachment, user_id=None, admin=None):
        """Remove the key, its secret and the env the attach wrote. Keeps the
        bucket and its data."""
        from app.services.env_service import EnvService
        from app.services.garage_admin import GarageAdmin, GarageError

        details = attachment.details
        warning = None
        if attachment.kind == 'storage' and details.get('access_key_id'):
            admin = admin or GarageAdmin(attachment.service_app.name)
            try:
                admin.delete_key(details['access_key_id'])
            except GarageError as exc:
                # The service may already be gone or stopped; the key is
                # useless without the env, and the row must still go.
                warning = f'Key could not be revoked on {attachment.service_app.name}: {exc}'
        if details.get('datasource_uid'):
            from app.services.grafana_provisioner import GrafanaError, GrafanaProvisioner
            try:
                (admin or GrafanaProvisioner.for_app(attachment.application)).delete_datasource(
                    details['datasource_uid'])
            except GrafanaError as exc:
                warning = f'Data source could not be removed from Grafana: {exc}'
        for env_key in details.get('env_keys') or []:
            EnvService.delete_env_var(attachment.app_id, env_key, user_id=user_id)
        if details.get('secret_name'):
            cls._delete_secret(details['secret_name'])
        db.session.delete(attachment)
        db.session.commit()
        return warning

    # -- internal vault ----------------------------------------------------------

    @staticmethod
    def _vault(user_id=None):
        from app.models.secret_vault import SecretVault
        vault = SecretVault.query.filter_by(slug=VAULT_SLUG).first()
        if vault is None:
            vault = SecretVault(name=VAULT_NAME, slug=VAULT_SLUG, created_by=user_id,
                                description='Keys ServerKit created when attaching a service to an app')
            db.session.add(vault)
            db.session.flush()
        return vault

    @classmethod
    def _store_secret(cls, name, value, user_id=None):
        from app.services.secret_vault_service import SecretService
        SecretService.upsert_internal_secret(
            cls._vault(user_id).id, name, value,
            description='S3 secret key for an attached storage bucket')

    @staticmethod
    def _delete_secret(name):
        from app.models.secret_vault import Secret, SecretVault
        vault = SecretVault.query.filter_by(slug=VAULT_SLUG).first()
        if vault is None:
            return
        Secret.query.filter_by(vault_id=vault.id, name=name).delete()
