"""Procfile worker processes for build-pack apps (plan 86 §C3).

Hermetic half: parsing, the plan, the preview compose, and the exact docker
commands a deploy issues. The real-docker half (a worker actually runs with
the app's env, and a removed line takes its container away) is the
docker-builds leg in test_real_worker_processes_docker.py.
"""
import json

import pytest
import yaml

from app import db
from app.services.buildpack_service import BuildpackService
from app.services.docker_service import DockerService
from app.services.worker_process_service import (
    WorkerProcessService, container_name, parse_procfile, worker_processes)
from tests.factories import make_application

PROCFILE = """# processes
web: gunicorn app:app --bind 0.0.0.0:$PORT
worker: celery -A app worker --loglevel=info
release: python manage.py migrate
Scheduler : celery -A app beat
web: ignored-second-definition
"""


def test_parse_keeps_the_first_definition_and_skips_comments():
    assert parse_procfile(PROCFILE) == {
        'web': 'gunicorn app:app --bind 0.0.0.0:$PORT',
        'worker': 'celery -A app worker --loglevel=info',
        'release': 'python manage.py migrate',
        'scheduler': 'celery -A app beat',
    }


def test_web_and_release_are_not_workers():
    assert list(worker_processes(PROCFILE)) == ['worker', 'scheduler']


def test_the_plan_carries_the_processes_and_the_web_command(tmp_path):
    (tmp_path / 'Procfile').write_text(PROCFILE)
    (tmp_path / 'requirements.txt').write_text('flask\n')
    (tmp_path / 'app.py').write_text('')
    plan = BuildpackService.detect(str(tmp_path))
    assert plan['start_command'] == 'gunicorn app:app --bind 0.0.0.0:$PORT'
    assert plan['processes'] == {'worker': 'celery -A app worker --loglevel=info',
                                 'scheduler': 'celery -A app beat'}


def test_preview_compose_runs_workers_from_the_same_image_without_ports(tmp_path):
    plan = {'port': 8000, 'processes': {'worker': 'celery -A app worker'}}
    compose = yaml.safe_load(BuildpackService.generate_compose(plan, 'Shop'))
    web, worker = compose['services']['shop'], compose['services']['shop-worker']
    assert worker['image'] == web['image'] == 'serverkit-shop:latest'
    assert worker['command'] == ['sh', '-c', 'celery -A app worker']
    assert 'ports' not in worker and 'build' not in worker


class TestDeploy:
    @pytest.fixture
    def site(self, app, tmp_path):
        (tmp_path / 'Procfile').write_text(PROCFILE)
        return make_application(db, name='shop', root_path=str(tmp_path))

    def test_replaces_old_workers_and_starts_one_per_process(self, site, fake_subprocess, monkeypatch):
        monkeypatch.setattr('app.services.service_connection_service.ServiceConnectionService'
                            '.needs_shared_network', classmethod(lambda cls, a: False))
        prefix = f'serverkit-app-{site.id}-'
        fake_subprocess.script(['docker', 'ps'], stdout=f'{prefix}old\n')
        fake_subprocess.script(['docker', 'stop'])
        fake_subprocess.script(['docker', 'rm'])
        fake_subprocess.script(['docker', 'run'], stdout='cid\n')

        result = WorkerProcessService.deploy(site, 'img:1', {'A': '1'}, ['vol:/data'])

        assert result == {'started': ['worker', 'scheduler'], 'failed': {}}
        cmds = fake_subprocess.commands()
        assert any(c[:2] == ['docker', 'stop'] and f'{prefix}old' in c for c in cmds)
        assert ['docker', 'rm', f'{prefix}old'] in [c[:3] for c in cmds]
        runs = [c for c in cmds if c[:2] == ['docker', 'run']]
        assert len(runs) == 2
        worker = runs[0]
        assert worker[worker.index('--name') + 1] == container_name(site, 'worker')
        assert worker[-4:] == ['img:1', 'sh', '-c', 'celery -A app worker --loglevel=info']
        assert '-p' not in worker, 'a worker publishes no port'
        assert 'A=1' in worker and 'vol:/data' in worker

    def test_a_failing_worker_is_reported_not_raised(self, site, fake_subprocess, monkeypatch):
        monkeypatch.setattr('app.services.service_connection_service.ServiceConnectionService'
                            '.needs_shared_network', classmethod(lambda cls, a: False))
        fake_subprocess.script(['docker', 'ps'], stdout='')
        fake_subprocess.script(['docker', 'run'], returncode=125, stderr='no such image')
        result = WorkerProcessService.deploy(site, 'img:1', {}, [])
        assert result['started'] == [] and set(result['failed']) == {'worker', 'scheduler'}


def test_build_pack_containers_are_listed_web_first(app, fake_subprocess):
    site = make_application(db, name='shop', buildpack_type='python', root_path='/nonexistent')
    rows = [{'ID': 'w1', 'Names': f'serverkit-app-{site.id}-worker', 'State': 'running'},
            {'ID': 'm1', 'Names': f'serverkit-app-{site.id}', 'State': 'running'}]
    fake_subprocess.script(['docker', 'compose'], stdout='')
    fake_subprocess.script(['docker', 'ps'], stdout='\n'.join(json.dumps(r) for r in rows))
    listed = DockerService.get_all_app_containers(site)
    assert [(c['id'], c['service']) for c in listed] == [('m1', 'web'), ('w1', 'worker')]
