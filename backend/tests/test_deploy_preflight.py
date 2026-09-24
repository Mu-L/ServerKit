"""Deploy preflight — plan 72 B.1.

The property under test is ORDERING, not cleverness: every redeploy path must
finish validating the new configuration *before* it stops anything. So most of
these tests make the preflight fail and then prove the destructive call was
never reached and the live configuration is byte-for-byte unchanged.

Covered:
- the compose preflight itself (validity, image resolution, port conflicts)
- port-conflict reuse of the manifest-apply probe, including the redeploy case
  where the port is held by the deploy's OWN containers (must not fire)
- GitDeployService._standard_restart      : compose_down not called on failure
- TemplateService.update_app              : compose_down not called, compose
                                            file on disk unchanged
- DeploymentService._deploy_docker        : stop/remove not called when the
                                            registry pull fails
- and, for each, that a valid deploy still deploys
"""
import os
import types

import pytest

from app import db
from app.models import Application, User
from app.services import deploy_preflight_service as preflight
from app.services.docker_service import DockerService
from app.services.manifest_apply_service import ManifestApplyService


# ── helpers ──────────────────────────────────────────────────────────────────

def _owner(username='pfowner'):
    user = User(email=f'{username}@test.local', username=username,
                password_hash='x', role=User.ROLE_ADMIN, is_active=True)
    db.session.add(user)
    db.session.commit()
    return user


def _app_row(root_path, name='pf-app', port=None):
    application = Application(name=name, app_type='docker', status='running',
                              root_path=str(root_path), port=port,
                              user_id=_owner(f'{name}-owner').id)
    db.session.add(application)
    db.session.commit()
    return application


def _stub_compose(monkeypatch, *, valid=True, config=None, build=True,
                  pull=True, ps=None, error='services.web.ports: invalid'):
    """Stub every docker seam the compose preflight touches; return a call log."""
    calls = []

    monkeypatch.setattr(DockerService, 'validate_compose_file', classmethod(
        lambda cls, path, compose_file=None: (
            calls.append('validate') or
            ({'valid': True} if valid else {'valid': False, 'error': error}))))
    monkeypatch.setattr(DockerService, 'get_compose_config', classmethod(
        lambda cls, path, compose_file=None: (
            calls.append('config') or
            {'success': True, 'config': config if config is not None
             else {'services': {'web': {'image': 'nginx:1.25'}}}})))
    monkeypatch.setattr(DockerService, 'compose_build', classmethod(
        lambda cls, path, service=None, compose_file=None, no_cache=False: (
            calls.append('build') or
            ({'success': True} if build else {'success': False, 'error': 'build blew up'}))))
    monkeypatch.setattr(DockerService, 'compose_pull', classmethod(
        lambda cls, path, service=None, compose_file=None: (
            calls.append('pull') or
            ({'success': True} if pull else {'success': False, 'error': 'manifest unknown'}))))
    monkeypatch.setattr(DockerService, 'compose_ps', classmethod(
        lambda cls, path, compose_file=None, **kw: (calls.append('ps') or (ps if ps is not None else []))))
    monkeypatch.setattr(DockerService, 'compose_down', classmethod(
        lambda cls, path, **kw: calls.append('DOWN') or {'success': True}))
    monkeypatch.setattr(DockerService, 'compose_up', classmethod(
        lambda cls, path, **kw: calls.append('UP') or {'success': True}))
    monkeypatch.setattr(ManifestApplyService, '_port_bound', staticmethod(lambda port: False))
    return calls


# ── the preflight unit ───────────────────────────────────────────────────────

def test_invalid_compose_is_blocking_and_stops_the_preflight(app, monkeypatch):
    calls = _stub_compose(monkeypatch, valid=False)
    lines = []

    result = preflight.preflight_compose_project('/srv/x', log=lines.append)

    assert result.ok is False
    assert [f.kind for f in result.blockers] == ['compose_invalid']
    assert 'services.web.ports: invalid' in result.error
    # Nothing past validation ran — no pull, no build, no probing.
    assert calls == ['validate']
    # The user-visible payoff line.
    assert any('left untouched' in line for line in lines)


def test_valid_compose_pulls_images_and_passes(app, monkeypatch):
    calls = _stub_compose(monkeypatch)

    result = preflight.preflight_compose_project('/srv/x')

    assert result.ok is True
    assert 'compose_config' in result.checks and 'image_pull' in result.checks
    assert 'pull' in calls and 'build' not in calls


