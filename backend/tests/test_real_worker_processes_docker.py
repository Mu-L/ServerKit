"""Procfile workers through the REAL docker daemon (plan 86 §C3 + §C1).

A build-pack app's worker runs from the app's image with the app's env, as a
plain `docker run` container, so it only reaches an attached Redis if the
deploy joined it to the shared service network (compose apps get that from
their override; single-container apps from connect_shared_network). The
worker pings Redis with the URL fromService resolved; a Procfile line removed
on the next deploy takes its container with it.

    SERVERKIT_DOCKER_BUILDS=1 pytest tests -m docker_builds
"""
import json
import os
import shutil
import subprocess
import time
import uuid

import pytest
import yaml

from app import db
from app.services.compose_env_service import ComposeEnvService
from app.services.env_service import EnvService
from app.services.template_service import TemplateService
from app.services.worker_process_service import WorkerProcessService, container_name
from tests.factories import make_application


def _docker_ready() -> bool:
    if shutil.which('docker') is None:
        return False
    try:
        return subprocess.run(['docker', 'info'], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.docker_builds,
    pytest.mark.skipif(os.environ.get('SERVERKIT_DOCKER_BUILDS') != '1',
                       reason='docker-builds leg; opt in with SERVERKIT_DOCKER_BUILDS=1'),
    pytest.mark.skipif(not _docker_ready(), reason='docker daemon not reachable'),
]

SECRET = 'wrk-s3cret'
WORKER = ('pip install -q --disable-pip-version-check redis >/dev/null 2>&1 && '
          'python -c "import os,redis; r=redis.Redis.from_url(os.environ[\'REDIS_URL\'], '
          'socket_connect_timeout=5); print(\'PONG\' if r.ping() else \'no\', flush=True)" '
          '&& sleep 3600')


def _logs(name):
    return subprocess.run(['docker', 'logs', name], capture_output=True, text=True,
                          timeout=30).stdout


def test_a_worker_reaches_redis_and_goes_away_with_its_line(app, tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    engine_name = f'skc-cache-{suffix}'
    installed = {}
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'repos': [], 'installed': installed}))

    template = TemplateService.get_template('redis')['template']
    variables = {'APP_NAME': engine_name, 'IMAGE_TAG': '7.2', 'BIND_ADDRESS': '127.0.0.1',
                 'PORT': '0', 'DB_PASSWORD': SECRET}
    engine_root = tmp_path / engine_name
    engine_root.mkdir()
    rendered = yaml.safe_load(TemplateService.generate_compose(template, variables))
    for svc in rendered['services'].values():
        svc.pop('ports', None)
    (engine_root / 'docker-compose.yml').write_text(yaml.safe_dump(rendered))
    (engine_root / '.serverkit-template.json').write_text(json.dumps(
        {'template_id': 'redis', 'variables': variables}))
    engine = make_application(db, name=engine_name, root_path=str(engine_root))
    installed[str(engine.id)] = {'template_id': 'redis'}

    web_root = tmp_path / f'web-{suffix}'
    web_root.mkdir()
    (web_root / 'Procfile').write_text(f'web: sleep 3600\nworker: {WORKER}\n')
    web = make_application(db, name=f'web-{suffix}', buildpack_type='python',
                           root_path=str(web_root))
    _v, _c, error = EnvService.set_env_reference(
        web.id, 'REDIS_URL', {'kind': 'service', 'service': engine_name, 'property': 'url'})
    assert error is None
    worker = container_name(web, 'worker')

    compose = ['docker', 'compose', '-p', engine_name,
               '-f', str(engine_root / 'docker-compose.yml'),
               '-f', str(engine_root / ComposeEnvService.OVERRIDE_NAME)]
    try:
        assert ComposeEnvService.refresh_for_project(str(engine_root))
        subprocess.run(compose + ['up', '-d'], check=True, capture_output=True, timeout=180)

        result = WorkerProcessService.deploy(
            web, 'python:3.12-alpine', EnvService.get_effective_env(web.id), [])
        assert result == {'started': ['worker'], 'failed': {}}

        deadline = time.time() + 120
        while 'PONG' not in _logs(worker):
            assert time.time() < deadline, f'worker never reached redis:\n{_logs(worker)}'
            time.sleep(2)

        (web_root / 'Procfile').write_text('web: sleep 3600\n')
        assert WorkerProcessService.deploy(web, 'python:3.12-alpine', {}, []) == {
            'started': [], 'failed': {}}
        assert WorkerProcessService.existing(web) == []
    finally:
        subprocess.run(['docker', 'rm', '-f', worker], capture_output=True, timeout=60)
        subprocess.run(compose + ['down', '-v'], capture_output=True, timeout=180)
