"""The Prometheus template scrapes the panel with the panel's token (plan 86 §A4).

The bundled template is rendered with the install pipeline's variable and
magic-token rules, so ``${SERVERKIT_METRICS_TOKEN}`` becomes the panel's own
stored token. Real Prometheus runs it against a stand-in that answers the
metrics path only when that token arrives; Prometheus's own targets API must
report the ``serverkit`` job up. (The panel endpoint accepting the stored token
is proven hermetically in test_metrics_token.py.)

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

from app.services import metrics_token_service
from app.services.template_service import TemplateService


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

STAND_IN = r'''
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        ok = (u.path == "/api/v1/fleet-monitor/prometheus"
              and parse_qs(u.query).get("token") == [os.environ["TOKEN"]])
        self.send_response(200 if ok else 401)
        self.end_headers()
        if ok:
            self.wfile.write(b"serverkit_up 1\n")
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
'''


def test_prometheus_scrapes_the_panel_with_its_token(app, tmp_path):
    suffix = uuid.uuid4().hex[:8]
    net, panel, prom = f'skc-net-{suffix}', f'skc-panel-{suffix}', f'skc-prom-{suffix}'
    token = metrics_token_service.get_or_create()
    assert metrics_token_service.get_or_create() == token, 'the token is stable'

    template = TemplateService.get_template('prometheus')['template']
    variables = {'APP_NAME': prom, 'HTTP_PORT': '0', 'RETENTION_TIME': '1d',
                 'PANEL_TARGET': f'{panel}:8080', 'PANEL_SCHEME': 'http'}
    variables.update(TemplateService.collect_magic_variables(template))
    assert variables['SERVERKIT_METRICS_TOKEN'] == token
    rendered = TemplateService._render_compose_and_files(template, variables, str(tmp_path))
    assert rendered['success'], rendered
    (config,) = [f for f in rendered['files'] if f['path'].endswith('prometheus.yml')]
    parsed = yaml.safe_load(config['content'])
    job = next(j for j in parsed['scrape_configs'] if j['job_name'] == 'serverkit')
    assert job['params'] == {'token': [token]}
    with open(tmp_path / 'prometheus.yml', 'w', newline='\n') as fh:
        fh.write(config['content'])
    (tmp_path / 'stand_in.py').write_text(STAND_IN)

    try:
        subprocess.run(['docker', 'network', 'create', net], check=True, capture_output=True)
        subprocess.run(['docker', 'run', '-d', '--name', panel, '--network', net,
                        '-e', f'TOKEN={token}',
                        '-v', f'{tmp_path / "stand_in.py"}:/s.py:ro',
                        'python:3.12-alpine', 'python', '/s.py'],
                       check=True, capture_output=True, timeout=300)
        subprocess.run(['docker', 'run', '-d', '--name', prom, '--network', net,
                        '-p', '127.0.0.1::9090',
                        '-v', f'{tmp_path / "prometheus.yml"}:/etc/prometheus/prometheus.yml:ro',
                        'prom/prometheus:v2.48.0',
                        '--config.file=/etc/prometheus/prometheus.yml',
                        '--storage.tsdb.path=/prometheus'],
                       check=True, capture_output=True, timeout=300)
        port = subprocess.run(['docker', 'port', prom, '9090'], capture_output=True,
                              text=True).stdout.strip().split(':')[-1]

        deadline = time.time() + 90
        while True:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/targets', timeout=5) as r:
                    targets = json.load(r)['data']['activeTargets']
                health = {t['labels']['job']: t['health'] for t in targets}
                if health.get('serverkit') == 'up':
                    break
            except OSError:
                health = {}
            assert time.time() < deadline, f'serverkit target never came up: {health}'
            time.sleep(2)
    finally:
        subprocess.run(['docker', 'rm', '-f', prom, panel], capture_output=True, timeout=60)
        subprocess.run(['docker', 'network', 'rm', net], capture_output=True, timeout=60)
