"""fromService → a real Redis, through the REAL docker daemon (plan 86 §C1).

test_service_connections.py proves the resolved URL parses back to the right
host, port and secret, and that both overrides name the shared network. This
leg proves the thing those assertions stand for: the bundled Redis template,
rendered with the panel's variable rules and started with its managed
override, answers `PING` from a separate compose project that only has the
URL `fromService` resolved. A negative control without the override shows
the shared network, not luck, is what makes the host reachable.

Gated with the other docker legs (pulls an image):

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

# URL-hostile, but no `$` (compose would interpolate it in the command line).
SECRET = 'p@ss:w/rd#1%'


def _compose(project, root, *files, args, check=True, timeout=180):
    argv = ['docker', 'compose', '-p', project]
    for name in files:
        argv += ['-f', os.path.join(root, name)]
    proc = subprocess.run(argv + args, capture_output=True, text=True,
                          timeout=timeout, cwd=root)
    if check:
        assert proc.returncode == 0, f'{argv + args}\n{proc.stdout}\n{proc.stderr}'
    return proc


def test_an_app_reaches_an_installed_redis_with_the_resolved_url(app, tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    engine_name, client_name = f'skc-cache-{suffix}', f'skc-web-{suffix}'
    installed = {}
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'repos': [], 'installed': installed}))

    template = TemplateService.get_template('redis')['template']
    variables = {'APP_NAME': engine_name, 'IMAGE_TAG': '7.2', 'BIND_ADDRESS': '127.0.0.1',
                 'PORT': '0', 'DB_PASSWORD': SECRET}
    engine_root = tmp_path / engine_name
    engine_root.mkdir()
    # PORT 0 would be an invalid host port; publish nothing — reachability must
    # come from the network alone.
    rendered = yaml.safe_load(TemplateService.generate_compose(template, variables))
    for svc in rendered['services'].values():
        svc.pop('ports', None)
    (engine_root / 'docker-compose.yml').write_text(yaml.safe_dump(rendered))
    (engine_root / '.serverkit-template.json').write_text(json.dumps(
        {'template_id': 'redis', 'variables': variables}))
    engine = make_application(db, name=engine_name, root_path=str(engine_root))
    installed[str(engine.id)] = {'template_id': 'redis'}

    client_root = tmp_path / client_name
    client_root.mkdir()
    # A real app client, not redis-cli: redis-cli 7.2 does not percent-decode
    # URL passwords, while the client libraries apps use (redis-py, ioredis,
    # node-redis) do, per RFC 3986 — and an unencoded `@`/`:`/`/` in the
    # password breaks every parser.
    probe = ("import os, redis; "
             "print('PONG' if redis.Redis.from_url(os.environ['REDIS_URL'], "
             "socket_connect_timeout=5).ping() else 'no')")
    (client_root / 'docker-compose.yml').write_text(yaml.safe_dump({'services': {
        'client': {'image': 'python:3.12-alpine',
                   'command': ['sh', '-c',
                               f'pip install -q --disable-pip-version-check redis >/dev/null 2>&1 '
                               f'&& python -c "{probe}"']},
    }}))
    client = make_application(db, name=client_name, root_path=str(client_root))
    _var, _created, error = EnvService.set_env_reference(
        client.id, 'REDIS_URL', {'kind': 'service', 'service': engine_name, 'property': 'url'})
    assert error is None, error

    base, override = 'docker-compose.yml', ComposeEnvService.OVERRIDE_NAME
    try:
        assert ComposeEnvService.refresh_for_project(str(engine_root))
        assert ComposeEnvService.refresh_for_project(str(client_root))
        _compose(engine_name, str(engine_root), base, override, args=['up', '-d'])

        deadline = time.time() + 60
        while True:
            probe = subprocess.run(['docker', 'exec', engine_name, 'redis-cli', '-a', SECRET,
                                    '--no-auth-warning', 'ping'],
                                   capture_output=True, text=True, timeout=20)
            if probe.stdout.strip() == 'PONG':
                break
            assert time.time() < deadline, f'redis never came up: {probe.stderr}'
            time.sleep(1)

        answered = _compose(client_name, str(client_root), base, override,
                            args=['run', '--rm', 'client'])
        assert answered.stdout.strip().endswith('PONG'), answered.stdout + answered.stderr

        # Negative control: same client, same URL, no managed override → no
        # shared network and no env, so the engine's name does not resolve.
        isolated = _compose(client_name, str(client_root), base, check=False,
                            args=['run', '--rm', '-e',
                                  f"REDIS_URL={EnvService.get_effective_env(client.id)['REDIS_URL']}",
                                  'client'])
        assert 'PONG' not in isolated.stdout, isolated.stdout
    finally:
        _compose(client_name, str(client_root), base, override, check=False,
                 args=['down', '-v', '--remove-orphans'])
        _compose(engine_name, str(engine_root), base, override, check=False,
                 args=['down', '-v', '--remove-orphans'])


def test_an_app_reaches_an_installed_rabbitmq_with_the_resolved_url(app, tmp_path, monkeypatch):
    """Same round trip for the broker (plan 86 §C3): AMQP login with the URL."""
    suffix = uuid.uuid4().hex[:8]
    engine_name, client_name = f'skc-mq-{suffix}', f'skc-jobs-{suffix}'
    installed = {}
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'repos': [], 'installed': installed}))

    template = TemplateService.get_template('rabbitmq')['template']
    variables = {'APP_NAME': engine_name, 'BIND_ADDRESS': '127.0.0.1', 'PORT': '0',
                 'RABBITMQ_USER': 'svc', 'RABBITMQ_PASSWORD': SECRET}
    engine_root = tmp_path / engine_name
    engine_root.mkdir()
    rendered = yaml.safe_load(TemplateService.generate_compose(template, variables))
    for svc in rendered['services'].values():
        svc.pop('ports', None)
    (engine_root / 'docker-compose.yml').write_text(yaml.safe_dump(rendered))
    (engine_root / '.serverkit-template.json').write_text(json.dumps(
        {'template_id': 'rabbitmq', 'variables': variables}))
    engine = make_application(db, name=engine_name, root_path=str(engine_root))
    installed[str(engine.id)] = {'template_id': 'rabbitmq'}

    client_root = tmp_path / client_name
    client_root.mkdir()
    probe = ("import os, time, pika\n"
             "for _ in range(60):\n"
             "    try:\n"
             "        c = pika.BlockingConnection(pika.URLParameters(os.environ['AMQP_URL']))\n"
             "        c.channel().queue_declare('probe'); print('CONNECTED'); break\n"
             "    except pika.exceptions.AMQPConnectionError:\n"
             "        time.sleep(2)\n")
    (client_root / 'probe.py').write_text(probe)
    (client_root / 'docker-compose.yml').write_text(yaml.safe_dump({'services': {
        'client': {'image': 'python:3.12-alpine', 'volumes': ['./probe.py:/probe.py:ro'],
                   'command': ['sh', '-c', 'pip install -q --disable-pip-version-check pika '
                               '>/dev/null 2>&1 && python /probe.py']},
    }}))
    client = make_application(db, name=client_name, root_path=str(client_root))
    _var, _created, error = EnvService.set_env_reference(
        client.id, 'AMQP_URL', {'kind': 'service', 'service': engine_name, 'property': 'url'})
    assert error is None, error

    base, override = 'docker-compose.yml', ComposeEnvService.OVERRIDE_NAME
    try:
        assert ComposeEnvService.refresh_for_project(str(engine_root))
        assert ComposeEnvService.refresh_for_project(str(client_root))
        _compose(engine_name, str(engine_root), base, override, args=['up', '-d'])
        answered = _compose(client_name, str(client_root), base, override,
                            args=['run', '--rm', 'client'], timeout=300)
        assert 'CONNECTED' in answered.stdout, answered.stdout + answered.stderr
    finally:
        _compose(client_name, str(client_root), base, override, check=False,
                 args=['down', '-v', '--remove-orphans'])
        _compose(engine_name, str(engine_root), base, override, check=False,
                 args=['down', '-v', '--remove-orphans'])
