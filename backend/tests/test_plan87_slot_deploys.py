"""Plan 87 §B — A/B slot deploys for single-container apps.

The fake world below plays Docker and nginx. Every docker call and every vhost
write re-checks one invariant: the port nginx proxies to has a running,
healthy container behind it. The failure matrix (plan 72 B.5, now against the
slot engine) asserts that invariant held for the WHOLE deploy — not just at
the end — because "the site came back" is not the property; "the site never
left" is.
"""
from datetime import datetime, timedelta

import pytest

from factories import make_application

from app import db
from app.models.app_slot import AppSlot
from app.models.deployment import Deployment
from app.models.domain import Domain
from app.services import deploy_preflight_service as preflight
from app.services import deploy_settings, health_gate, slot_deploy_service
from app.services.build_service import BuildService
from app.services.deployment_service import DeploymentService
from app.services.docker_service import DockerService
from app.services.git_service import GitService
from app.services.slot_deploy_service import SlotDeployService


class World:
    """Containers, their ports and health, and where nginx points."""

    def __init__(self):
        self.containers = {}     # name -> {image, port, running, healthy}
        self.vhost_port = None
        self.timeline = []       # (event, serving?)
        self.nginx_fails = False
        self.run_fails = False
        self.unhealthy_images = set()
        self.degrades_after = {}  # image -> probes before it starts failing
        self.probes = {}
        self.ran = []
        self.notified = []

    def serving(self):
        for c in self.containers.values():
            if c['port'] == self.vhost_port and c['running'] and self._healthy(c, count=False):
                return True
        return False

    def _healthy(self, c, count=True):
        if c['image'] in self.unhealthy_images:
            return False
        limit = self.degrades_after.get(c['image'])
        if limit is None:
            return True
        seen = self.probes.get(c['image'], 0)
        if count:
            self.probes[c['image']] = seen + 1
        return seen < limit

    def note(self, event):
        self.timeline.append((event, self.serving()))

    def outage(self):
        return [event for event, ok in self.timeline if not ok]

    def by_port(self, port):
        return next((c for c in self.containers.values()
                     if c['port'] == port and c['running']), None)


