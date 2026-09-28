"""PgBouncer beside a REAL PostgreSQL (plan 86 §D1).

The bundled PostgreSQL template is rendered with the panel's rules; turning
pooling on adds the sidecar through the managed override. A separate project
that only has ``pooledUrl`` from fromService queries through the pooler (its
host is the pooler's name, so an answer proves the path), and turning pooling
off removes the sidecar while PostgreSQL keeps running.

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
from app.services import pooler_service
from app.services.compose_env_service import ComposeEnvService
from app.services.env_service import EnvService
from app.services.template_service import TemplateService
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

SECRET = 'p@ss:w/rd#1%'


def _running(name):
    out = subprocess.run(['docker', 'ps', '--filter', f'name=^{name}$', '--format', '{{.Names}}'],
                         capture_output=True, text=True).stdout
    return name in out.split()


def test_an_app_queries_postgres_through_the_pooler(app, tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    pg_name, client_name = f'skc-pg-{suffix}', f'skc-api-{suffix}'
    installed = {}
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'repos': [], 'installed': installed}))
    template = TemplateService.get_template('postgresql')['template']
    variables = {'APP_NAME': pg_name, 'IMAGE_TAG': '16', 'BIND_ADDRESS': '127.0.0.1',
                 'PORT': '0', 'DB_PASSWORD': SECRET, 'DB_NAME': 'shop'}
    root = tmp_path / pg_name
    root.mkdir()
    compose = yaml.safe_load(TemplateService.generate_compose(template, variables))
    for svc in compose['services'].values():
        svc.pop('ports', None)
    (root / 'docker-compose.yml').write_text(yaml.safe_dump(compose))
    (root / '.serverkit-template.json').write_text(json.dumps(
        {'template_id': 'postgresql', 'variables': variables}))
    pg = make_application(db, name=pg_name, root_path=str(root),
                          compose_file='docker-compose.yml')
    installed[str(pg.id)] = {'template_id': 'postgresql'}

    client_root = tmp_path / client_name
    client_root.mkdir()
    (client_root / 'docker-compose.yml').write_text(yaml.safe_dump({'services': {'api': {
        'image': 'postgres:16-alpine',
        'command': ['sh', '-c', 'for i in $$(seq 30); do '
                    'psql "$$DATABASE_URL" -Atc "select 40 + 2" && exit 0; sleep 2; done; exit 1'],
    }}}))
    client = make_application(db, name=client_name, root_path=str(client_root))
    _v, _c, error = EnvService.set_env_reference(
        client.id, 'DATABASE_URL', {'kind': 'service', 'service': pg_name, 'property': 'pooledUrl'})
    assert error is None

    pg_files = ['-f', str(root / 'docker-compose.yml'),
                '-f', str(root / ComposeEnvService.OVERRIDE_NAME)]
    client_files = ['-f', str(client_root / 'docker-compose.yml'),
                    '-f', str(client_root / ComposeEnvService.OVERRIDE_NAME)]
    try:
        result = pooler_service.set_enabled(pg, True)
        assert result['applied'] is True, result
        assert _running(pooler_service.container_name(pg))
        assert EnvService.get_effective_env(client.id)['DATABASE_URL'].startswith(
            f'postgresql://postgres:')

        ComposeEnvService.refresh_for_project(str(client_root))
        ran = subprocess.run(['docker', 'compose', '-p', client_name, *client_files,
                              'run', '--rm', 'api'], capture_output=True, text=True, timeout=300)
        assert ran.stdout.strip().splitlines()[-1:] == ['42'], ran.stdout + ran.stderr

        off = pooler_service.set_enabled(pg, False)
        assert off['applied'] is True
        assert not _running(pooler_service.container_name(pg))
        assert _running(pg_name), 'turning pooling off must not stop PostgreSQL'
    finally:
        subprocess.run(['docker', 'compose', '-p', client_name, *client_files, 'down', '-v'],
                       capture_output=True, timeout=180)
        subprocess.run(['docker', 'rm', '-f', pooler_service.container_name(pg)],
                       capture_output=True, timeout=60)
        subprocess.run(['docker', 'compose', '-p', pg_name, *pg_files, 'down', '-v'],
                       capture_output=True, timeout=180)
