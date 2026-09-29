"""Plan 87 §A — deploy foundations. One regression test per verified bug; each
fails on the code before the fix.

- A1  every build was tagged :latest, so a rollback redeployed the NEW image
- A2  commit_hash was read before the pull (named the old commit)
- A3  the health wait never failed a deploy, and counted a 4xx as healthy
- A4  health path + timeout are editable settings
- A5  two deploys of one app raced
- A6  a compose rollback changed files and restarted nothing; rollback with no
      version found no target at all
"""
import threading
import time
import types

import pytest

from factories import make_application

from app import db
from app.models.deployment import Deployment
from app.services import deploy_lock, deploy_settings, health_gate
from app.services import deploy_preflight_service as preflight
from app.services.build_service import BuildService
from app.services.deployment_service import DeploymentService
from app.services.docker_service import DockerService
from app.services.git_service import GitService
from app.services.health_gate import HealthGateError


def _image_app(**kw):
    fields = dict(name='imgapp', app_type='docker', status='running', root_path='/srv/img',
                  docker_image=None, compose_file=None, managed_by=None, port=9300)
    fields.update(kw)
    return make_application(db, **fields)


@pytest.fixture
def image_deploy(monkeypatch):
    """Stub build + run so deploy()/rollback() touch no Docker or Git."""
    seen = {'built': [], 'ran': []}
    monkeypatch.setattr(GitService, 'get_commit_info', classmethod(lambda cls, *a: None))
    monkeypatch.setattr(GitService, 'get_app_config', classmethod(lambda cls, app_id: None))
    monkeypatch.setattr(BuildService, 'get_app_build_config', classmethod(lambda cls, app_id: None))

    def build(cls, app_id, no_cache=False, log_callback=None, image_tag=None):
        tag = image_tag or f'serverkit-app-{app_id}:latest'
        seen['built'].append(tag)
        return {'success': True, 'image_tag': tag}

    monkeypatch.setattr(BuildService, 'build', classmethod(build))
    monkeypatch.setattr(DeploymentService, '_deploy_docker', classmethod(
        lambda cls, app, deployment, log_callback=None:
        seen['ran'].append(deployment.image_tag) or {'success': True, 'container_id': 'c1'}))
    monkeypatch.setattr(DockerService, 'run', classmethod(
        lambda cls, args, **kw: {'success': True, 'output': ''}))
    return seen


# ── A1 immutable image tags ──────────────────────────────────────────────────

def test_each_deploy_builds_its_own_immutable_tag(app, image_deploy):
    row = _image_app()
    first = DeploymentService.deploy(row.id)['deployment']
    second = DeploymentService.deploy(row.id)['deployment']
    assert first['image_tag'] == f'serverkit-app-{row.id}:d{first["id"]}'
    assert second['image_tag'] == f'serverkit-app-{row.id}:d{second["id"]}'
    assert first['image_tag'] != second['image_tag']


def test_rollback_redeploys_the_image_that_actually_ran(app, image_deploy):
    row = _image_app()
    first = DeploymentService.deploy(row.id)['deployment']
    second = DeploymentService.deploy(row.id)['deployment']
    result = DeploymentService.rollback(row.id, target_version=first['version'])
    assert result['success'], result
    # Rolled back to v1's own image — not the tag v2's build overwrote.
    assert image_deploy['ran'][-1] == first['image_tag']
    assert image_deploy['ran'][-1] != second['image_tag']