def test_project_with_a_build_section_builds_before_anything_stops(app, monkeypatch):
    calls = _stub_compose(monkeypatch,
                          config={'services': {'api': {'build': {'context': '.'}}}})

    result = preflight.preflight_compose_project('/srv/x')

    assert result.ok is True
    assert 'image_build' in result.checks
    assert 'build' in calls and 'pull' not in calls


def test_mixed_project_builds_and_pulls_only_the_image_only_services(app, monkeypatch):
    """A whole-project `compose pull` also tries to pull the buildable service
    and fails on an image that only exists once built — so pull by name."""
    calls = _stub_compose(monkeypatch, config={'services': {
        'api': {'build': '.', 'image': 'local/api:dev'},
        'cache': {'image': 'redis:7'},
    }})
    pulled = []
    monkeypatch.setattr(DockerService, 'compose_pull', classmethod(
        lambda cls, path, service=None, compose_file=None: (
            pulled.append(service) or {'success': True})))

    result = preflight.preflight_compose_project('/srv/x')

    assert result.ok is True
    assert pulled == ['cache']
    assert 'build' in calls
    assert result.checks == ['compose_config', 'image_build', 'image_pull']


def test_build_failure_blocks(app, monkeypatch):
    _stub_compose(monkeypatch, build=False,
                  config={'services': {'api': {'build': '.'}}})

    result = preflight.preflight_compose_project('/srv/x')

    assert result.ok is False
    assert result.blockers[0].kind == 'build_failed'
    assert 'build blew up' in result.error


def test_unpullable_image_blocks_when_it_is_not_present_locally(app, monkeypatch):
    _stub_compose(monkeypatch, pull=False)
    monkeypatch.setattr(DockerService, 'image_exists', staticmethod(lambda ref: False))

    result = preflight.preflight_compose_project('/srv/x')

    assert result.ok is False
    assert result.blockers[0].kind == 'image_unresolved'
    assert 'nginx:1.25' in result.error


def test_unpullable_image_is_advisory_when_already_present_locally(app, monkeypatch):
    """A locally-tagged image that was never pushed cannot be pulled and
    deployed fine before B.1 existed — it must keep deploying."""
    _stub_compose(monkeypatch, pull=False)
    monkeypatch.setattr(DockerService, 'image_exists', staticmethod(lambda ref: True))

    result = preflight.preflight_compose_project('/srv/x')

    assert result.ok is True
    assert [f.severity for f in result.findings] == ['warning']


def test_port_conflict_is_reported_via_the_manifest_apply_probe(app, monkeypatch):
    _stub_compose(monkeypatch,
                  config={'services': {'web': {'image': 'nginx',
                                               'ports': ['8080:80']}}},
                  ps=[])
    probed = []
    monkeypatch.setattr(ManifestApplyService, '_port_bound',
                        staticmethod(lambda port: probed.append(port) or True))

    result = preflight.preflight_compose_project('/srv/x')

    assert probed == [8080]
    assert result.ok is False
    assert result.blockers[0].kind == 'port_conflict'
    assert '8080' in result.error


def test_a_port_the_project_already_publishes_is_not_a_conflict(app, monkeypatch):
    """The redeploy case. Probing a port our own live container holds would
    abort every ordinary redeploy, so owned ports are excluded."""
    _stub_compose(monkeypatch,
                  config={'services': {'web': {'image': 'nginx',
                                               'ports': ['8080:80']}}},
                  ps=[{'Publishers': [{'PublishedPort': 8080, 'TargetPort': 80}]}])
    probed = []
    monkeypatch.setattr(ManifestApplyService, '_port_bound',
                        staticmethod(lambda port: probed.append(port) or True))

    result = preflight.preflight_compose_project('/srv/x')

    assert probed == []          # never even asked
    assert result.ok is True


def test_unknown_port_ownership_skips_the_check_rather_than_aborting(app, monkeypatch):
    _stub_compose(monkeypatch,
                  config={'services': {'web': {'image': 'nginx', 'ports': ['8080:80']}}})
    monkeypatch.setattr(DockerService, 'compose_ps', classmethod(
        lambda cls, path, compose_file=None, **kw: (_ for _ in ()).throw(RuntimeError('no docker'))))
    monkeypatch.setattr(ManifestApplyService, '_port_bound',
                        staticmethod(lambda port: True))

    result = preflight.preflight_compose_project('/srv/x')

    assert result.ok is True
    assert 'port_conflicts' in result.skipped


@pytest.mark.parametrize('spec,expected', [
    ('8080:80', 8080),
    ('127.0.0.1:8080:80', 8080),
    ('8080:80/tcp', 8080),
    ('80', None),
    ('8080-8090:80', None),
])
def test_short_port_syntax_parsing(spec, expected):
    assert preflight._published_from_short(spec) == expected


