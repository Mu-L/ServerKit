"""Proving tests for Phase 2 — the manifest apply engine (plan 17)."""

import pytest

import app.models.application_manifest  # noqa: F401
from app.services.manifest_spec_service import ManifestSpecService
from app.services.manifest_apply_service import ManifestApplyService


MANIFEST = {
    'version': 1,
    'services': [
        {
            'name': 'api', 'type': 'web', 'port': 8000, 'healthCheckPath': '/health',
            'envVars': [{'key': 'LOG_LEVEL', 'value': 'info'},
                        {'key': 'SECRET_TOKEN', 'fromSecret': 'tok'}],
            'disks': [{'name': 'uploads', 'mountPath': '/data/uploads',
                       'backup': {'schedule': 'daily', 'retain': 7}}],
        },
        {'name': 'db', 'type': 'postgres', 'version': '16',
         'disk': {'size': '10GB', 'backup': {'schedule': 'daily', 'retain': 7}}},
    ],
    'domains': [{'host': 'api.example.com', 'service': 'api', 'ssl': 'auto'}],
}


@pytest.fixture
def project(app):
    from app import db
    from app.models import Project, Environment
    from app.services.workspace_service import WorkspaceService
    ws = WorkspaceService.ensure_default_workspace()
    proj = Project(workspace_id=ws.id, name='Shop', slug='shop')
    db.session.add(proj)
    db.session.commit()
    env = Environment(project_id=proj.id, name='Production', slug='production', is_default=True)
    db.session.add(env)
    db.session.commit()
    return proj


@pytest.fixture
def owner(app):
    from app import db
    from app.models import User
    user = User.query.filter_by(username='testadmin').first()
    if not user:
        user = User(username='testadmin', email='admin@test.local', role='admin')
        if hasattr(user, 'set_password'):
            user.set_password('admin')
        db.session.add(user)
        db.session.commit()
    return user


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    # avoid Docker / nginx / DNS in the harness
    from app.services.docker_service import DockerService
    from app.services.domain_attach_service import DomainAttachService
    monkeypatch.setattr(DockerService, 'create_volume',
                        classmethod(lambda cls, name, driver='local': {'success': True}))
    def _fake_attach(cls, app, host, ssl='auto', email=None, make_primary=False):
        # realistic: create the Domain row so idempotence is genuinely exercised
        from app import db as _db
        from app.models.domain import Domain
        if not any(d.name == host for d in (app.domains or [])):
            _db.session.add(Domain(name=host, application_id=app.id, is_primary=False))
            _db.session.commit()
        return {'success': True, 'domain': host, 'created': True, 'warnings': []}
    monkeypatch.setattr(DomainAttachService, 'attach', classmethod(_fake_attach))


def test_plan_is_ordered(project):
    n = ManifestSpecService.normalize(MANIFEST)
    plan = ManifestApplyService.plan(project, n)
    types = [s['type'] for s in plan['steps']]
    # dbs before consumers, domains last
    assert 'provision_db' in types
    assert types.index('provision_db') < types.index('create_app')
    assert types[-1] == 'attach_domain'
    # every declared piece shows up
    assert 'set_env' in types
    assert 'ensure_volume' in types
    assert types.count('upsert_backup_policy') == 2  # db + files


def test_apply_materializes_and_is_idempotent(project, owner):
    from app.models import Application
    from app.models.managed_database import ManagedDatabase
    from app.models.backup_policy import BackupPolicy
    n = ManifestSpecService.normalize(MANIFEST)

    result = ManifestApplyService.apply(project, n, user_id=owner.id)
    assert result['success'] is True, result
    assert result['applied'] >= 6

    api = Application.query.filter_by(project_id=project.id, name='api').first()
    assert api is not None
    assert api.port == 8000
    assert api.healthcheck_path == '/health'
    assert api.source == 'manifest'

    # env literal set; secret ref is BOUND (Phase 3) but resolves empty because
    # the referenced vault secret does not exist here
    from app.services.env_service import EnvService
    env = EnvService.get_effective_env(api.id)
    assert env.get('LOG_LEVEL') == 'info'
    assert env.get('SECRET_TOKEN') == ''

    # managed db recorded
    mdb = ManagedDatabase.query.filter_by(engine='postgresql', name='db').first()
    assert mdb is not None

    # backup policies: files (app) + database
    assert BackupPolicy.query.filter_by(target_type='files', target_id=api.id).count() == 1
    assert BackupPolicy.query.filter_by(target_type='database', target_id=mdb.id).count() == 1

    # volume created
    assert any(v.mount_path == '/data/uploads' for v in api.volumes)

    # IDEMPOTENCE: a second plan is empty
    plan2 = ManifestApplyService.plan(project, n)
    assert plan2['step_count'] == 0, plan2['summary']


def test_apply_records_job(project, owner):
    from app.models.deployment_job import DeploymentJob
    n = ManifestSpecService.normalize(MANIFEST)
    result = ManifestApplyService.apply(project, n, user_id=owner.id)
    job = DeploymentJob.query.get(result['job_id'])
    assert job is not None
    assert job.kind == 'manifest.apply'
    assert job.status == 'succeeded'