def test_old_deploy_images_are_pruned_but_recent_and_live_kept(app, monkeypatch):
    from app.services import app_image_retention
    row = _image_app()
    rows = []
    for version in range(1, 6):
        dep = Deployment(app_id=row.id, version=version, status='rolled_back')
        db.session.add(dep)
        db.session.flush()
        dep.image_tag = app_image_retention.deploy_image_tag(row.id, dep.id)
        rows.append(dep)
    rows[0].status = 'live'   # an old deployment that is somehow still live
    db.session.commit()
    tags = [f'd{d.id}' for d in rows] + ['latest']
    monkeypatch.setattr(DockerService, 'run', classmethod(
        lambda cls, args, **kw: {'success': True, 'output': '\n'.join(tags)}))
    removed = []
    monkeypatch.setattr(DockerService, 'remove_image', staticmethod(
        lambda ref, force=False: removed.append(ref) or {'success': True}))

    app_image_retention.prune(row.id, keep=2, protect={rows[1].image_tag})

    # newest two + live (rows[0]) + protected (rows[1]) survive; :latest untouched
    assert removed == [rows[2].image_tag]


# ── A2 commit recorded after the pull ────────────────────────────────────────

def test_the_deployment_records_the_commit_the_pull_checked_out(app, image_deploy, monkeypatch):
    head = {'hash': 'a' * 40, 'message': 'old'}
    monkeypatch.setattr(GitService, 'get_commit_info', classmethod(lambda cls, *a: dict(head)))
    monkeypatch.setattr(GitService, 'get_app_config', classmethod(lambda cls, app_id: {'branch': 'main'}))

    def pull(cls, path, branch=None):
        head.update(hash='b' * 40, message='new')
        return {'success': True}

    monkeypatch.setattr(GitService, 'pull_changes', classmethod(pull))
    row = _image_app()
    result = DeploymentService.deploy(row.id)
    assert result['deployment']['commit_hash'] == 'b' * 40
    assert result['deployment']['commit_message'] == 'new'


# ── A3 the health gate ───────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _gate(statuses, **kw):
    clock = _Clock()
    answers = iter(statuses)
    calls = []

    def probe(url, host_header=None):
        calls.append(url)
        status = next(answers, statuses[-1])
        return (status, None) if isinstance(status, int) else (None, status)

    kw.setdefault('timeout', 10)
    result = health_gate.wait_healthy(9300, kw.pop('path', '/health'), _probe=probe,
                                      _sleep=clock.sleep, _clock=clock, **kw)
    return result, calls


def test_a_4xx_on_the_health_path_fails_the_gate():
    with pytest.raises(HealthGateError) as info:
        _gate([404])
    assert info.value.last == 404


def test_a_4xx_passes_only_when_explicitly_allowed():
    result, _ = _gate([401], allow_4xx=True)
    assert result['status'] == 401


def test_a_release_that_never_answers_fails_after_the_timeout():
    with pytest.raises(HealthGateError) as info:
        _gate(['Connection refused'], timeout=5)
    assert 'did not pass within 5s' in str(info.value)


def test_it_takes_consecutive_passes_a_blip_resets_the_streak():
    result, calls = _gate([200, 500, 200, 200, 200], consecutive=3)
    assert result['status'] == 200 and len(calls) == 5


def test_an_exited_container_fails_at_once_not_after_the_timeout():
    with pytest.raises(HealthGateError) as info:
        _gate([200], container='c', timeout=600,
              _container_health=lambda name: ('exited', ''))
    assert 'exited' in str(info.value)


def test_a_docker_healthcheck_still_starting_is_not_a_pass():
    states = iter([('running', 'starting')] * 2 + [('running', 'healthy')] * 3)
    result, calls = _gate([200], container='c', consecutive=3,
                          _container_health=lambda name: next(states))
    assert len(calls) == 5


def test_the_gate_reads_the_apps_settings(app):
    row = _image_app(healthcheck_path='ready')
    deploy_settings.update(row, {'healthcheck_timeout': 7, 'healthcheck_allow_4xx': True})
    seen = {}

    def fake(port, path, **kw):
        seen.update(port=port, path=path, **kw)
        return {'url': 'u', 'status': 200}

    original = health_gate.wait_healthy
    health_gate.wait_healthy = fake
    try:
        health_gate.gate_for_app(row)
    finally:
        health_gate.wait_healthy = original
    assert seen['port'] == 9300 and seen['path'] == 'ready'
    assert seen['timeout'] == 7 and seen['allow_4xx'] is True and seen['consecutive'] == 3


