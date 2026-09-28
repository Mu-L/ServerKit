"""Attach a service an app uses: object storage today (plan 86 §C2).

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