def test_plan_and_apply_endpoints(client, auth_headers, project):
    # plan (dry-run)
    resp = client.post('/api/v1/manifests/plan', headers=auth_headers,
                       json={'project_id': project.id, 'manifest': MANIFEST})
    assert resp.status_code == 200, resp.get_json()
    plan = resp.get_json()['plan']
    assert plan['step_count'] > 0

    # apply
    resp = client.post('/api/v1/manifests/apply', headers=auth_headers,
                       json={'project_id': project.id, 'manifest': MANIFEST})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['success'] is True

    # stored manifest is now retrievable + marked applied
    resp = client.get(f'/api/v1/manifests?project_id={project.id}', headers=auth_headers)
    assert resp.status_code == 200
    stored = resp.get_json()['manifest']
    assert stored['status'] == 'applied'

    # re-plan through the API is empty (idempotent)
    resp = client.post('/api/v1/manifests/plan', headers=auth_headers,
                       json={'project_id': project.id})
    assert resp.get_json()['plan']['step_count'] == 0


def test_redis_installs_the_engine_under_the_service_name(project, owner, monkeypatch):
    """`type: redis` used to log "declared but not provisioned" (plan 86 §C3)."""
    from app.services.deployment_job_service import DeploymentJobService
    from app.services.template_service import TemplateService
    monkeypatch.setattr(TemplateService, '_get_docker_used_ports', classmethod(lambda cls: set()))
    installs = []
    monkeypatch.setattr(DeploymentJobService, 'install_template', classmethod(
        lambda cls, **kw: installs.append(kw) or {'success': True, 'job_id': 'j1'}))
    n = ManifestSpecService.normalize({'version': 1,
                                       'services': [{'name': 'cache', 'type': 'redis'}]})

    plan = ManifestApplyService.plan(project, n)
    assert [s['type'] for s in plan['steps']] == ['provision_engine']
    result = ManifestApplyService.apply(project, n, user_id=owner.id)

    assert result['success'] is True, result
    assert [(i['template_id'], i['app_name']) for i in installs] == [('redis', 'cache')]


def test_an_installed_redis_is_not_installed_again(project, owner):
    from app import db
    from tests.factories import make_application
    make_application(db, name='cache')
    n = ManifestSpecService.normalize({'version': 1,
                                       'services': [{'name': 'cache', 'type': 'redis'}]})
    assert ManifestApplyService.plan(project, n)['steps'] == []


# -- Dockerfile builds ------------------------------------------------------

DOCKERFILE_MANIFEST = {
    'version': 1,
    'services': [
        {'name': 'api-worker', 'type': 'worker', 'autoDeploy': True,
         'dockerfilePath': 'services/api/Dockerfile',
         'envVars': [{'key': 'LOG_LEVEL', 'value': 'info'}]},
    ],
}


def _store_source(project, repo='https://example.com/acme/mono.git', ref='main'):
    from app import db
    from app.models.application_manifest import ApplicationManifest
    row = ApplicationManifest(project_id=project.id, source_repo=repo, source_ref=ref)
    db.session.add(row)
    db.session.commit()
    return row


def test_dockerfile_build_plans_and_blocks_without_source(project):
    n = ManifestSpecService.normalize(DOCKERFILE_MANIFEST)
    plan = ManifestApplyService.plan(project, n)
    create = next(s for s in plan['steps'] if s['type'] == 'create_app')
    assert create['payload']['dockerfile_path'] == 'services/api/Dockerfile'
    assert create['payload']['auto_deploy'] is True
    assert 'services/api/Dockerfile' in create['description']
    # no repository on record -> plan-time blocker (apply would refuse)
    assert any(b['kind'] == 'dockerfile_no_source' for b in plan['blockers'])


def test_dockerfile_build_applies_end_to_end(project, owner, monkeypatch, tmp_path):
    import os
    from app import paths
    from app.models import Application
    from app.services.git_service import GitService
    from app.services.build_service import BuildService

    monkeypatch.setattr(paths, 'APPS_DIR', str(tmp_path / 'apps'))
    calls = {}

    def _clone(cls, app_path, repo_url, branch='main'):
        os.makedirs(os.path.join(app_path, '.git'))
        calls['clone'] = {'path': app_path, 'repo': repo_url, 'branch': branch}
        return {'success': True, 'path': app_path}

    def _deploy(cls, app_id, app_path, repo_url, branch='main',
                auto_deploy=True, **kw):
        calls['deploy'] = {'app_id': app_id, 'repo': repo_url,
                           'branch': branch, 'auto_deploy': auto_deploy}
        return {'success': True}

    def _build(cls, app_id, app_path, **kw):
        calls['build'] = {'app_id': app_id, **kw}
        return {'success': True}

    monkeypatch.setattr(GitService, 'clone_repository', classmethod(_clone))
    monkeypatch.setattr(GitService, 'configure_deployment', classmethod(_deploy))
    monkeypatch.setattr(BuildService, 'configure_build', classmethod(_build))

    _store_source(project)
    n = ManifestSpecService.normalize(DOCKERFILE_MANIFEST)
    result = ManifestApplyService.apply(project, n, user_id=owner.id)
    assert result['success'] is True, result

    app_row = Application.query.filter_by(project_id=project.id,
                                          name='api-worker').first()
    assert app_row is not None
    assert app_row.root_path == calls['clone']['path']
    assert calls['clone']['repo'] == 'https://example.com/acme/mono.git'
    assert calls['deploy']['auto_deploy'] is True
    assert calls['build']['build_method'] == 'dockerfile'
    assert calls['build']['dockerfile_path'] == 'services/api/Dockerfile'


def test_dockerfile_build_remote_target_blocked(project):
    manifest = {'version': 1,
                'services': [{**DOCKERFILE_MANIFEST['services'][0],
                              'server': 'edge-1'}]}
    _store_source(project)
    n = ManifestSpecService.normalize(manifest)
    plan = ManifestApplyService.plan(project, n)
    assert any(b['kind'] in ('remote_target', 'observed_server')
               for b in plan['blockers'])
