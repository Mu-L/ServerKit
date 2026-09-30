"""Plan 87 §C — slot deploys for compose apps.

Two layers. The renderer is pure and is tested against the exact shape
``docker compose config`` prints (captured from compose v5). The engine runs
against the same fake world as the container slots, extended with compose
projects, and asserts the site served the whole time.
"""
import os

import pytest
import yaml

from factories import make_application

from app import db
from app.models.app_slot import AppSlot
from app.models.deployment import Deployment
from app.models.domain import Domain
from app.services import deploy_preflight_service as preflight
from app.services import deploy_settings
from app.services import slot_compose_service as sc
from app.services.deployment_service import DeploymentService
from app.services.docker_service import DockerService
from app.services.slot_deploy_service import SlotDeployService

from test_plan87_slot_deploys import World, world  # noqa: F401 - the shared fake world fixture

CONFIG = yaml.safe_load("""
name: shop
services:
  db:
    container_name: shop-db
    image: postgres:16
    networks: {default: null}
    volumes:
      - {type: volume, source: pgdata, target: /var/lib/postgresql/data, volume: {}}
  web:
    build: {context: /srv/shop, dockerfile: Dockerfile}
    container_name: shop
    depends_on:
      db: {condition: service_started, required: true}
    environment: {DB: postgres://db/x}
    networks: {default: null}
    ports:
      - {mode: ingress, target: 3000, published: "8100", protocol: tcp}
    volumes:
      - {type: bind, source: /srv/shop/conf, target: /conf, bind: {}}
  worker:
    image: shop-worker:latest
    container_name: shop-worker
    networks: {default: null}
    ports:
      - {mode: ingress, target: 9000, published: "9000", protocol: tcp}
networks:
  default: {name: shop_default}
volumes:
  pgdata: {name: shop_pgdata}
""")

STATELESS = yaml.safe_load("""
name: shop
services:
  web:
    image: shop:latest
    container_name: shop
    networks: {default: null}
    ports:
      - {mode: ingress, target: 3000, published: "8100", protocol: tcp}
    volumes:
      - {type: volume, source: uploads, target: /app/uploads, volume: {}}
  cache:
    image: memcached:1.6
    container_name: shop-cache
    networks: {default: null}
networks:
  default: {name: shop_default}
volumes:
  uploads: {name: shop_uploads}
""")


class _App:
    id = 7
    name = 'Shop'
    root_path = '/srv/shop'
    compose_file = 'docker-compose.yml'
    port = 8100


# ── the renderer ─────────────────────────────────────────────────────────────

def test_the_web_service_is_the_one_publishing_the_apps_port():
    assert sc.web_service(CONFIG, 8100) == ('web', 3000)
    assert sc.web_service(CONFIG, 1234) == (None, None)


def test_stateful_means_a_named_volume_on_anything_but_web():
    assert sc.classify(CONFIG, 'web') == (['web', 'worker'], ['db'])
    assert sc.classify(STATELESS, 'web') == (['web', 'cache'], [])


def test_a_slot_render_strips_names_publishes_only_web_and_pins_volumes():
    rendered = sc.render_slot(_App, STATELESS, 'web', 9401, 3000, {}, with_data_network=False)
    services = rendered['services']
    assert all('container_name' not in s for s in services.values())
    assert services['web']['ports'] == ['127.0.0.1:9401:3000']
    assert 'ports' not in services['cache']
    # Pinned: without this, project shop-b would get an empty shop-b_uploads.
    assert rendered['volumes'] == {'uploads': {'name': 'shop_uploads'}}
    # Per-slot network: two slots never share one DNS namespace.
    assert rendered['networks'] == {'default': {}}


def test_a_split_render_leaves_the_stateful_services_to_the_data_project():
    tags = {'web': 'serverkit-app-7-web:d12'}
    rendered = sc.render_slot(_App, CONFIG, 'web', 9401, 3000, tags, with_data_network=True)
    services = rendered['services']
    assert set(services) == {'web', 'worker'}
    assert 'depends_on' not in services['web']
    assert services['web']['image'] == 'serverkit-app-7-web:d12'
    assert services['web']['build']['context'] == '/srv/shop', 'built under the immutable tag'
    assert services['web']['networks'] == {'default': None, 'shop-data': None}
    assert rendered['networks']['shop-data'] == {'external': True, 'name': 'shop-data'}
    assert 'volumes' not in rendered, 'pgdata belongs to the data project now'