@pytest.fixture
def world(monkeypatch):
    w = World()

    def get_container(name):
        c = w.containers.get(name)
        return {'Id': name, 'State': {'Running': c['running']}} if c else None

    def run_container(image, name=None, ports=None, **kw):
        if w.run_fails:
            return {'success': False, 'error': 'port is already allocated'}
        port = int(ports[0].split(':')[-2]) if ports else None
        w.containers[name] = {'image': image, 'port': port, 'running': True}
        w.ran.append((name, image, ports[0] if ports else None))
        w.note(f'run {name}')
        return {'success': True, 'container_id': name}

    def start_container(name):
        if name in w.containers:
            w.containers[name]['running'] = True
        w.note(f'start {name}')
        return {'success': True}

    def stop_container(name, timeout=10):
        if name in w.containers:
            w.containers[name]['running'] = False
        w.note(f'stop {name}')
        return {'success': True}

    def remove_container(name, force=False, volumes=False):
        w.containers.pop(name, None)
        w.note(f'rm {name}')
        return {'success': True}

    monkeypatch.setattr(DockerService, 'get_container', staticmethod(get_container))
    monkeypatch.setattr(DockerService, 'run_container', staticmethod(run_container))
    monkeypatch.setattr(DockerService, 'start_container', staticmethod(start_container))
    monkeypatch.setattr(DockerService, 'stop_container', staticmethod(stop_container))
    monkeypatch.setattr(DockerService, 'remove_container', staticmethod(remove_container))
    monkeypatch.setattr(DockerService, 'run', classmethod(
        lambda cls, args, **kw: {'success': True, 'output': ''}))

    def container_health(name):
        c = w.containers.get(name)
        if not c:
            return None, ''
        return ('running' if c['running'] else 'exited'), ''

    def probe(url, host_header=None, timeout=3.0):
        if host_header:                      # through nginx
            c = w.by_port(w.vhost_port)
            return (200 if c and w._healthy(c) else 502), None
        port = int(url.split(':')[2].split('/')[0])
        c = w.by_port(port)
        if not c:
            return None, 'Connection refused'
        return (200 if w._healthy(c) else 500), None

    monkeypatch.setattr(health_gate, 'container_health', container_health)
    monkeypatch.setattr(health_gate, 'probe', probe)
    monkeypatch.setattr(health_gate, 'sleep', lambda s: None)
    clock = {'t': 0.0}

    def tick(seconds=0):
        clock['t'] += seconds or 1
    monkeypatch.setattr(health_gate, 'clock', lambda: clock['t'])
    monkeypatch.setattr(health_gate, 'sleep', tick)
    monkeypatch.setattr(slot_deploy_service, '_clock', lambda: clock['t'])
    monkeypatch.setattr(slot_deploy_service, '_sleep', tick)

    from app.services.site_domain_service import SiteDomainService

    def write_app_vhost(app, force_type=None):
        if w.nginx_fails:
            w.note('nginx -t failed')
            return {'nginx': {'success': False}, 'warning': 'nginx vhost not created: nginx -t failed'}
        w.vhost_port = app.port
        w.note(f'vhost -> {app.port}')
        return {'nginx': {'success': True}, 'warning': None}

    monkeypatch.setattr(SiteDomainService, 'write_app_vhost', classmethod(
        lambda cls, app, force_type=None: write_app_vhost(app)))

    from app.services.template_service import TemplateService
    ports = iter(range(9400, 9500))
    monkeypatch.setattr(TemplateService, '_find_available_port', classmethod(
        lambda cls, start_port=8000, max_attempts=1000: next(ports)))

    from app.services.worker_process_service import WorkerProcessService
    from app.services import worker_process_service
    monkeypatch.setattr(WorkerProcessService, 'deploy', classmethod(
        lambda cls, *a, **k: {'started': [], 'failed': {}}))
    monkeypatch.setattr(worker_process_service, 'connect_shared_network', lambda *a: None)

    from app.plugins_sdk import notify
    monkeypatch.setattr(notify, 'send', lambda event, **kw: w.notified.append(event))

    # The deploy pipeline around the engine.
    from app.services.container_registry_service import ContainerRegistryService
    from app.services.env_service import EnvService
    monkeypatch.setattr(ContainerRegistryService, 'for_app', staticmethod(lambda app: None))
    monkeypatch.setattr(EnvService, 'get_effective_env', staticmethod(lambda *a, **k: {}))
    monkeypatch.setattr(preflight, 'preflight_image', lambda image, pull=None, log=None:
                        preflight.PreflightResult())
    monkeypatch.setattr(GitService, 'get_commit_info', classmethod(lambda cls, *a: None))
    monkeypatch.setattr(GitService, 'get_app_config', classmethod(lambda cls, app_id: None))
    monkeypatch.setattr(BuildService, 'get_app_build_config', classmethod(lambda cls, app_id: None))
    w.build_fails = False

    def build(cls, app_id, no_cache=False, log_callback=None, image_tag=None):
        if w.build_fails:
            return {'success': False, 'error': 'npm ERR! build failed'}
        return {'success': True, 'image_tag': image_tag}

    monkeypatch.setattr(BuildService, 'build', classmethod(build))
    return w


def _live_app(world, **kw):
    """An app deployed the old in-place way, published on a domain, serving."""
    fields = dict(name='shop', app_type='docker', status='running', root_path='/srv/shop',
                  docker_image='serverkit-app-1:d0', compose_file=None, managed_by=None,
                  port=8100)
    fields.update(kw)
    row = make_application(db, **fields)
    db.session.add(Domain(name='shop.example.com', application_id=row.id, is_primary=True))
    dep = Deployment(app_id=row.id, version=1, status='live', image_tag=f'serverkit-app-{row.id}:d0',
                     deploy_completed_at=datetime.utcnow())
    db.session.add(dep)
    db.session.commit()
    world.containers[f'serverkit-app-{row.id}'] = {
        'image': dep.image_tag, 'port': 8100, 'running': True}
    world.vhost_port = 8100
    world.note('initial')
    return row


