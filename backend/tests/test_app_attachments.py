"""Attach storage: bucket + scoped key per app (plan 86 §C2).

Hermetic: a fake Garage admin records calls. The real-Garage round trip
(write own bucket, refused elsewhere, key revoked on detach) is the
docker-builds leg in test_real_storage_attach_docker.py.
"""
import json
import os

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

    services = client.get('/api/v1/apps/attachments/services?kind=storage', headers=auth_headers)
    assert [s['name'] for s in services.get_json()['services']] == ['store']
    assert 'bucket' in services.get_json()['failure_mode']

    made = client.post(f'/api/v1/apps/{web.id}/attachments/storage',
                       json={'service_app_id': store.id}, headers=auth_headers)
    assert made.status_code == 201, made.get_json()
    body = made.get_json()
    assert body['redeploy_required'] is True
    assert body['attachment']['bucket'] == 'web'
    assert 'topsecret' not in json.dumps(body), 'the secret value never leaves the vault'

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


# ── cache and queue (plan 86 §C4) ─────────────────────────────────────────────

def _installed_with_vars(installed, name, template_id, variables):
    row = installed(name, template_id)
    path = os.path.join(row.root_path, '.serverkit-template.json')
    with open(path, 'w') as fh:
        json.dump({'template_id': template_id, 'variables': variables}, fh)
    return row


@pytest.mark.parametrize('kind,template_id,keys,scheme', [
    ('cache', 'redis', ['REDIS_URL'], 'redis://'),
    ('cache', 'valkey', ['REDIS_URL'], 'redis://'),
    ('queue', 'rabbitmq', ['AMQP_URL', 'BROKER_URL'], 'amqp://'),
    ('queue', 'redis', ['BROKER_URL'], 'redis://'),
])
def test_connection_kinds_wire_the_services_url(installed, kind, template_id, keys, scheme):
    svc_app = _installed_with_vars(installed, 'svc', template_id,
                                   {'DB_PASSWORD': 'pw', 'RABBITMQ_USER': 'u',
                                    'RABBITMQ_PASSWORD': 'pw'})
    web = installed('web')
    row, created = AppAttachmentService.attach(web, svc_app, kind)
    assert created and row.details['env_keys'] == keys
    env = EnvService.get_effective_env(web.id)
    for key in keys:
        assert env[key].startswith(scheme + ('' if scheme == 'amqp://' else ':')), env[key]
    assert 'AMQP_URL' not in env or template_id == 'rabbitmq'

    AppAttachmentService.detach(row)
    env = EnvService.get_effective_env(web.id)
    assert not any(k in env for k in keys)


@pytest.mark.parametrize('kind,template_id', [
    ('cache', 'rabbitmq'), ('cache', 'garage'), ('queue', 'garage'), ('cache', None),
])
def test_a_service_that_does_not_offer_the_kind_is_refused(installed, kind, template_id):
    target, web = installed('target', template_id), installed('web')
    with pytest.raises(AttachmentError, match=f'cannot be used as a {kind}'):
        AppAttachmentService.attach(web, target, kind)


def test_services_for_lists_only_matching_services(installed):
    installed('cache1', 'redis')
    installed('mq', 'rabbitmq')
    installed('store', 'garage')
    installed('web')
    names = lambda kind: [a.name for a in AppAttachmentService.services_for(kind)]
    assert names('cache') == ['cache1']
    assert names('queue') == ['cache1', 'mq']
    assert names('storage') == ['store']


def test_api_rejects_an_unknown_kind(installed, client, auth_headers):
    web, cache = installed('web'), installed('cache1', 'redis')
    bad = client.post(f'/api/v1/apps/{web.id}/attachments/tracing',
                      json={'service_app_id': cache.id}, headers=auth_headers)
    assert bad.status_code == 400
    assert client.get('/api/v1/apps/attachments/services?kind=nope',
                      headers=auth_headers).status_code == 400
    ok = client.post(f'/api/v1/apps/{web.id}/attachments/cache',
                     json={'service_app_id': cache.id}, headers=auth_headers)
    assert ok.status_code == 201 and ok.get_json()['attachment']['env_keys'] == ['REDIS_URL']


# ── Grafana data sources (plan 86 §A4) ────────────────────────────────────────

class FakeGrafana:
    def __init__(self):
        self.calls = []

    def upsert_datasource(self, kind, service_name, url):
        self.calls.append(('upsert', kind, service_name, url))
        return f'serverkit-{kind}-{service_name}'

    def import_dashboard(self, uid):
        self.calls.append(('dashboard', uid))

    def delete_datasource(self, uid):
        self.calls.append(('delete', uid))


