"""Attach storage: bucket + scoped key per app (plan 86 §C2).

Hermetic: a fake Garage admin records calls. The real-Garage round trip
(write own bucket, refused elsewhere, key revoked on detach) is the
docker-builds leg in test_real_storage_attach_docker.py.
"""
import json

import pytest
import yaml

from app import db
from app.models import Application
from app.models.app_attachment import AppAttachment
from app.models.secret_vault import Secret, SecretVault
from app.services import app_attachment_service as svc
from app.services.app_attachment_service import AppAttachmentService, AttachmentError
from app.services.env_service import EnvService
from app.services.template_service import TemplateService
from tests.factories import make_application


class FakeGarage:
    def __init__(self):
        self.calls = []
        self.buckets = {}

    def ensure_layout(self):
        self.calls.append(('layout',))
        return True

    def ensure_bucket(self, alias):
        self.calls.append(('bucket', alias))
        return self.buckets.setdefault(alias, f'id-{alias}')

    def create_key(self, name):
        self.calls.append(('key', name))
        return {'access_key_id': 'GKabc', 'secret_access_key': 'topsecret'}

    def allow(self, bucket_id, access_key_id):
        self.calls.append(('allow', bucket_id, access_key_id))

    def delete_key(self, access_key_id):
        self.calls.append(('delete', access_key_id))


@pytest.fixture
def installed(app, tmp_path, monkeypatch):
    records = {}
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'repos': [], 'installed': records}))

    def make(name, template_id=None):
        root = tmp_path / name
        root.mkdir()
        (root / 'docker-compose.yml').write_text(yaml.safe_dump({'services': {name: {'image': 'x'}}}))
        if template_id:
            (root / '.serverkit-template.json').write_text(json.dumps(
                {'template_id': template_id, 'variables': {}}))
        row = make_application(db, name=name, root_path=str(root))
        if template_id:
            records[str(row.id)] = {'template_id': template_id}
        return row
    return make


def test_attach_creates_scoped_key_and_wires_env(installed):
    store, web = installed('store', 'garage'), installed('Web_App')
    fake = FakeGarage()

    row, created = AppAttachmentService.attach_storage(web, store, admin=fake)

    assert created
    assert fake.calls == [('layout',), ('bucket', 'web-app'), ('key', 'serverkit-Web_App'),
                          ('allow', 'id-web-app', 'GKabc')]
    env = EnvService.get_effective_env(web.id)
    assert env['S3_BUCKET'] == 'web-app'
    assert env['S3_ENDPOINT'] == env['AWS_ENDPOINT_URL'] == 'http://store:3900'
    assert env['S3_REGION'] == 'garage'
    assert env['S3_ACCESS_KEY_ID'] == env['AWS_ACCESS_KEY_ID'] == 'GKabc'
    assert env['S3_SECRET_ACCESS_KEY'] == env['AWS_SECRET_ACCESS_KEY'] == 'topsecret'
    # The secret lives encrypted in the vault, not in the env row or attachment.
    stored = EnvService.get_env_var(web.id, 'S3_SECRET_ACCESS_KEY')
    assert stored.value_from and 'topsecret' not in (stored.encrypted_value or '')
    assert 'topsecret' not in json.dumps(row.to_dict()) + (row.details_json or '')


def test_attach_is_idempotent_and_refuses_a_second_service(installed):
    store, other, web = installed('store', 'garage'), installed('store2', 'garage'), installed('web')
    fake = FakeGarage()
    first, _ = AppAttachmentService.attach_storage(web, store, admin=fake)
    again, created = AppAttachmentService.attach_storage(web, store, admin=fake)
    assert again.id == first.id and not created
    assert [c for c in fake.calls if c[0] == 'key'] == [('key', 'serverkit-web')]
    with pytest.raises(AttachmentError, match='already has storage'):
        AppAttachmentService.attach_storage(web, other, admin=fake)


@pytest.mark.parametrize('template_id', [None, 'redis'])
def test_only_a_storage_provisioner_can_be_attached(installed, template_id):
    target, web = installed('target', template_id), installed('web')
    with pytest.raises(AttachmentError, match='not an object storage'):
        AppAttachmentService.attach_storage(web, target, admin=FakeGarage())