def _enable(row):
    result = SlotDeployService.set_enabled(row, True)
    assert result['success'], result
    return result


# ── adoption ─────────────────────────────────────────────────────────────────

def test_opting_in_adopts_the_running_container_as_slot_a_without_touching_it(app, world):
    row = _live_app(world)
    _enable(row)
    a = AppSlot.query.filter_by(application_id=row.id, slot='a').one()
    assert (a.state, a.host_port, a.container_name) == ('live', 8100, f'serverkit-app-{row.id}')
    assert row.active_slot == 'a'
    assert world.ran == [] and world.outage() == []


def test_an_app_that_cannot_use_slots_says_why(app, world):
    row = _live_app(world)
    db.session.delete(row.domains[0])
    row.server_id = None
    row.app_type = 'php'
    db.session.commit()
    verdict = SlotDeployService.eligibility(row)
    assert verdict['eligible'] is False
    assert any('php' in r for r in verdict['reasons'])
    assert any('domain' in r for r in verdict['reasons'])
    result = SlotDeployService.set_enabled(row, True)
    assert result['success'] is False and row.slot_deploys_enabled is False


def test_shared_volumes_need_the_operators_word(app, world):
    from app.models.app_volume import AppVolume
    row = _live_app(world)
    db.session.add(AppVolume(application_id=row.id, name='data', mount_path='/data',
                             docker_volume_name=f'serverkit-app-{row.id}-data'))
    db.session.commit()
    assert SlotDeployService.eligibility(row)['eligible'] is False
    deploy_settings.update(row, {'slot_volumes_confirmed': True})
    verdict = SlotDeployService.eligibility(row)
    assert verdict['eligible'] is True and verdict['warnings']


# ── the happy path ───────────────────────────────────────────────────────────

def test_a_slot_deploy_boots_beside_the_live_release_then_switches(app, world):
    row = _live_app(world)
    _enable(row)

    result = DeploymentService.deploy(row.id)

    assert result['success'], result
    b = AppSlot.query.filter_by(application_id=row.id, slot='b').one()
    a = AppSlot.query.filter_by(application_id=row.id, slot='a').one()
    # The new release is on its own loopback port, and app.port followed it.
    assert world.ran == [(f'serverkit-slot-{row.id}-b', result['deployment']['image_tag'],
                          f'127.0.0.1:{b.host_port}:8100')]
    assert row.port == b.host_port and row.active_slot == 'b' and world.vhost_port == b.host_port
    assert (b.state, a.state) == ('live', 'standby')
    assert world.containers[f'serverkit-app-{row.id}']['running'], 'standby stays warm'
    assert world.outage() == []
    # The next deploy goes back to A, on A's own port.
    assert DeploymentService.deploy(row.id)['success']
    assert row.active_slot == 'a' and row.port == 8100
    assert world.outage() == []


# ── failure matrix: each asserts the live slot served the whole time ────────

def test_failing_build_never_touches_a_slot(app, world):
    row = _live_app(world)
    _enable(row)
    world.build_fails = True
    result = DeploymentService.deploy(row.id)
    assert result['success'] is False
    assert world.ran == [] and world.outage() == [] and row.port == 8100


def test_failing_boot_keeps_the_site_on_the_old_release(app, world):
    row = _live_app(world)
    _enable(row)
    world.run_fails = True
    result = DeploymentService.deploy(row.id)
    assert result['success'] is False and result['still_serving'] is True
    assert 'still serving v1' in result['error']
    assert row.status == 'running' and row.port == 8100 and row.active_slot == 'a'
    assert world.outage() == [] and 'app.deploy_aborted' in world.notified