def test_the_data_project_keeps_the_same_volume_and_a_named_network():
    rendered = sc.render_data(_App, CONFIG, 'web')
    assert list(rendered['services']) == ['db']
    assert 'container_name' not in rendered['services']['db']
    assert rendered['volumes'] == {'pgdata': {'name': 'shop_pgdata'}}
    assert rendered['networks'] == {'default': {'name': 'shop-data'}}


def test_project_names():
    assert sc.slot_project(_App, 'a') == 'shop-a'
    assert sc.original_project(_App) == 'shop'
    assert sc.data_network(_App) == 'shop-data'


# ── the engine, against a fake compose ──────────────────────────────────────

@pytest.fixture
def compose_world(world, monkeypatch, tmp_path):  # noqa: F811 - pytest fixture injection
    w = world
    w.config = STATELESS
    w.compose_calls = []
    w.up_fails = False

    def compose(project, files, *args, cwd=None, timeout=None):
        verb = args[0]
        w.compose_calls.append((project, verb))
        members = [n for n in w.containers if n.startswith(f'{project}-')]
        if verb == 'up':
            if w.up_fails:
                return {'success': False, 'error': 'port is already allocated'}
            with open(files[0], encoding='utf-8') as fh:
                rendered = yaml.safe_load(fh.read().split('\n', 2)[2])
            for name, service in rendered['services'].items():
                port = None
                if service.get('ports'):
                    port = int(str(service['ports'][0]).split(':')[1])
                w.containers[f'{project}-{name}-1'] = {
                    'image': f'{project}:{name}', 'port': port, 'running': True}
            w.note(f'up {project}')
        elif verb in ('stop', 'start'):
            for n in members:
                w.containers[n]['running'] = verb == 'start'
            w.note(f'{verb} {project}')
        elif verb == 'down':
            for n in members:
                w.containers.pop(n)
            w.note(f'down {project}')
        elif verb == 'restart':
            w.note(f'restart {project}')
        return {'success': True, 'output': ''}

    def project_containers(project, service=None):
        rows = []
        for name, c in w.containers.items():
            if not name.startswith(f'{project}-'):
                continue
            svc = name[len(project) + 1:].rsplit('-', 1)[0]
            if service and svc != service:
                continue
            rows.append({'id': name, 'name': name, 'service': svc,
                         'state': 'running' if c['running'] else 'exited'})
        return rows

    monkeypatch.setattr(sc, 'compose', compose)
    monkeypatch.setattr(sc, 'project_containers', project_containers)
    monkeypatch.setattr(sc, 'merged_config', lambda app: (w.config, None))
    monkeypatch.setattr(DockerService, 'ensure_network', classmethod(lambda cls, name: {'success': True}))
    monkeypatch.setattr(preflight, 'preflight_compose_project',
                        lambda *a, **k: preflight.PreflightResult())
    monkeypatch.setattr(DockerService, 'compose_up', classmethod(
        lambda cls, path, **k: (w.note('IN-PLACE up'), {'success': True})[1]))
    monkeypatch.setattr(DockerService, 'compose_down', classmethod(
        lambda cls, path, **k: (w.note('IN-PLACE down'), {'success': True})[1]))
    w.root = tmp_path
    return w