def test_detach_revokes_the_key_and_removes_env_and_secret(installed):
    store, web = installed('store', 'garage'), installed('web')
    fake = FakeGarage()
    row, _ = AppAttachmentService.attach_storage(web, store, admin=fake)
    EnvService.set_env_var(web.id, 'UNRELATED', 'keep')

    assert AppAttachmentService.detach(row, admin=fake) is None

    assert ('delete', 'GKabc') in fake.calls
    env = EnvService.get_effective_env(web.id)
    assert not any(k.startswith(('S3_', 'AWS_')) for k in env)
    assert env['UNRELATED'] == 'keep'
    vault = SecretVault.query.filter_by(slug=svc.VAULT_SLUG).first()
    assert Secret.query.filter_by(vault_id=vault.id).count() == 0
    assert AppAttachment.query.count() == 0


def test_detach_still_cleans_up_when_the_service_is_unreachable(installed):
    from app.services.garage_admin import GarageError

    class Down(FakeGarage):
        def delete_key(self, access_key_id):
            raise GarageError('container not running')

    store, web = installed('store', 'garage'), installed('web')
    row, _ = AppAttachmentService.attach_storage(web, store, admin=FakeGarage())
    warning = AppAttachmentService.detach(row, admin=Down())
    assert 'could not be revoked' in warning
    assert AppAttachment.query.count() == 0


@pytest.mark.parametrize('name,bucket', [
    ('Shop', 'shop'), ('my_app.v2', 'my-app-v2'), ('a', 'app-a'),
    ('--x--', 'app-x'), ('a' * 80, 'a' * 63),
])
def test_bucket_names_are_s3_valid(name, bucket):
    assert svc._bucket_name(name) == bucket


# ── API ──────────────────────────────────────────────────────────────────────

def test_api_attach_list_detach(installed, client, auth_headers, monkeypatch):
    store, web = installed('store', 'garage'), installed('web')
    fake = FakeGarage()
    monkeypatch.setattr(svc, 'GarageAdmin', lambda name: fake, raising=False)
    monkeypatch.setattr('app.services.garage_admin.GarageAdmin', lambda name: fake)

    services = client.get('/api/v1/apps/attachments/storage-services', headers=auth_headers)
    assert [s['name'] for s in services.get_json()['services']] == ['store']

    made = client.post(f'/api/v1/apps/{web.id}/attachments/storage',
                       json={'service_app_id': store.id}, headers=auth_headers)
    assert made.status_code == 201, made.get_json()
    body = made.get_json()
    assert body['redeploy_required'] is True
    assert body['attachment']['bucket'] == 'web'
    assert 'secret' not in json.dumps(body).lower().replace('s3_secret', '')

    listed = client.get(f'/api/v1/apps/{web.id}/attachments', headers=auth_headers)
    assert [a['id'] for a in listed.get_json()['attachments']] == [body['attachment']['id']]

    gone = client.delete(f"/api/v1/apps/{web.id}/attachments/{body['attachment']['id']}",
                         headers=auth_headers)
    assert gone.status_code == 200
    assert client.get(f'/api/v1/apps/{web.id}/attachments',
                      headers=auth_headers).get_json()['attachments'] == []


def test_api_rejects_bad_input(installed, client, auth_headers):
    web, plain = installed('web'), installed('plain')
    url = f'/api/v1/apps/{web.id}/attachments/storage'
    assert client.post(url, json={}, headers=auth_headers).status_code == 400
    assert client.post(url, json={'service_app_id': 999999}, headers=auth_headers).status_code == 404
    assert client.post(url, json={'service_app_id': plain.id},
                       headers=auth_headers).status_code == 400
    assert client.post('/api/v1/apps/999999/attachments/storage', json={'service_app_id': plain.id},
                       headers=auth_headers).status_code == 404


def test_deleting_either_app_takes_the_attachment(installed):
    store, web = installed('store', 'garage'), installed('web')
    AppAttachmentService.attach_storage(web, store, admin=FakeGarage())
    db.session.delete(Application.query.get(store.id))
    db.session.commit()
    assert AppAttachment.query.count() == 0