def test_legacy_ports_string_is_understood(app, monkeypatch):
    """Older compose ps output carries `Ports` as a string, not `Publishers`."""
    monkeypatch.setattr(DockerService, 'compose_ps', classmethod(
        lambda cls, path, compose_file=None, **kw: [
            {'Ports': '0.0.0.0:8080->80/tcp, :::8080->80/tcp'},
            {'Ports': '127.0.0.1:5432->5432/tcp'},
        ]))
    assert preflight.published_host_ports('/srv/x') == {8080, 5432}


# ── path 3: GitDeployService._standard_restart (webhook default) ─────────────

def test_standard_restart_never_stops_when_the_preflight_fails(app, monkeypatch, tmp_path):
    from app.services.git_deploy_service import GitDeployService

    calls = _stub_compose(monkeypatch, valid=False)
    application = _app_row(tmp_path, name='git-app')

    result = GitDeployService._standard_restart(application)

    assert result['success'] is False
    assert 'DOWN' not in calls, 'compose_down ran despite a failed preflight'
    assert 'UP' not in calls
    assert result['preflight']['findings'][0]['kind'] == 'compose_invalid'


def test_standard_restart_builds_before_it_stops_then_deploys(app, monkeypatch, tmp_path):
    from app.services.git_deploy_service import GitDeployService

    calls = _stub_compose(monkeypatch,
                          config={'services': {'api': {'build': '.'}}})
    application = _app_row(tmp_path, name='git-app-ok')

    result = GitDeployService._standard_restart(application)

    assert result['success'] is True
    assert calls.index('build') < calls.index('DOWN') < calls.index('UP')


# ── path 2: TemplateService.update_app ───────────────────────────────────────

def _install_template_app(monkeypatch, tmp_path, *, compose_body='services: {web: {image: old}}'):
    from app.services.template_service import TemplateService

    application = _app_row(tmp_path, name='tmpl-app')
    compose_path = tmp_path / 'docker-compose.yml'
    compose_path.write_text(compose_body)
    (tmp_path / '.serverkit-template.json').write_text(
        '{"template_id": "ghost", "template_version": "1.0", "variables": {}}')

    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'installed': {str(application.id): {'template_id': 'ghost'}}}))
    monkeypatch.setattr(TemplateService, 'save_config', classmethod(lambda cls, cfg: None))
    monkeypatch.setattr(TemplateService, 'get_template', classmethod(
        lambda cls, tid: {'success': True,
                          'template': {'name': 'ghost', 'version': '2.0', 'variables': {}}}))
    monkeypatch.setattr(TemplateService, 'generate_compose', classmethod(
        lambda cls, template, variables: 'services: {web: {image: new}}'))
    return application, compose_path


def test_template_update_leaves_the_live_stack_untouched_when_validation_fails(
        app, monkeypatch, tmp_path):
    from app.services.template_service import TemplateService

    calls = _stub_compose(monkeypatch, valid=False)
    application, compose_path = _install_template_app(monkeypatch, tmp_path)
    before = compose_path.read_bytes()

    result = TemplateService.update_app(application.id)

    assert result['success'] is False
    assert 'DOWN' not in calls, 'compose_down ran before the new compose was validated'
    assert compose_path.read_bytes() == before, 'the live compose file was rewritten'
    assert result['preflight']['findings'][0]['kind'] == 'compose_invalid'
    # The candidate file the preflight validated is cleaned up either way.
    assert not os.path.exists(tmp_path / '.serverkit-preflight-compose.yml')


def test_template_update_validates_the_new_compose_before_stopping(app, monkeypatch, tmp_path):
    from app.services.template_service import TemplateService

    calls = _stub_compose(monkeypatch)
    seen = {}

    real_validate = DockerService.validate_compose_file

    def spy_validate(cls, path, compose_file=None):
        # The candidate must exist on disk at validation time and hold the NEW
        # compose, while docker-compose.yml still holds the old one.
        seen['candidate'] = (open(os.path.join(path, compose_file)).read()
                             if compose_file else None)
        seen['live'] = open(os.path.join(path, 'docker-compose.yml')).read()
        return real_validate(path, compose_file)

    monkeypatch.setattr(DockerService, 'validate_compose_file', classmethod(spy_validate))
    application, compose_path = _install_template_app(monkeypatch, tmp_path)

    result = TemplateService.update_app(application.id)

    assert result['success'] is True, result
    assert seen['candidate'] == 'services: {web: {image: new}}'
    assert seen['live'] == 'services: {web: {image: old}}'
    assert calls.index('validate') < calls.index('DOWN') < calls.index('UP')
    assert compose_path.read_text() == 'services: {web: {image: new}}'
    assert not os.path.exists(tmp_path / '.serverkit-preflight-compose.yml')


