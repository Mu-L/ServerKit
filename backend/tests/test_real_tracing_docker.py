"""App → OpenTelemetry Collector → Jaeger, through REAL containers (plan 86 §A4).

The bundled collector and Jaeger templates are rendered with the panel's rules.
The collector gets Jaeger attached (traces), an app gets the collector attached
(tracing); the app posts one OTLP/HTTP span to the endpoint it was given, and
Jaeger's API then knows the app's service name.

    SERVERKIT_DOCKER_BUILDS=1 pytest tests -m docker_builds
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request
import uuid

import pytest
import yaml

from app import db
from app.services.app_attachment_service import AppAttachmentService
from app.services.compose_env_service import ComposeEnvService
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

SEND = r'''
import json, os, time, urllib.request
now = time.time_ns()
span = {"resourceSpans": [{"resource": {"attributes": [
    {"key": "service.name", "value": {"stringValue": os.environ["SERVICE"]}}]},
  "scopeSpans": [{"spans": [{"traceId": "5b8efff798038103d269b633813fc60c",
    "spanId": "eee19b7ec3c1b174", "name": "probe", "kind": 1,
    "startTimeUnixNano": str(now - 1000000), "endTimeUnixNano": str(now)}]}]}]}
req = urllib.request.Request(os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] + "/v1/traces",
                             data=json.dumps(span).encode(),
                             headers={"Content-Type": "application/json"})
for _ in range(30):
    try:
        print("SENT", urllib.request.urlopen(req, timeout=5).status, flush=True)
        break
    except OSError:
        time.sleep(2)
'''


def _install(tmp_path, installed, template_id, name, variables):
    template = TemplateService.get_template(template_id)['template']
    variables = dict(variables, APP_NAME=name)
    for var in template.get('variables', []):
        variables.setdefault(var['name'], TemplateService.generate_value(var))
    root = tmp_path / name
    root.mkdir()
    rendered = TemplateService._render_compose_and_files(template, variables, str(root))
    compose = yaml.safe_load(rendered['compose_content'])
    for svc in compose['services'].values():
        svc.pop('ports', None)       # the shared network is the only path in
    (root / 'docker-compose.yml').write_text(yaml.safe_dump(compose))
    for f in rendered['files']:
        with open(f['path'], 'w', newline='\n') as fh:
            fh.write(f['content'])
    (root / '.serverkit-template.json').write_text(json.dumps(
        {'template_id': template_id, 'variables': variables}))
    row = make_application(db, name=name, root_path=str(root))
    installed[str(row.id)] = {'template_id': template_id}
    return row


def _files(row):
    files = ['-f', os.path.join(row.root_path, 'docker-compose.yml')]
    override = os.path.join(row.root_path, ComposeEnvService.OVERRIDE_NAME)
    return files + (['-f', override] if os.path.exists(override) else [])


def test_a_span_reaches_jaeger_through_the_collector(app, tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    installed = {}
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'repos': [], 'installed': installed}))
    jaeger = _install(tmp_path, installed, 'jaeger', f'skc-jaeger-{suffix}', {})
    otel = _install(tmp_path, installed, 'otel-collector', f'skc-otel-{suffix}', {})
    web_root = tmp_path / f'skc-web-{suffix}'
    web_root.mkdir()
    (web_root / 'send.py').write_text(SEND)
    service = f'probe-{suffix}'
    (web_root / 'docker-compose.yml').write_text(yaml.safe_dump({'services': {'app': {
        'image': 'python:3.12-alpine', 'volumes': ['./send.py:/send.py:ro'],
        'environment': {'SERVICE': service}, 'command': ['python', '/send.py']}}}))
    web = make_application(db, name=f'skc-web-{suffix}', root_path=str(web_root))

    AppAttachmentService.attach(otel, jaeger, 'traces')
    AppAttachmentService.attach(web, otel, 'tracing')
    rows = [jaeger, otel, web]
    try:
        for row in (jaeger, otel):
            ComposeEnvService.refresh_for_project(row.root_path)
            subprocess.run(['docker', 'compose', '-p', row.name, *_files(row), 'up', '-d'],
                           check=True, capture_output=True, timeout=300)
        ComposeEnvService.refresh_for_project(web.root_path)
        sent = subprocess.run(['docker', 'compose', '-p', web.name, *_files(web), 'run', '--rm', 'app'],
                              capture_output=True, text=True, timeout=300)
        assert 'SENT 200' in sent.stdout, sent.stdout + sent.stderr

        # Jaeger's query API, from inside the network (its UI port is not published).
        probe = ('import json,urllib.request,time\n'
                 'for _ in range(45):\n'
                 '    try:\n'
                 f'        d=json.load(urllib.request.urlopen("http://{jaeger.name}:16686/api/services"))\n'
                 f'        if "{service}" in (d.get("data") or []): print("FOUND"); break\n'
                 '    except OSError: pass\n'
                 '    time.sleep(2)\n')
        found = subprocess.run(['docker', 'run', '--rm', '--network', 'serverkit-services',
                                'python:3.12-alpine', 'python', '-c', probe],
                               capture_output=True, text=True, timeout=300)
        if 'FOUND' not in found.stdout:
            logs = {name: subprocess.run(['docker', 'logs', '--tail', '40', name],
                                         capture_output=True, text=True).stderr[-3000:]
                    for name in (otel.name, jaeger.name)}
            pytest.fail(f'span never reached jaeger\n{found.stdout}{found.stderr}\n{logs}')
    finally:
        for row in reversed(rows):
            subprocess.run(['docker', 'compose', '-p', row.name, *_files(row), 'down', '-v'],
                           capture_output=True, timeout=180)
