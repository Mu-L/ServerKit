"""Git-based Docker apps: a compose repository deploys as its compose project,
and every deploy of a Git app builds the configured branch's latest commit.

Found by a deploy benchmark (2026-09-24): importing a repo with
both a Dockerfile and a docker-compose.yml started the web container without
its database; "Deploy latest" rebuilt the old checkout; a push webhook pulled
code but never rebuilt the containers.
"""
import subprocess

import pytest

from app import db
from app.models.application import Application
from app.services import deploy_preflight_service as preflight
from app.services.build_service import BuildService
from app.services.deployment_service import DeploymentService
from app.services.docker_service import DockerService
from app.services.git_service import GitService


# --- import ---------------------------------------------------------------

def _mock_import(monkeypatch, strategy, manifests):
    from app.services.repository_manifest_service import RepositoryManifestService
    from app.services.manifest_persistence_service import ManifestPersistenceService
    from app.services.deployment_job_service import DeploymentJobService

    monkeypatch.setattr(GitService, 'clone_repository', classmethod(lambda cls, *a, **k: {'success': True}))
    monkeypatch.setattr(RepositoryManifestService, 'analyze_path', classmethod(lambda cls, *a, **k: {
        'recommended': {'app_type': 'docker', 'build_method': 'dockerfile', 'port': 3000},
        'strategy': strategy, 'manifests': manifests}))
    monkeypatch.setattr(BuildService, 'detect_build_method', classmethod(lambda cls, *a, **k: {
        'build_method': 'dockerfile', 'has_dockerfile': True, 'has_docker_compose': True}))
    monkeypatch.setattr(GitService, 'configure_deployment', classmethod(lambda cls, *a, **k: {
        'success': True, 'webhook_url': 'http://panel/hooks/1'}))
    monkeypatch.setattr(BuildService, 'configure_build', classmethod(lambda cls, *a, **k: {
        'success': True, 'config': {'build_method': 'dockerfile'}}))
    monkeypatch.setattr(ManifestPersistenceService, 'apply_import', classmethod(lambda cls, *a, **k: None))
    monkeypatch.setattr(DeploymentJobService, 'enqueue_app_deploy',
                        classmethod(lambda cls, app, **k: {'success': True, 'job_id': 'dj-1'}))


COMPOSE = [{'type': 'docker_compose', 'file': 'docker-compose.yml', 'label': 'Docker Compose', 'summary': ''},
           {'type': 'dockerfile', 'file': 'Dockerfile', 'label': 'Dockerfile', 'summary': ''}]


def test_a_compose_repository_imports_as_its_compose_project(app, client, auth_headers, monkeypatch):
    _mock_import(monkeypatch, 'docker_compose', COMPOSE)
    res = client.post('/api/v1/apps/from-repository', headers=auth_headers, json={
        'name': 'bench-app', 'repo_url': 'https://github.com/acme/bench.git'})
    assert res.status_code == 201
    created = Application.query.filter_by(name='bench-app').first()
    assert created.compose_file == 'docker-compose.yml'
    assert created.managed_by == 'docker_compose'
    assert created.app_type == 'docker'


@pytest.mark.parametrize('choice', [{'build_method': 'nixpacks'}, {'app_type': 'static'}])
def test_an_explicit_non_compose_choice_still_wins(app, client, auth_headers, monkeypatch, choice):
    _mock_import(monkeypatch, 'docker_compose', COMPOSE)
    res = client.post('/api/v1/apps/from-repository', headers=auth_headers, json={
        'name': 'chosen-app', 'repo_url': 'https://github.com/acme/bench.git', **choice})
    assert res.status_code == 201
    created = Application.query.filter_by(name='chosen-app').first()
    assert created.compose_file is None and created.managed_by is None