def test_grafana_gets_metrics_and_logs_kinds_and_others_do_not(installed):
    graf, web = installed('graf', 'grafana'), installed('web')
    assert AppAttachmentService.kinds_for(graf) == ['cache', 'storage', 'queue', 'tracing',
                                                    'metrics', 'logs']
    assert AppAttachmentService.kinds_for(web) == ['cache', 'storage', 'queue', 'tracing']


def test_metrics_attach_creates_a_data_source_and_the_dashboard(installed):
    graf, prom = installed('graf', 'grafana'), installed('prom', 'prometheus')
    fake = FakeGrafana()
    row, created = AppAttachmentService.attach_grafana_source(graf, prom, 'metrics', grafana=fake)
    assert created
    assert fake.calls == [('upsert', 'metrics', 'prom', 'http://prom:9090'),
                          ('dashboard', 'serverkit-metrics-prom')]
    assert AppAttachmentService.detach(row, admin=fake) is None
    assert fake.calls[-1] == ('delete', 'serverkit-metrics-prom')


def test_logs_attach_adds_loki_without_a_dashboard(installed):
    graf, loki = installed('graf', 'grafana'), installed('logs1', 'loki')
    fake = FakeGrafana()
    AppAttachmentService.attach_grafana_source(graf, loki, 'logs', grafana=fake)
    assert fake.calls == [('upsert', 'logs', 'logs1', 'http://logs1:3100')]


@pytest.mark.parametrize('consumer,service,kind,match', [
    ('web', 'prometheus', 'metrics', 'Only a Grafana'),
    ('grafana', 'loki', 'metrics', 'cannot be used as metrics'),
    ('grafana', 'redis', 'logs', 'cannot be used as logs'),
])
def test_grafana_kinds_refuse_the_wrong_pair(installed, consumer, service, kind, match):
    target = installed('target', consumer if consumer != 'web' else None)
    svc_app = installed('svc', service)
    with pytest.raises(AttachmentError, match=match):
        AppAttachmentService.attach_grafana_source(target, svc_app, kind, grafana=FakeGrafana())


def test_an_attached_app_joins_the_shared_network(installed):
    from app.services.service_connection_service import ServiceConnectionService
    graf, prom = installed('graf', 'grafana'), installed('prom', 'prometheus')
    assert not ServiceConnectionService.needs_shared_network(graf)
    AppAttachmentService.attach_grafana_source(graf, prom, 'metrics', grafana=FakeGrafana())
    assert ServiceConnectionService.needs_shared_network(graf)


def test_the_bundled_dashboard_charts_the_metrics_the_panel_exports():
    import re
    from app.services.grafana_provisioner import DASHBOARD_PATH
    exported = {'serverkit_cpu_percent', 'serverkit_memory_percent', 'serverkit_disk_percent',
                'serverkit_containers_running', 'serverkit_server_up'}
    with open(DASHBOARD_PATH, encoding='utf-8') as fh:
        used = set(re.findall(r'serverkit_[a-z_]+', fh.read()))
    assert used and used <= exported, used - exported


# ── tracing (plan 86 §A4) ─────────────────────────────────────────────────────

def test_tracing_sets_the_standard_otel_variables(installed):
    otel, web = installed('otel', 'otel-collector'), installed('web')
    row, _ = AppAttachmentService.attach(web, otel, 'tracing')
    env = EnvService.get_effective_env(web.id)
    assert env['OTEL_EXPORTER_OTLP_ENDPOINT'] == 'http://otel:4318'
    assert env['OTEL_EXPORTER_OTLP_PROTOCOL'] == 'http/protobuf'
    AppAttachmentService.detach(row)
    assert 'OTEL_EXPORTER_OTLP_PROTOCOL' not in EnvService.get_effective_env(web.id)


def test_only_a_collector_forwards_traces_to_jaeger(installed):
    otel, jaeger, web = (installed('otel', 'otel-collector'), installed('jaeger1', 'jaeger'),
                         installed('web'))
    assert 'traces' in AppAttachmentService.kinds_for(otel)
    assert 'traces' not in AppAttachmentService.kinds_for(web)
    with pytest.raises(AttachmentError, match='Only a otel-collector'):
        AppAttachmentService.attach(web, jaeger, 'traces')
    AppAttachmentService.attach(otel, jaeger, 'traces')
    assert EnvService.get_effective_env(otel.id)['TRACES_OTLP_ENDPOINT'] == 'jaeger1:4317'