# ── path 1: DeploymentService._deploy_docker ─────────────────────────────────

def _stub_container_deploy(monkeypatch, *, pull_ok=True):
    from app.services.container_registry_service import ContainerRegistryService
    from app.services.env_service import EnvService

    calls = []
    registry = types.SimpleNamespace(id=1, name='ghcr')

    monkeypatch.setattr(ContainerRegistryService, 'for_app',
                        staticmethod(lambda application: registry))
    monkeypatch.setattr(EnvService, 'get_effective_env', staticmethod(lambda *a, **k: {}))
    monkeypatch.setattr(DockerService, 'pull_image', staticmethod(
        lambda image, tag=None, registry=None: (
            calls.append('pull') or
            ({'success': True} if pull_ok
             else {'success': False, 'error': 'registry unreachable'}))))
    monkeypatch.setattr(DockerService, 'get_container',
                        staticmethod(lambda name: {'Id': 'old'}))
    monkeypatch.setattr(DockerService, 'stop_container', staticmethod(
        lambda name, timeout=10: calls.append('STOP') or {'success': True}))
    monkeypatch.setattr(DockerService, 'remove_container', staticmethod(
        lambda name, force=False, volumes=False: calls.append('REMOVE') or {'success': True}))
    monkeypatch.setattr(DockerService, 'run_container', staticmethod(
        lambda **kw: calls.append('RUN') or {'success': True, 'container_id': 'new'}))
    return calls


def test_deploy_docker_keeps_the_old_container_when_the_image_cannot_be_pulled(
        app, monkeypatch, tmp_path):
    """The pull used to sit BELOW stop+remove: a registry hiccup left the app
    with no container at all."""
    from app.services.deployment_service import DeploymentService

    calls = _stub_container_deploy(monkeypatch, pull_ok=False)
    application = _app_row(tmp_path, name='reg-app', port=9123)
    deployment = types.SimpleNamespace(image_tag='ghcr.io/acme/app:v2')

    result = DeploymentService._deploy_docker(application, deployment)

    assert result['success'] is False
    assert 'registry unreachable' in result['error']
    assert 'STOP' not in calls and 'REMOVE' not in calls, \
        'the live container was destroyed before the image was resolved'
    assert 'RUN' not in calls
    assert result['preflight']['findings'][0]['kind'] == 'image_unresolved'


def test_deploy_docker_pulls_before_it_stops(app, monkeypatch, tmp_path):
    from app.services.deployment_service import DeploymentService

    calls = _stub_container_deploy(monkeypatch, pull_ok=True)
    application = _app_row(tmp_path, name='reg-app-ok', port=9124)
    deployment = types.SimpleNamespace(image_tag='ghcr.io/acme/app:v2')

    result = DeploymentService._deploy_docker(application, deployment)

    assert result['success'] is True
    assert calls == ['pull', 'STOP', 'REMOVE', 'RUN']


def test_an_unreadable_project_is_unknown_not_empty(app, monkeypatch):
    """docker-compose v1 has no `ps --format json`: the listing fails. That
    must skip the port check, not flag the app's own ports as taken."""
    from app.services import deploy_preflight_service as preflight
    monkeypatch.setattr(DockerService, 'run_compose', classmethod(
        lambda cls, cmd, cwd=None, **kw: {'success': False, 'output': '', 'error': 'unknown flag: --format'}))
    assert DockerService.compose_ps('/srv/app') == []
    assert DockerService.compose_ps('/srv/app', strict=True) is None
    assert preflight.published_host_ports('/srv/app') is None
    # v1 exits 0 with its usage text instead of failing outright.
    monkeypatch.setattr(DockerService, 'run_compose', classmethod(
        lambda cls, cmd, cwd=None, **kw: {'success': True, 'output': '', 'returncode': 0,
                                          'stderr': 'List containers.\n\nUsage: ps [options]'}))
    assert DockerService.compose_ps('/srv/app', strict=True) is None
    assert preflight.published_host_ports('/srv/app') is None
    # A project that genuinely has no containers is still an empty set.
    monkeypatch.setattr(DockerService, 'run_compose', classmethod(
        lambda cls, cmd, cwd=None, **kw: {'success': True, 'output': '', 'stderr': '', 'returncode': 0}))
    assert preflight.published_host_ports('/srv/app') == set()