def test_a_dockerfile_only_repository_is_unchanged(app, client, auth_headers, monkeypatch):
    _mock_import(monkeypatch, 'dockerfile', [COMPOSE[1]])
    res = client.post('/api/v1/apps/from-repository', headers=auth_headers, json={
        'name': 'image-app', 'repo_url': 'https://github.com/acme/image.git'})
    assert res.status_code == 201
    assert Application.query.filter_by(name='image-app').first().compose_file is None


# --- deploy ---------------------------------------------------------------

def _app(compose=True, app_type='docker'):
    row = Application(name='bench', app_type=app_type, status='running', root_path='/srv/bench', user_id=1,
                      compose_file='docker-compose.yml' if compose else None,
                      managed_by='docker_compose' if compose else None)
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def calls(monkeypatch):
    """Record the order of every step the deploy takes; nothing touches Docker or Git."""
    seen = []
    monkeypatch.setattr(GitService, 'get_commit_info', classmethod(lambda cls, *a: None))
    monkeypatch.setattr(GitService, 'get_app_config', classmethod(lambda cls, app_id: {'branch': 'next'}))
    monkeypatch.setattr(GitService, 'pull_changes', classmethod(
        lambda cls, path, branch=None: seen.append(('pull', branch)) or {'success': True}))
    monkeypatch.setattr(preflight, 'preflight_compose_project',
                        lambda path, compose_file=None, **k: seen.append(('preflight', compose_file))
                        or preflight.PreflightResult())
    monkeypatch.setattr(DockerService, 'compose_up', classmethod(
        lambda cls, path, detach=True, build=False, compose_file=None:
        seen.append(('up', build, compose_file)) or {'success': True}))
    monkeypatch.setattr(BuildService, 'build', classmethod(
        lambda cls, app_id, **k: seen.append(('image_build',)) or {'success': True, 'image_tag': 'bench:1'}))
    monkeypatch.setattr(DeploymentService, '_deploy_application', classmethod(
        lambda cls, app, deployment, log=None: seen.append(('run_image',)) or {'success': True}))
    return seen


def test_a_compose_app_pulls_then_deploys_every_service(app, calls):
    row = _app()
    result = DeploymentService.deploy(row.id)
    assert result['success'], result
    # Latest commit first, then the whole project: never the single-image path.
    assert calls == [('pull', 'next'), ('preflight', 'docker-compose.yml'), ('up', True, 'docker-compose.yml')]
    assert db.session.get(Application, row.id).status == 'running'


def test_a_failed_compose_preflight_stops_nothing(app, calls, monkeypatch):
    failing = preflight.PreflightResult()
    failing.add(preflight.PreflightFinding(kind=preflight.KIND_COMPOSE_INVALID, message='bad compose'))
    monkeypatch.setattr(preflight, 'preflight_compose_project', lambda *a, **k: failing)
    row = _app()
    result = DeploymentService.deploy(row.id)
    assert not result['success'] and 'bad compose' in result['error']
    assert ('up', True, 'docker-compose.yml') not in calls


def test_an_image_app_from_git_builds_the_latest_commit(app, calls):
    row = _app(compose=False)
    assert DeploymentService.deploy(row.id)['success']
    assert calls == [('pull', 'next'), ('image_build',), ('run_image',)]


def test_a_failed_pull_fails_before_building(app, calls, monkeypatch):
    monkeypatch.setattr(GitService, 'pull_changes',
                        classmethod(lambda cls, *a, **k: {'success': False, 'error': 'network down'}))
    row = _app(compose=False)
    result = DeploymentService.deploy(row.id)
    assert not result['success'] and result['error'] == 'network down'
    assert ('image_build',) not in calls


def test_an_app_without_git_is_not_pulled(app, calls, monkeypatch):
    monkeypatch.setattr(GitService, 'get_app_config', classmethod(lambda cls, app_id: None))
    row = _app()
    assert DeploymentService.deploy(row.id)['success']
    assert not any(step[0] == 'pull' for step in calls)


# --- push webhook / Deploy tab -------------------------------------------