def test_a_release_that_never_becomes_healthy_is_torn_down_before_the_switch(app, world):
    row = _live_app(world)
    _enable(row)
    world.unhealthy_images.add(f'serverkit-app-{row.id}:d{Deployment.query.count() + 1}')
    result = DeploymentService.deploy(row.id)
    assert result['success'] is False and result['still_serving'] is True
    assert 'never became healthy' in result['error']
    assert f'serverkit-slot-{row.id}-b' not in world.containers
    assert world.vhost_port == 8100 and world.outage() == []
    assert AppSlot.query.filter_by(application_id=row.id, slot='b').one().state == 'failed'
    assert Deployment.query.filter_by(app_id=row.id).order_by(Deployment.id.desc()).first().status == 'failed'


def test_healthy_then_unhealthy_in_the_watch_window_switches_back(app, world):
    row = _live_app(world)
    _enable(row)
    image = f'serverkit-app-{row.id}:d{Deployment.query.count() + 1}'
    world.degrades_after[image] = 4      # passes the 3-probe gate, then falls over
    result = DeploymentService.deploy(row.id)
    assert result['success'] is False and result['still_serving'] is True
    assert 'went back to v1' in result['error']
    assert row.active_slot == 'a' and row.port == 8100 and world.vhost_port == 8100
    newest = Deployment.query.filter_by(app_id=row.id).order_by(Deployment.id.desc()).first()
    assert newest.status == 'rolled_back' and row.status == 'running'
    assert 'app.deploy_reverted' in world.notified
    # The outage window is the watch window only: the bad release served, the
    # watch caught it, and traffic went back. Nothing before the switch failed.
    switch = next(i for i, (event, _) in enumerate(world.timeline) if event.startswith('vhost -> 94'))
    assert all(ok for _, ok in world.timeline[:switch])
    assert world.timeline[-1][1] is True


def test_nginx_t_failure_at_the_switch_leaves_traffic_where_it_was(app, world):
    row = _live_app(world)
    _enable(row)
    world.nginx_fails = True
    result = DeploymentService.deploy(row.id)
    assert result['success'] is False and result['still_serving'] is True
    assert 'Could not switch traffic' in result['error']
    assert row.port == 8100 and row.active_slot == 'a' and world.outage() == []


# ── rollback round-trips ─────────────────────────────────────────────────────

def _two_deploys(world, row):
    _enable(row)
    assert DeploymentService.deploy(row.id)['success']   # v2 -> slot b, a standby


def test_switch_back_to_a_warm_standby_starts_nothing_new(app, world):
    row = _live_app(world)
    _two_deploys(world, row)
    ran_before = list(world.ran)

    result = SlotDeployService.switch_back(row)

    assert result['success'], result
    assert world.ran == ran_before, 'a warm standby must not be rebuilt or re-run'
    assert row.active_slot == 'a' and row.port == 8100 and world.outage() == []
    assert AppSlot.query.filter_by(application_id=row.id, slot='b').one().state == 'standby'


def test_switch_back_to_a_stopped_standby_starts_and_gates_it(app, world):
    row = _live_app(world)
    _two_deploys(world, row)
    a = AppSlot.query.filter_by(application_id=row.id, slot='a').one()
    a.standby_until = datetime.utcnow() - timedelta(minutes=1)
    db.session.commit()
    assert SlotDeployService.sweep_standby() == {'stopped': [f'{row.id}/a']}
    assert world.containers[f'serverkit-app-{row.id}']['running'] is False
    assert world.outage() == []

    result = SlotDeployService.switch_back(row)

    assert result['success'], result
    assert world.containers[f'serverkit-app-{row.id}']['running'] is True
    assert row.active_slot == 'a' and world.outage() == []


