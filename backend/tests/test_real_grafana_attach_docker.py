"""Attach Prometheus to a REAL Grafana (plan 86 §A4).

Both bundled templates are rendered with the panel's rules and started with
their managed overrides. Attaching creates the data source and the bundled
dashboard through Grafana's API; after the redeploy that puts Grafana on the
shared network, Grafana's own health check on that data source passes. A
detach removes the data source again.

    SERVERKIT_DOCKER_BUILDS=1 pytest tests -m docker_builds
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
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


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _install(tmp_path, installed, template_id, name, variables, drop_ports=False):
    template = TemplateService.get_template(template_id)['template']
    variables = dict(variables, APP_NAME=name)
    for var in template.get('variables', []):
        variables.setdefault(var['name'], TemplateService.generate_value(var))
    variables.update(TemplateService.collect_magic_variables(template))
    root = tmp_path / name
    root.mkdir()
    rendered = TemplateService._render_compose_and_files(template, variables, str(root))
    compose = yaml.safe_load(rendered['compose_content'])
    if drop_ports:
        for svc in compose['services'].values():
            svc.pop('ports', None)
    (root / 'docker-compose.yml').write_text(yaml.safe_dump(compose))
    for f in rendered['files']:
        with open(f['path'], 'w', newline='\n') as fh:
            fh.write(f['content'])
    (root / '.serverkit-template.json').write_text(json.dumps(
        {'template_id': template_id, 'variables': variables}))
    row = make_application(db, name=name, root_path=str(root))
    installed[str(row.id)] = {'template_id': template_id}
    return row, variables


def _up(row):
    ComposeEnvService.refresh_for_project(row.root_path)
    files = ['-f', os.path.join(row.root_path, 'docker-compose.yml')]
    override = os.path.join(row.root_path, ComposeEnvService.OVERRIDE_NAME)
    if os.path.exists(override):
        files += ['-f', override]
    subprocess.run(['docker', 'compose', '-p', row.name, *files, 'up', '-d'],
                   check=True, capture_output=True, timeout=300)
    return files


def _grafana(port, user, password, path):
    token = base64.b64encode(f'{user}:{password}'.encode()).decode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                                 headers={'Authorization': f'Basic {token}'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def test_grafana_gets_a_working_prometheus_data_source(app, tmp_path, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    installed = {}
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'repos': [], 'installed': installed}))
    port = _free_port()
    prom, _ = _install(tmp_path, installed, 'prometheus', f'skc-prom-{suffix}',
                       {'HTTP_PORT': '0', 'PANEL_TARGET': 'localhost:9090'}, drop_ports=True)
    graf, gvars = _install(tmp_path, installed, 'grafana', f'skc-graf-{suffix}',
                           {'HTTP_PORT': str(port), 'ADMIN_USER': 'admin',
                            'ADMIN_PASSWORD': 'graf-pw-123'})
    projects = []
    try:
        projects.append((prom, _up(prom)))
        projects.append((graf, _up(graf)))
        deadline = time.time() + 120
        while True:
            try:
                _grafana(port, 'admin', 'graf-pw-123', '/api/health')
                break
            except (OSError, urllib.error.URLError):
                assert time.time() < deadline, 'grafana never came up'
                time.sleep(2)

        row, created = AppAttachmentService.attach(graf, prom, 'metrics')
        assert created
        uid = row.details['datasource_uid']
        dash = _grafana(port, 'admin', 'graf-pw-123', '/api/dashboards/uid/serverkit-overview')
        assert dash['dashboard']['title'] == 'ServerKit servers'

        # The attachment puts Grafana on the shared network at its redeploy.
        projects[-1] = (graf, _up(graf))
        deadline = time.time() + 120
        while True:
            try:
                health = _grafana(port, 'admin', 'graf-pw-123', f'/api/datasources/uid/{uid}/health')
                if health.get('status') == 'OK':
                    break
            except (OSError, urllib.error.URLError):
                health = None
            assert time.time() < deadline, f'data source never healthy: {health}'
            time.sleep(2)

        assert AppAttachmentService.detach(row) is None
        with pytest.raises(urllib.error.HTTPError):
            _grafana(port, 'admin', 'graf-pw-123', f'/api/datasources/uid/{uid}')
    finally:
        for app_row, files in reversed(projects):
            subprocess.run(['docker', 'compose', '-p', app_row.name, *files, 'down', '-v'],
                           capture_output=True, timeout=180)