def test_a_docker_app_webhook_deploy_queues_a_rebuild(app, monkeypatch):
    from app.services.deployment_job_service import DeploymentJobService
    row = _app()
    queued, pulled = [], []
    monkeypatch.setattr(GitService, 'get_app_config',
                        classmethod(lambda cls, app_id: {'app_path': '/srv/bench', 'branch': 'main'}))
    monkeypatch.setattr(GitService, 'pull_changes',
                        classmethod(lambda cls, path, branch=None: pulled.append(branch) or {'success': True}))
    monkeypatch.setattr(DeploymentJobService, 'enqueue_app_deploy', classmethod(
        lambda cls, app, trigger='install', **k: queued.append((app.id, trigger)) or
        {'success': True, 'job_id': 'dj-9'}))
    result = GitService.deploy(row.id, trigger='webhook')
    assert result == {'success': True, 'message': 'Deployment queued', 'deploy_job_id': 'dj-9'}
    # Pulled synchronously too, so serverkit.yaml re-sync reads the new commit.
    assert pulled == ['main'] and queued == [(row.id, 'webhook')]


def test_a_non_docker_app_keeps_its_pull_deploy(app, monkeypatch):
    from app.services.deployment_job_service import DeploymentJobService
    row = _app(compose=False, app_type='flask')
    monkeypatch.setattr(GitService, 'get_app_config',
                        classmethod(lambda cls, app_id: {'app_path': '/srv/bench', 'branch': 'main'}))
    monkeypatch.setattr(GitService, 'pull_changes',
                        classmethod(lambda cls, *a, **k: {'success': True, 'commit': None}))
    monkeypatch.setattr(GitService, 'get_config', classmethod(lambda cls: {'apps': {str(row.id): {}}}))
    monkeypatch.setattr(GitService, 'save_config', classmethod(lambda cls, config: None))
    monkeypatch.setattr(GitService, '_log_deployment', classmethod(lambda cls, log: None))
    monkeypatch.setattr(DeploymentJobService, 'enqueue_app_deploy',
                        classmethod(lambda cls, *a, **k: pytest.fail('non-Docker apps are not rebuilt')))
    assert GitService.deploy(row.id)['message'] == 'Deployment completed successfully'


# --- branch switch on a single-branch clone ------------------------------

def _git(*args, cwd=None):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(subprocess.run(['git', '--version'], capture_output=True).returncode != 0,
                    reason='git is not installed')
def test_pull_reaches_a_branch_the_single_branch_clone_never_fetched(tmp_path, monkeypatch):
    # The panel pins remote transports to https/ssh; this test's origin is a
    # local directory, so allow file:// here only. The git commands are real.
    import os
    import app.services.git_service as git_module
    monkeypatch.setattr(git_module, 'git_argv', lambda *args: ['git', *args])
    monkeypatch.setattr(git_module, 'git_env', lambda: {**os.environ, 'GIT_ALLOW_PROTOCOL': 'file'})
    origin, work = tmp_path / 'origin', tmp_path / 'work'
    _git('init', '-q', '-b', 'main', str(origin))
    for key, value in (('user.email', 't@example.com'), ('user.name', 't')):
        _git('config', key, value, cwd=origin)
    (origin / 'VERSION').write_text('1.0.0\n')
    _git('add', '.', cwd=origin)
    _git('commit', '-q', '-m', 'main', cwd=origin)
    _git('checkout', '-q', '-b', 'next', cwd=origin)
    (origin / 'VERSION').write_text('1.0.1\n')
    _git('commit', '-q', '-am', 'next', cwd=origin)
    _git('checkout', '-q', 'main', cwd=origin)
    # How imports clone: one branch only.
    _git('clone', '-q', '--branch', 'main', '--single-branch', str(origin), str(work))

    result = GitService.pull_changes(str(work), 'next')
    assert result['success'], result
    assert (work / 'VERSION').read_text() == '1.0.1\n'