def _compose_app(w, **kw):
    fields = dict(name='shop', app_type='docker', status='running', root_path=str(w.root),
                  docker_image=None, compose_file='docker-compose.yml',
                  managed_by='docker_compose', port=8100)
    fields.update(kw)
    row = make_application(db, **fields)
    db.session.add(Domain(name='shop.example.com', application_id=row.id, is_primary=True))
    db.session.add(Deployment(app_id=row.id, version=1, status='live',
                              deploy_completed_at=db.func.now()))
    db.session.commit()
    project = sc.original_project(row)
    # The in-place project running today (compose derives its name from the dir).
    w.containers[f'{project}-web-1'] = {'image': 'shop:v1', 'port': 8100, 'running': True}
    w.containers[f'{project}-cache-1'] = {'image': 'memcached', 'port': None, 'running': True}
    w.vhost_port = 8100
    w.note('initial')
    deploy_settings.update(row, {'slot_volumes_confirmed': True})
    db.session.commit()
    return row


def test_opting_in_adopts_the_running_project_as_slot_a(app, compose_world):
    w = compose_world
    row = _compose_app(w)
    assert SlotDeployService.set_enabled(row, True)['success']
    a = AppSlot.query.filter_by(application_id=row.id, slot='a').one()
    assert a.project_name == sc.original_project(row) and a.state == 'live'
    assert a.container_port == 3000 and a.host_port == 8100
    assert w.compose_calls == [] and w.outage() == []


def test_a_compose_slot_deploy_boots_a_second_project_then_switches(app, compose_world):
    w = compose_world
    row = _compose_app(w)
    SlotDeployService.set_enabled(row, True)

    result = DeploymentService.deploy(row.id)

    assert result['success'], result
    b = AppSlot.query.filter_by(application_id=row.id, slot='b').one()
    assert b.project_name == 'shop-b' and row.active_slot == 'b' and row.port == b.host_port
    rendered = yaml.safe_load(open(sc.slot_file(row, 'b'), encoding='utf-8').read().split('\n', 2)[2])
    assert rendered['services']['web']['ports'] == [f'127.0.0.1:{b.host_port}:3000']
    assert rendered['volumes']['uploads']['name'] == 'shop_uploads'
    assert 'IN-PLACE down' not in [e for e, _ in w.timeline], 'the live stack was never stopped'
    assert w.outage() == []
    # The standby is the original project, still up.
    assert w.containers[f'{sc.original_project(row)}-web-1']['running']

    # Back to A: the adopted in-place project makes way for shop-a.
    again = DeploymentService.deploy(row.id)
    assert again['success'], again
    assert row.active_slot == 'a'
    assert (sc.original_project(row), 'down') in w.compose_calls
    assert ('shop-a', 'up') in w.compose_calls and w.outage() == []


def test_a_compose_slot_that_never_gets_healthy_is_removed_and_nothing_switches(app, compose_world):
    w = compose_world
    row = _compose_app(w)
    SlotDeployService.set_enabled(row, True)
    w.unhealthy_images.add('shop-b:web')
    result = DeploymentService.deploy(row.id)
    assert result['success'] is False and result['still_serving'] is True
    assert ('shop-b', 'down') in w.compose_calls
    assert row.active_slot == 'a' and w.outage() == []


def test_a_stack_with_a_database_needs_the_split_first(app, compose_world):
    w = compose_world
    w.config = CONFIG
    row = _compose_app(w)
    verdict = SlotDeployService.eligibility(row)
    assert verdict['eligible'] is False and verdict['split_needed'] is True
    assert any('db' in r and 'shop-data' in r for r in verdict['reasons'])

    preview = SlotDeployService.compose_split_preview(row)
    assert preview['stateful'] == ['db'] and 'shop_pgdata' in preview['data_compose']

    result = SlotDeployService.compose_split_apply(row)

    assert result['success'], result
    assert [c for c in w.compose_calls] == [(sc.original_project(row), 'down'),
                                            ('shop-data', 'up'), ('shop-a', 'up')]
    assert row.slot_deploys_enabled and row.active_slot == 'a'
    assert SlotDeployService.eligibility(row)['eligible'] is True
    # From now on deploys touch only the stateless services.
    assert DeploymentService.deploy(row.id)['success']
    rendered = yaml.safe_load(open(sc.slot_file(row, 'b'), encoding='utf-8').read().split('\n', 2)[2])
    assert 'db' not in rendered['services']
    assert ('shop-data', 'down') not in w.compose_calls


