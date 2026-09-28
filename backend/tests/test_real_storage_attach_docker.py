"""Attach storage against a REAL Garage (plan 86 §C2).

The bundled Garage template is rendered with the panel's own variable and
file rules and started with its managed override. Attaching gives an app a
bucket and a scoped key; a separate project that has only the env the
attach wrote can write and read its own bucket, is refused on another
bucket, and after a detach the same key is refused everywhere.

Gated with the other docker legs (pulls images):

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
from app.services.app_attachment_service import AppAttachmentService
from app.services.compose_env_service import ComposeEnvService
from app.services.env_service import EnvService
from app.services.garage_admin import GarageAdmin, GarageError
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

PROBE = r'''
import os, sys, boto3, botocore
cfg = dict(endpoint_url=os.environ["S3_ENDPOINT"], region_name=os.environ["S3_REGION"],
           aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
           aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"])
s3 = boto3.client("s3", **cfg)
def attempt(fn):
    try:
        fn(); return "ok"
    except botocore.exceptions.ClientError as e:
        return e.response["Error"]["Code"]
own = os.environ["S3_BUCKET"]
print("own-put", attempt(lambda: s3.put_object(Bucket=own, Key="k", Body=b"v")))
print("own-get", attempt(lambda: s3.get_object(Bucket=own, Key="k")["Body"].read()))
print("other-put", attempt(lambda: s3.put_object(Bucket=sys.argv[1], Key="k", Body=b"v")))
'''


def _compose(project, root, *files, args, check=True, timeout=300):
    argv = ['docker', 'compose', '-p', project]
    for name in files:
        argv += ['-f', os.path.join(root, name)]
    proc = subprocess.run(argv + args, capture_output=True, text=True,
                          timeout=timeout, cwd=root)
    if check:
        assert proc.returncode == 0, f'{argv + args}\n{proc.stdout}\n{proc.stderr}'
    return proc


def _results(stdout):
    return dict(line.split(' ', 1) for line in stdout.splitlines()
                if line.split(' ', 1)[0] in ('own-put', 'own-get', 'other-put'))


def test_attach_storage_scopes_a_key_to_one_bucket(app, tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    store_name, web_name = f'skc-store-{suffix}', f'skc-web-{suffix}'
    installed = {}
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'repos': [], 'installed': installed}))

    template = TemplateService.get_template('garage')['template']
    variables = {'APP_NAME': store_name, 'API_PORT': '0', 'BIND_ADDRESS': '127.0.0.1'}
    for var in template['variables']:
        if var['name'] not in variables:
            variables[var['name']] = TemplateService.generate_value(var)
    store_root = tmp_path / store_name
    store_root.mkdir()
    rendered = TemplateService._render_compose_and_files(template, variables, str(store_root))
    assert rendered['success'], rendered
    compose = yaml.safe_load(rendered['compose_content'])
    for svc in compose['services'].values():
        svc.pop('ports', None)       # reachability comes from the network alone
    (store_root / 'docker-compose.yml').write_text(yaml.safe_dump(compose))
    for f in rendered['files']:
        with open(f['path'], 'w', newline='\n') as fh:
            fh.write(f['content'])
    (store_root / '.serverkit-template.json').write_text(json.dumps(
        {'template_id': 'garage', 'variables': variables}))
    store = make_application(db, name=store_name, root_path=str(store_root))
    installed[str(store.id)] = {'template_id': 'garage'}

    web_root = tmp_path / web_name
    web_root.mkdir()
    (web_root / 'probe.py').write_text(PROBE)
    web = make_application(db, name=web_name, root_path=str(web_root))

    base, override = 'docker-compose.yml', ComposeEnvService.OVERRIDE_NAME
    other_bucket = f'other-{suffix}'
    try:
        assert ComposeEnvService.refresh_for_project(str(store_root))
        _compose(store_name, str(store_root), base, override, args=['up', '-d'])
        admin = GarageAdmin(store_name)
        deadline = time.time() + 60
        while True:
            try:
                admin.call('GetClusterStatus')
                break
            except GarageError:
                assert time.time() < deadline, 'garage never answered'
                time.sleep(1)

        row, created = AppAttachmentService.attach_storage(web, store)
        assert created
        again, created_again = AppAttachmentService.attach_storage(web, store)
        assert again.id == row.id and not created_again
        admin.ensure_bucket(other_bucket)

        (web_root / 'docker-compose.yml').write_text(yaml.safe_dump({'services': {'probe': {
            'image': 'python:3.12-alpine',
            'volumes': ['./probe.py:/probe.py:ro'],
            'command': ['sh', '-c', 'pip install -q --disable-pip-version-check boto3 '
                        f'>/dev/null 2>&1 && python /probe.py {other_bucket}'],
        }}}))
        assert ComposeEnvService.refresh_for_project(str(web_root))
        first = _compose(web_name, str(web_root), base, override, args=['run', '--rm', 'probe'])
        assert _results(first.stdout) == {
            'own-put': 'ok', 'own-get': 'ok', 'other-put': 'AccessDenied'}, first.stdout

        # Detach revokes the key: replay the same credentials explicitly.
        creds = EnvService.get_effective_env(web.id)
        assert AppAttachmentService.detach(row) is None
        assert 'S3_BUCKET' not in EnvService.get_effective_env(web.id)
        replay = ['run', '--rm']
        for key in ('S3_ENDPOINT', 'S3_REGION', 'S3_ACCESS_KEY_ID',
                    'S3_SECRET_ACCESS_KEY', 'S3_BUCKET'):
            replay += ['-e', f'{key}={creds[key]}']
        # Keep the network so only the revoked key can explain a refusal.
        (web_root / 'net.yml').write_text(yaml.safe_dump({
            'services': {'probe': {'networks': ['default', 'serverkit-services']}},
            'networks': {'serverkit-services': {'external': True}}}))
        after = _compose(web_name, str(web_root), base, 'net.yml', args=replay + ['probe'])
        assert _results(after.stdout)['own-put'] in ('InvalidAccessKeyId', 'AccessDenied'), after.stdout
    finally:
        _compose(web_name, str(web_root), base, check=False,
                 args=['down', '-v', '--remove-orphans'])
        _compose(store_name, str(store_root), base, override, check=False,
                 args=['down', '-v', '--remove-orphans'])