def test_a_rolling_restart_whose_new_container_is_unhealthy_fails(app, monkeypatch):
    from app.services import git_deploy_service
    from app.services.git_deploy_service import GitDeployService
    row = _image_app(healthcheck_path='/health')
    commands = []
    monkeypatch.setattr(git_deploy_service, 'run_checked', lambda cmd, **kw: (
        commands.append(cmd) or {'success': True, 'output': '', 'error': None}))

    def unhealthy(app, port=None, **kw):
        raise HealthGateError('Health check did not pass within 120s')

    monkeypatch.setattr(health_gate, 'gate_for_app', unhealthy)
    result = GitDeployService._zero_downtime_restart(row)
    assert result['success'] is False and 'did not pass' in result['error']
    # ... and it still scaled back to one copy.
    assert commands[-1][-1] == 'web=1'


# ── A4 settings API ──────────────────────────────────────────────────────────

def test_health_settings_round_trip_through_the_app_api(app, client, auth_headers):
    row = _image_app()
    resp = client.put(f'/api/v1/apps/{row.id}', headers=auth_headers, json={
        'healthcheck_path': '/ready',
        'deploy_settings': {'healthcheck_timeout': 45, 'healthcheck_allow_4xx': True},
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()['app']
    assert body['healthcheck_path'] == '/ready'
    assert body['deploy_settings']['healthcheck_timeout'] == 45
    assert body['deploy_settings']['healthcheck_allow_4xx'] is True
    assert body['deploy_settings']['healthcheck_consecutive'] == 3


@pytest.mark.parametrize('bad', [{'healthcheck_timeout': 0}, {'healthcheck_timeout': 'x'},
                                 {'nope': 1}, {'healthcheck_allow_4xx': 'yes'}])
def test_bad_deploy_settings_are_rejected(app, client, auth_headers, bad):
    row = _image_app()
    resp = client.put(f'/api/v1/apps/{row.id}', headers=auth_headers,
                      json={'deploy_settings': bad})
    assert resp.status_code == 400


# ── A5 per-app deploy lock ───────────────────────────────────────────────────

def test_a_second_deploy_of_the_same_app_waits_for_the_first():
    order = []
    release_first = threading.Event()

    def first():
        with deploy_lock.deploy_lock(41, 'first'):
            order.append('first-start')
            release_first.wait(5)
            order.append('first-end')

    def second():
        with deploy_lock.deploy_lock(41, 'second'):
            order.append('second-start')

    t1 = threading.Thread(target=first)
    t1.start()
    while 'first-start' not in order:
        time.sleep(0.01)
    t2 = threading.Thread(target=second)
    t2.start()
    time.sleep(0.1)
    assert order == ['first-start'], 'the second deploy ran while the first held the app'
    release_first.set()
    t1.join(5)
    t2.join(5)
    assert order == ['first-start', 'first-end', 'second-start']


def test_other_apps_do_not_wait():
    with deploy_lock.deploy_lock(51, 'a'):
        done = []
        t = threading.Thread(target=lambda: deploy_lock.deploy_lock(52, 'b').__enter__() or done.append(1))
        t.start()
        t.join(2)
        assert done == [1]


def test_the_lock_is_reentrant_for_a_deploy_that_rolls_itself_back():
    with deploy_lock.deploy_lock(61, 'deploy'):
        with deploy_lock.deploy_lock(61, 'auto-revert'):
            assert deploy_lock.holder(61) == 'deploy'
    assert not deploy_lock.is_locked(61)


def test_every_redeploy_path_holds_the_lock(app, image_deploy, monkeypatch):
    from app.services.git_deploy_service import GitDeployService
    from app.services.template_service import TemplateService
    row = _image_app()
    held = {}

    def spy(label):
        def record(*a, **k):
            held[label] = deploy_lock.is_locked(row.id)
            raise RuntimeError('stop here')
        return record

    monkeypatch.setattr(BuildService, 'get_app_build_config', classmethod(spy('deploy')))
    with pytest.raises(RuntimeError):
        DeploymentService.deploy(row.id)
    monkeypatch.setattr(GitDeployService, '_create_snapshot', classmethod(spy('webhook')))
    GitDeployService.deploy(row.id)
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(spy('template')))
    with pytest.raises(RuntimeError):
        TemplateService.update_app(row.id)
    monkeypatch.setattr(Deployment, 'get_current', classmethod(spy('rollback')))
    with pytest.raises(RuntimeError):
        DeploymentService.rollback(row.id)
    assert held == {'deploy': True, 'webhook': True, 'template': True, 'rollback': True}
    assert not deploy_lock.is_locked(row.id)


# ── A6 rollback actually restarts ────────────────────────────────────────────

def _compose_history(row):
    for version, status, sha in ((1, 'rolled_back', 'a' * 40), (2, 'live', 'b' * 40)):
        db.session.add(Deployment(app_id=row.id, version=version, status=status,
                                  commit_hash=sha, deploy_completed_at=db.func.now()))
    db.session.commit()


def test_rollback_without_a_version_finds_the_release_before(app, monkeypatch):
    row = _image_app(compose_file='docker-compose.yml')
    _compose_history(row)
    target = Deployment.get_previous(row.id, 2)
    assert target is not None and target.version == 1


def test_a_compose_rollback_recreates_the_containers(app, monkeypatch):
    from app.services import deployment_service
    row = _image_app(compose_file='docker-compose.yml')
    _compose_history(row)
    steps = []
    monkeypatch.setattr(GitService, 'get_app_config', classmethod(lambda cls, app_id: None))
    monkeypatch.setattr(deployment_service, 'run_checked', lambda cmd, **kw: (
        steps.append(('git', cmd[-1])) or {'success': True, 'output': '', 'error': None}))
    monkeypatch.setattr(preflight, 'preflight_compose_project',
                        lambda path, compose_file=None, **k: steps.append(('preflight',))
                        or preflight.PreflightResult())
    monkeypatch.setattr(DockerService, 'compose_up', classmethod(
        lambda cls, path, detach=True, build=False, compose_file=None:
        steps.append(('up', build)) or {'success': True}))

    result = DeploymentService.rollback(row.id)

    assert result['success'], result
    assert steps == [('git', 'a' * 40), ('preflight',), ('up', True)]


def test_a_failed_template_update_says_when_the_restore_failed_too(app, monkeypatch, tmp_path):
    from app.services.template_service import TemplateService
    row = _image_app(root_path=str(tmp_path))
    (tmp_path / 'docker-compose.yml').write_text('services: {web: {image: old}}')
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'installed': {str(row.id): {'template_id': 't'}}}))
    monkeypatch.setattr(TemplateService, 'get_template', classmethod(
        lambda cls, tid: {'success': True, 'template': {'name': 't', 'version': '2'}}))
    monkeypatch.setattr(TemplateService, 'generate_compose', classmethod(
        lambda cls, template, variables: 'services: {web: {image: new}}'))
    monkeypatch.setattr(preflight, 'preflight_compose_project',
                        lambda *a, **k: preflight.PreflightResult())
    monkeypatch.setattr(DockerService, 'compose_down', classmethod(lambda cls, path, **k: {'success': True}))
    monkeypatch.setattr(DockerService, 'compose_pull', classmethod(lambda cls, path, **k: {'success': True}))
    monkeypatch.setattr(DockerService, 'compose_up', classmethod(
        lambda cls, path, **k: {'success': False, 'error': 'port is already allocated'}))

    result = TemplateService.update_app(row.id)

    assert result['success'] is False and result['restored'] is False
    assert 'the app is down' in result['error']