def test_rollback_older_than_the_standby_is_a_slot_deploy_of_that_image(app, world):
    row = _live_app(world)
    _two_deploys(world, row)                               # v2 live (b), v1 standby (a)
    assert DeploymentService.deploy(row.id)['success']     # v3 live (a), v2 standby (b)
    assert row.active_slot == 'a'
    v1 = Deployment.query.filter_by(app_id=row.id, version=1).one()

    result = DeploymentService.rollback(row.id, target_version=1)

    assert result['success'], result
    assert world.ran[-1][1] == v1.image_tag, 'rolled back to the image v1 actually ran'
    assert row.active_slot == 'b' and world.outage() == []


def test_no_standby_means_no_switch_back(app, world):
    row = _live_app(world)
    _enable(row)
    result = SlotDeployService.switch_back(row)
    assert result['success'] is False and 'no standby' in result['error']


# ── standby, retention, leaving slots ────────────────────────────────────────

def test_a_standby_is_not_stopped_while_a_deploy_holds_the_app(app, world):
    from app.services.deploy_lock import deploy_lock
    row = _live_app(world)
    _two_deploys(world, row)
    a = AppSlot.query.filter_by(application_id=row.id, slot='a').one()
    a.standby_until = datetime.utcnow() - timedelta(minutes=1)
    db.session.commit()
    with deploy_lock(row.id, 'deploy'):
        assert SlotDeployService.sweep_standby() == {}
    assert a.state == 'standby'


def test_slot_images_are_never_pruned(app, world):
    row = _live_app(world)
    _two_deploys(world, row)
    protected = DeploymentService._protected_images(row)
    assert {s.image_ref for s in row.slots} == protected and len(protected) == 2


def test_slot_ports_stay_reserved(app, world):
    from app.services.template_service import TemplateService
    row = _live_app(world)
    _two_deploys(world, row)
    used = TemplateService._get_database_used_ports()
    assert {8100, row.port} <= used


def test_turning_slots_off_folds_them_back_into_an_in_place_deploy(app, world):
    row = _live_app(world)
    _two_deploys(world, row)                               # live on slot b, loopback port
    assert SlotDeployService.set_enabled(row, False)['success']

    assert DeploymentService.deploy(row.id)['success']

    assert AppSlot.query.filter_by(application_id=row.id).count() == 0
    assert row.active_slot is None and row.port == 8100
    assert not any(name.startswith('serverkit-slot-') for name in world.containers)
    assert world.containers[f'serverkit-app-{row.id}']['port'] == 8100
    assert world.vhost_port == 8100


def test_an_app_that_stops_qualifying_fails_instead_of_deploying_in_place(app, world):
    row = _live_app(world)
    _enable(row)
    for d in row.domains:
        db.session.delete(d)
    db.session.commit()
    result = DeploymentService.deploy(row.id)
    assert result['success'] is False and 'no longer qualifies' in result['error']
    assert world.ran == [] and row.status == 'running'


# ── API ──────────────────────────────────────────────────────────────────────

def test_slots_api_round_trip(app, world, client, auth_headers):
    row = _live_app(world)
    resp = client.get(f'/api/v1/apps/{row.id}/slots', headers=auth_headers)
    assert resp.status_code == 200 and resp.get_json()['eligibility']['eligible'] is True
    resp = client.put(f'/api/v1/apps/{row.id}/slots', headers=auth_headers, json={'enabled': True})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()['slots']
    assert body['enabled'] is True and body['active_slot'] == 'a'
    resp = client.post(f'/api/v1/apps/{row.id}/slots/switch-back', headers=auth_headers)
    assert resp.status_code == 409
    resp = client.put(f'/api/v1/apps/{row.id}/slots', headers=auth_headers, json={'enabled': 'yes'})
    assert resp.status_code == 400


# ── §E: everything else that touches the container follows the live slot ────