def test_a_split_app_cannot_quietly_leave_slots(app, compose_world):
    w = compose_world
    w.config = CONFIG
    row = _compose_app(w)
    SlotDeployService.compose_split_apply(row)
    assert SlotDeployService.set_enabled(row, False)['success'] is False


def test_switching_back_restarts_the_standby_project_without_rebuilding(app, compose_world):
    w = compose_world
    row = _compose_app(w)
    SlotDeployService.set_enabled(row, True)
    assert DeploymentService.deploy(row.id)['success']
    ups = [c for c in w.compose_calls if c[1] == 'up']
    assert SlotDeployService.switch_back(row)['success']
    assert [c for c in w.compose_calls if c[1] == 'up'] == ups, 'no new project was brought up'
    assert row.active_slot == 'a' and row.port == 8100 and w.outage() == []


def test_the_webhook_path_deploys_through_the_slots(app, compose_world, monkeypatch):
    from app.services.git_deploy_service import GitDeployService
    w = compose_world
    row = _compose_app(w)
    SlotDeployService.set_enabled(row, True)
    result = GitDeployService._standard_restart(row)
    assert result['success'], result
    assert row.active_slot == 'b'
    assert 'IN-PLACE down' not in [e for e, _ in w.timeline] and w.outage() == []


def test_a_template_update_on_slots_never_stops_the_live_stack(app, compose_world, monkeypatch):
    from app.services.template_service import TemplateService
    w = compose_world
    row = _compose_app(w)
    (w.root / 'docker-compose.yml').write_text('services: {web: {image: old}}')
    SlotDeployService.set_enabled(row, True)
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'installed': {str(row.id): {'template_id': 't'}}}))
    monkeypatch.setattr(TemplateService, 'save_config', classmethod(lambda cls, c: None))
    monkeypatch.setattr(TemplateService, 'get_template', classmethod(
        lambda cls, tid: {'success': True, 'template': {'name': 't', 'version': '2'}}))
    monkeypatch.setattr(TemplateService, 'generate_compose', classmethod(
        lambda cls, template, variables: 'services: {web: {image: new}}'))

    w.up_fails = True
    failed = TemplateService.update_app(row.id)
    assert failed['success'] is False
    assert (w.root / 'docker-compose.yml').read_text() == 'services: {web: {image: old}}'

    w.up_fails = False
    assert TemplateService.update_app(row.id)['success']
    assert (w.root / 'docker-compose.yml').read_text() == 'services: {web: {image: new}}'
    assert 'IN-PLACE down' not in [e for e, _ in w.timeline] and w.outage() == []


def test_lifecycle_and_status_follow_the_live_project(app, compose_world):
    from app.services import application_lifecycle_service as lifecycle
    from app.services.container_status_service import _ContainerIndex
    w = compose_world
    row = _compose_app(w)
    SlotDeployService.set_enabled(row, True)
    assert DeploymentService.deploy(row.id)['success']

    lifecycle.restart_application(row)
    assert w.compose_calls[-1] == ('shop-b', 'restart')

    index = _ContainerIndex([
        {'id': '1', 'name': 'shop-b-web-1', 'project': 'shop-b', 'state': 'running'},
        {'id': '2', 'name': 'x-web-1', 'project': sc.original_project(row), 'state': 'exited'},
    ])
    assert [c['name'] for c in index.for_app(row)] == ['shop-b-web-1'], 'the standby is not counted'


def test_image_update_on_a_slot_app_is_a_queued_slot_deploy(app, compose_world, client,
                                                            auth_headers, monkeypatch):
    from app.services.deployment_job_service import DeploymentJobService
    w = compose_world
    row = _compose_app(w)
    SlotDeployService.set_enabled(row, True)
    monkeypatch.setattr(DeploymentJobService, '_enqueue_app_deploy', classmethod(lambda cls, job: None))
    resp = client.post(f'/api/v1/apps/{row.id}/image-update/apply', headers=auth_headers)
    assert resp.status_code == 202, resp.get_json()
    assert 'IN-PLACE up' not in [e for e, _ in w.timeline]
