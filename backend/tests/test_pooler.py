"""PgBouncer beside an installed PostgreSQL (plan 86 §D1). Hermetic half; the
real round trip is test_real_pooler_docker.py."""
import json

import pytest
import yaml

from app import db
from app.services import pooler_service
from app.services.compose_env_service import ComposeEnvService
from app.services.service_connection_service import SHARED_NETWORK, ServiceConnectionService
from app.services.template_service import TemplateService
from tests.factories import make_application


@pytest.fixture
def installed(app, tmp_path, monkeypatch):
    records = {}
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'repos': [], 'installed': records}))

    def make(name, template_id, variables):
        root = tmp_path / name
        root.mkdir()
        (root / 'docker-compose.yml').write_text(yaml.safe_dump({'services': {'app': {'image': 'x'}}}))
        (root / '.serverkit-template.json').write_text(json.dumps(
            {'template_id': template_id, 'variables': variables}))
        row = make_application(db, name=name, root_path=str(root))
        records[str(row.id)] = {'template_id': template_id}
        return row
    return make


def test_pooling_adds_a_pgbouncer_service_and_a_pooled_url(installed):
    pg = installed('pg', 'postgresql', {'DB_PASSWORD': 'pa$$', 'DB_NAME': 'shop'})
    assert 'pooledUrl' not in ServiceConnectionService.spec(pg)['properties']
    assert 'pgbouncer' not in (ComposeEnvService.render_override(pg.root_path)['content'] or '')

    pg.pooler_enabled = True
    db.session.commit()

    override = yaml.safe_load(ComposeEnvService.render_override(pg.root_path)['content'])
    bouncer = override['services']['pgbouncer']
    assert bouncer['image'] == pooler_service.POOLER_IMAGE
    assert bouncer['container_name'] == 'pg-pooler'
    assert bouncer['environment']['DB_HOST'] == 'app'
    assert bouncer['environment']['POOL_MODE'] == 'transaction'
    assert bouncer['environment']['DB_PASSWORD'] == 'pa$$$$', 'compose would interpolate $'
    assert 'ports' not in bouncer
    assert bouncer['networks'] == {'default': {}, SHARED_NETWORK: {}}
    props = ServiceConnectionService.spec(pg)['properties']
    assert props['pooledUrl'] == 'postgresql://postgres:pa%24%24@pg-pooler:5432/shop'
    assert props['connectionString'].startswith('postgresql://postgres:pa%24%24@pg:5432/')


def test_only_postgres_engines_get_a_pooler(installed):
    cache = installed('cache', 'redis', {'DB_PASSWORD': 'x'})
    cache.pooler_enabled = True
    db.session.commit()
    assert not pooler_service.is_postgres_engine(cache)
    assert 'pgbouncer' not in (ComposeEnvService.render_override(cache.root_path)['content'] or '')


def test_api(installed, client, auth_headers, monkeypatch):
    pg = installed('pg', 'postgresql', {'DB_PASSWORD': 'x'})
    cache = installed('cache', 'redis', {'DB_PASSWORD': 'x'})
    monkeypatch.setattr('app.services.docker_service.DockerService.compose_up',
                        classmethod(lambda cls, path, **kw: {'success': True}))
    got = client.get(f'/api/v1/apps/{pg.id}/pooler', headers=auth_headers).get_json()
    assert got['available'] is True and got['enabled'] is False and 'LISTEN' in got['tradeoff']
    assert client.get(f'/api/v1/apps/{cache.id}/pooler',
                      headers=auth_headers).get_json()['available'] is False

    put = client.put(f'/api/v1/apps/{pg.id}/pooler', json={'enabled': True}, headers=auth_headers)
    assert put.status_code == 200 and put.get_json()['applied'] is True
    assert client.put(f'/api/v1/apps/{pg.id}/pooler', json={'enabled': 'yes'},
                      headers=auth_headers).status_code == 400
    assert client.put(f'/api/v1/apps/{cache.id}/pooler', json={'enabled': True},
                      headers=auth_headers).status_code == 400