def test_restart_and_stop_act_on_the_live_slot_and_stop_the_standby(app, world, monkeypatch):
    from app.services import application_lifecycle_service as lifecycle
    from app.services.worker_process_service import WorkerProcessService
    monkeypatch.setattr(WorkerProcessService, 'each', classmethod(lambda cls, app, action: None))
    restarted = []
    monkeypatch.setattr(DockerService, 'restart_container', staticmethod(
        lambda name: restarted.append(name) or {'success': True}))
    row = _live_app(world)
    _two_deploys(world, row)                              # live b, standby a

    lifecycle.restart_application(row)
    assert restarted == [f'serverkit-slot-{row.id}-b']

    lifecycle.stop_application(row)
    assert world.containers[f'serverkit-slot-{row.id}-b']['running'] is False
    assert world.containers[f'serverkit-app-{row.id}']['running'] is False, 'standby stopped too'
    assert AppSlot.query.filter_by(application_id=row.id, slot='a').one().state == 'stopped'


def test_container_listing_shows_live_standby_and_workers(app, world, monkeypatch):
    import json
    row = _live_app(world)
    _two_deploys(world, row)
    listed = [{'ID': '1', 'Names': f'serverkit-app-{row.id}', 'State': 'running'},
              {'ID': '2', 'Names': f'serverkit-slot-{row.id}-b', 'State': 'running'},
              {'ID': '3', 'Names': f'serverkit-app-{row.id}-worker', 'State': 'running'}]
    monkeypatch.setattr(DockerService, 'run', classmethod(lambda cls, args, **kw: {
        'success': True, 'output': '\n'.join(json.dumps(c) for c in listed)}))

    containers = DockerService.get_all_app_containers(row)

    assert [(c['name'], c['service']) for c in containers] == [
        (f'serverkit-slot-{row.id}-b', 'web'),
        (f'serverkit-app-{row.id}-worker', 'worker'),
        (f'serverkit-app-{row.id}', 'standby'),
    ]
    # Status, stats, terminal and logs resolve the live container by id.
    assert DockerService.get_app_container_id(row) == f'serverkit-slot-{row.id}-b'


def test_a_vhost_restore_point_brings_back_the_slot_it_pointed_at(app, world, monkeypatch):
    from app.services import restore_point_adapter_nginx as adapter
    from app.services.nginx_service import NginxService
    row = _live_app(world)
    _two_deploys(world, row)                              # live b; vhost -> b's port
    old_vhost = 'server {\n    location / {\n        proxy_pass http://127.0.0.1:8100;\n    }\n}\n'
    monkeypatch.setattr(adapter.os.path, 'exists', lambda p: True)
    monkeypatch.setattr(NginxService, 'read_vhost', classmethod(lambda cls, name: old_vhost))

    payload = adapter.capture(row.name)
    # The captured bytes point at 8100 = slot a, whatever app.port says now.
    assert payload['slot'] == {'active_slot': 'a', 'port': 8100}

    world.containers[f'serverkit-app-{row.id}']['running'] = False   # standby went cold
    monkeypatch.setattr(NginxService, 'write_vhost', classmethod(
        lambda cls, name, content, enable=True: {'success': True}))
    result = adapter.restore(row.name, payload)

    assert result['success'], result
    assert row.active_slot == 'a' and row.port == 8100
    assert world.containers[f'serverkit-app-{row.id}']['running'] is True


# ── §F: the Deploy Console walks the slot stages ─────────────────────────────

def test_a_slot_deploy_job_reports_each_stage_to_the_console(app, world, monkeypatch):
    from app.models.deployment_job import DeploymentJob
    from app.services.deployment_job_service import DeploymentJobService
    monkeypatch.setattr(DeploymentJobService, '_enqueue_app_deploy', classmethod(lambda cls, job: None))
    row = _live_app(world)
    _enable(row)

    queued = DeploymentJobService.enqueue_app_deploy(row)
    job = db.session.get(DeploymentJob, queued['job_id'])
    steps = [s['name'] for s in job.get_plan()['steps']]
    assert steps == ['Preflight', 'Build', 'Snapshot', 'Release', 'Boot slot B', 'Health gate',
                     'Switch', 'Watch']

    from app.services.run_log_service import RunLogStream
    reached = []
    real = RunLogStream.set_step

    def spy(self, index, name):
        reached.append((index, name))
        return real(self, index, name)

    monkeypatch.setattr(RunLogStream, 'set_step', spy)

    result = DeploymentJobService.run_job(job.id)

    assert result['success'], result
    assert reached == [(1, 'Preflight'), (2, 'Build'), (3, 'Snapshot'), (4, 'Release'),
                       (5, 'Boot slot B'), (6, 'Health gate'), (7, 'Switch'), (8, 'Watch')]


def test_an_in_place_deploy_job_keeps_its_three_steps(app, world, monkeypatch):
    from app.models.deployment_job import DeploymentJob
    from app.services.deployment_job_service import DeploymentJobService
    monkeypatch.setattr(DeploymentJobService, '_enqueue_app_deploy', classmethod(lambda cls, job: None))
    row = _live_app(world)
    queued = DeploymentJobService.enqueue_app_deploy(row)
    job = db.session.get(DeploymentJob, queued['job_id'])
    assert [s['name'] for s in job.get_plan()['steps']] == [
        'Prepare deployment', 'Build application', 'Start containers']


# ── §D: release command + DB snapshot ────────────────────────────────────────

@pytest.fixture
def release_world(world, monkeypatch, tmp_path):
    """The world, plus a Procfile with a release line and a recording docker run."""
    (tmp_path / 'Procfile').write_text('web: node server.js\nrelease: npm run migrate\n')
    world.releases = []
    world.release_fails = False

    def run(cls, args, **kw):
        if args[:2] == ['run', '--rm']:
            world.releases.append((args[args.index('sh') - 1], args[-1]))
            world.note('release')
            if world.release_fails:
                return {'success': False, 'error': 'exit status 1', 'output': 'migration 42 failed'}
        return {'success': True, 'output': ''}

    monkeypatch.setattr(DockerService, 'run', classmethod(run))
    from app.services.service_connection_service import ServiceConnectionService
    monkeypatch.setattr(ServiceConnectionService, 'needs_shared_network',
                        classmethod(lambda cls, app: False))
    world.root = tmp_path
    return world


def _order(world):
    return [event.split(' ')[0] for event, _ in world.timeline]


def test_the_release_command_runs_once_in_the_new_image_before_the_boot(app, release_world):
    world = release_world
    row = _live_app(world, root_path=str(world.root))
    _enable(row)
    result = DeploymentService.deploy(row.id)
    assert result['success'], result
    image = result['deployment']['image_tag']
    assert world.releases == [(image, 'npm run migrate')]
    order = _order(world)
    assert order.index('release') < order.index('run'), 'release must run before the new slot boots'
    assert Deployment.query.get(result['deployment']['id']).get_metadata()['release_ran'] is True
    assert world.outage() == []


def test_a_failed_release_aborts_with_the_live_slot_untouched(app, release_world):
    world = release_world
    world.release_fails = True
    row = _live_app(world, root_path=str(world.root))
    _enable(row)
    result = DeploymentService.deploy(row.id)
    assert result['success'] is False and result['still_serving'] is True
    assert 'release command (Procfile) failed' in result['error']
    assert world.ran == [] and row.active_slot == 'a' and world.outage() == []
    assert 'app.deploy_aborted' in world.notified


def test_a_rollback_never_reruns_the_release(app, release_world):
    world = release_world
    row = _live_app(world, root_path=str(world.root))
    _enable(row)
    assert DeploymentService.deploy(row.id)['success']
    assert len(world.releases) == 1
    assert SlotDeployService.switch_back(row)['success']
    assert len(world.releases) == 1


def test_stop_old_before_release_is_announced_downtime_that_heals_on_failure(app, release_world):
    world = release_world
    world.release_fails = True
    row = _live_app(world, root_path=str(world.root))
    _enable(row)
    deploy_settings.update(row, {'stop_old_before_release': True})
    db.session.commit()
    result = DeploymentService.deploy(row.id)
    assert result['success'] is False and result['still_serving'] is True
    # Down during the release (the operator chose that), serving again after.
    assert world.outage() == [f'stop serverkit-app-{row.id}', 'release']
    assert world.timeline[-1][1] is True
    assert world.containers[f'serverkit-app-{row.id}']['running'] is True


def test_the_release_command_can_come_from_settings(app, release_world):
    world = release_world
    row = _live_app(world, root_path=str(world.root))
    _enable(row)
    deploy_settings.update(row, {'release_command': 'python manage.py migrate'})
    db.session.commit()
    assert DeploymentService.deploy(row.id)['success']
    assert world.releases[0][1] == 'python manage.py migrate'


def _owned_db(row):
    from app.models.managed_database import ManagedDatabase
    managed = ManagedDatabase(engine='postgresql', name='shopdb', host='localhost', port=5432,
                              owner_application_id=row.id)
    db.session.add(managed)
    db.session.commit()
    return managed


def test_owned_databases_are_snapshotted_before_the_release(app, release_world, monkeypatch):
    from app.services.backup_service import BackupService
    world = release_world
    dumped = []
    monkeypatch.setattr(BackupService, 'backup_database', classmethod(
        lambda cls, **kw: (dumped.append(kw['db_name']), world.note('dump'),
                           {'success': True, 'path': '/backups/shopdb.sql.gz'})[2]))
    row = _live_app(world, root_path=str(world.root))
    _owned_db(row)
    _enable(row)
    result = DeploymentService.deploy(row.id)
    assert result['success'], result
    assert dumped == ['shopdb']
    order = _order(world)
    assert order.index('dump') < order.index('release')
    snaps = Deployment.query.get(result['deployment']['id']).get_metadata()['db_snapshots']
    assert snaps[0]['path'] == '/backups/shopdb.sql.gz'


def test_a_failed_snapshot_warns_but_never_blocks(app, release_world, monkeypatch):
    from app.services.backup_service import BackupService
    world = release_world
    monkeypatch.setattr(BackupService, 'backup_database', classmethod(
        lambda cls, **kw: {'success': False, 'error': 'pg_dump: connection refused'}))
    lines = []
    row = _live_app(world, root_path=str(world.root))
    _owned_db(row)
    _enable(row)
    result = DeploymentService.deploy(row.id, log_callback=lines.append)
    assert result['success'], result
    assert any('snapshot of shopdb failed' in line for line in lines)


def test_after_a_switch_back_the_database_restore_is_offered_then_applied(
        app, release_world, monkeypatch, client, auth_headers):
    from app.services.backup_service import BackupService
    world = release_world
    monkeypatch.setattr(BackupService, 'backup_database', classmethod(
        lambda cls, **kw: {'success': True, 'path': '/backups/shopdb.sql.gz'}))
    restored = []
    monkeypatch.setattr(BackupService, 'restore_database', classmethod(
        lambda cls, path, db_type, db_name, **kw: restored.append((path, db_name)) or {'success': True}))
    row = _live_app(world, root_path=str(world.root))
    _owned_db(row)
    _enable(row)
    bad = DeploymentService.deploy(row.id)['deployment']
    assert SlotDeployService.status(row)['restorable_db'] is None, 'nothing to undo while it serves'
    assert SlotDeployService.switch_back(row)['success']

    offer = SlotDeployService.status(row)['restorable_db']
    assert offer['deployment_id'] == bad['id'] and offer['databases'] == ['shopdb']

    resp = client.post(f'/api/v1/apps/{row.id}/slots/restore-db', headers=auth_headers,
                       json={'deployment_id': bad['id']})
    assert resp.status_code == 200, resp.get_json()
    assert restored == [('/backups/shopdb.sql.gz', 'shopdb')]
    assert SlotDeployService.status(row)['restorable_db'] is None
