"""Slot deploys through the REAL docker daemon (plan 87, the real Docker leg).

The fake-world tests prove the ordering; this proves the property against real
containers and a real nginx: two deploys of a tiny HTTP image while a probe
loop hammers the site through nginx, and the loop must record ZERO non-2xx
answers — through the switch, and through the switch back.

nginx runs in a container on a private network and proxies to the live slot
by container name; the switch is still what production does — rewrite the
vhost, `nginx -t`, graceful reload — only the upstream address differs (a
containerised nginx cannot reach the host's loopback ports).

The compose half brings a rendered slot project up for real and checks the
claim the whole compose design rests on: the slot mounts the SAME named
volume as the original project, not an empty project-prefixed one.

    SERVERKIT_DOCKER_BUILDS=1 pytest tests -m docker_builds
"""
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest
import yaml

from factories import make_application

from app import db
from app.models.deployment import Deployment
from app.models.domain import Domain


def _docker_ready() -> bool:
    if shutil.which('docker') is None:
        return False
    try:
        return subprocess.run(['docker', 'info'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.docker_builds,
    pytest.mark.skipif(os.environ.get('SERVERKIT_DOCKER_BUILDS') != '1',
                       reason='docker-builds leg; opt in with SERVERKIT_DOCKER_BUILDS=1'),
    pytest.mark.skipif(not _docker_ready(), reason='docker daemon not reachable'),
]


def _docker(*args, check=True, timeout=300):
    result = subprocess.run(['docker', *args], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _build_app_image(tmp, tag, port, body):
    """nginx:alpine serving `body` on `port` — the tiny HTTP app."""
    ctx = tmp / tag.replace(':', '_')
    ctx.mkdir()
    (ctx / 'index.html').write_text(body)
    (ctx / 'default.conf').write_text(
        f'server {{ listen {port}; location / {{ root /usr/share/nginx/html; }} }}\n')
    (ctx / 'Dockerfile').write_text(
        'FROM nginx:alpine\nCOPY default.conf /etc/nginx/conf.d/default.conf\n'
        'COPY index.html /usr/share/nginx/html/index.html\n')
    _docker('build', '-t', tag, str(ctx), timeout=600)


class Probe(threading.Thread):
    """GET the site through nginx every few ms; remember every answer."""

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url = url
        self.answers = []
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=3) as resp:
                    self.answers.append((resp.status, resp.read().decode().strip()))
            except urllib.error.HTTPError as exc:
                self.answers.append((exc.code, ''))
            except Exception as exc:  # noqa: BLE001 - a refused connection is a failure too
                self.answers.append((None, str(exc)))
            time.sleep(0.02)

    def bad(self):
        return [a for a in self.answers if a[0] is None or not 200 <= a[0] < 300]


@pytest.fixture
def real_slots(app, monkeypatch, tmp_path):
    from app.services import slot_deploy_service
    from app.services.site_domain_service import SiteDomainService

    run = uuid.uuid4().hex[:8]
    net = f'sk-slot-net-{run}'
    proxy = f'sk-slot-proxy-{run}'
    conf_dir = tmp_path / 'conf.d'
    conf_dir.mkdir()
    port = _free_port()
    proxy_port = _free_port()
    images = [f'sk-slot-e2e-{run}:v1', f'sk-slot-e2e-{run}:v2']
    for version, tag in enumerate(images, start=1):
        _build_app_image(tmp_path, tag, port, f'v{version}')

    _docker('network', 'create', net)
    row = make_application(db, name=f'e2e{run}', app_type='docker', status='running',
                           root_path=None, compose_file=None, docker_image=images[0], port=port)
    db.session.add(Domain(name='e2e.example.test', application_id=row.id, is_primary=True))
    db.session.add(Deployment(app_id=row.id, version=1, status='live', image_tag=images[0],
                              deploy_completed_at=db.func.now()))
    db.session.commit()
    # The app as an in-place deploy leaves it (port:port), on the proxy's network.
    legacy = f'serverkit-app-{row.id}'
    _docker('run', '-d', '--name', legacy, '--network', net, '-p', f'127.0.0.1:{port}:{port}',
            images[0])

    def write_vhost(app, force_type=None):
        live = slot_deploy_service.live_container_name(app)
        _docker('network', 'connect', net, live, check=False)
        (conf_dir / 'site.conf').write_text(
            f'server {{ listen 80; server_name e2e.example.test; '
            f'location / {{ proxy_pass http://{live}:{port}; }} }}\n')
        tested = _docker('exec', proxy, 'nginx', '-t', check=False)
        if tested.returncode != 0:
            return {'nginx': {'success': False}, 'warning': tested.stderr}
        _docker('exec', proxy, 'nginx', '-s', 'reload')
        return {'nginx': {'success': True}, 'warning': None}

    (conf_dir / 'site.conf').write_text(
        f'server {{ listen 80; location / {{ proxy_pass http://{legacy}:{port}; }} }}\n')
    _docker('run', '-d', '--name', proxy, '--network', net, '-p', f'127.0.0.1:{proxy_port}:80',
            '-v', f'{conf_dir}:/etc/nginx/conf.d:ro', 'nginx:alpine')
    monkeypatch.setattr(SiteDomainService, 'write_app_vhost', classmethod(
        lambda cls, app, force_type=None: write_vhost(app)))
    monkeypatch.setattr(slot_deploy_service, 'NGINX_PROBE_URL', f'http://127.0.0.1:{proxy_port}/')
    from app.plugins_sdk import notify
    monkeypatch.setattr(notify, 'send', lambda *a, **k: None)
    from app.services import deploy_settings
    deploy_settings.update(row, {'watch_seconds': 3, 'healthcheck_timeout': 60})
    db.session.commit()

    # nginx answering before the probe starts
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{proxy_port}/', timeout=2)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)

    yield row, images, proxy_port
    for name in (proxy, legacy, f'serverkit-slot-{row.id}-a', f'serverkit-slot-{row.id}-b'):
        _docker('rm', '-f', name, check=False)
    _docker('network', 'rm', net, check=False)
    _docker('rmi', '-f', *images, check=False)


def test_two_slot_deploys_and_a_switch_back_never_drop_a_request(real_slots):
    from app.services.slot_deploy_service import SlotDeployService
    row, images, proxy_port = real_slots
    assert SlotDeployService.set_enabled(row, True)['success']

    probe = Probe(f'http://127.0.0.1:{proxy_port}/')
    probe.start()
    try:
        time.sleep(0.5)
        v2 = Deployment(app_id=row.id, version=2, status='deploying', image_tag=images[1])
        db.session.add(v2)
        db.session.commit()
        result = SlotDeployService.deploy_container(row, v2, images[1], {}, [])
        assert result['success'], result
        assert row.active_slot == 'b'
        time.sleep(0.5)

        # Switch back to the warm standby: a start-and-switch, no new container.
        v3 = Deployment(app_id=row.id, version=3, status='deploying', image_tag=images[0],
                        deploy_trigger='rollback')
        db.session.add(v3)
        db.session.commit()
        back = SlotDeployService.deploy_container(row, v3, images[0], {}, [])
        assert back['success'], back
        assert row.active_slot == 'a'
        time.sleep(0.5)
    finally:
        probe.stop.set()
        probe.join(5)

    bodies = [body for status, body in probe.answers if status == 200]
    assert probe.bad() == [], f'{len(probe.bad())} failed requests during the switch: {probe.bad()[:5]}'
    assert 'v1' in bodies and 'v2' in bodies, 'the probe never saw the switch happen'
    assert bodies[-1] == 'v1'
    assert len(probe.answers) > 50


def test_a_compose_slot_mounts_the_original_projects_volume(tmp_path):
    """The volume-pinning claim, against real compose."""
    from types import SimpleNamespace
    from app.services import slot_compose_service as sc

    run = uuid.uuid4().hex[:8]
    root = tmp_path / f'shop{run}'
    root.mkdir()
    port = _free_port()
    (root / 'docker-compose.yml').write_text(yaml.safe_dump({
        'services': {'web': {'image': 'nginx:alpine', 'container_name': f'shop{run}',
                             'ports': [f'{port}:80'],
                             'volumes': ['uploads:/usr/share/nginx/html']}},
        'volumes': {'uploads': {}},
    }))
    original = sc.original_project(SimpleNamespace(root_path=str(root)))
    compose = ['docker', 'compose']
    try:
        subprocess.run(compose + ['-p', original, 'up', '-d'], cwd=root, check=True,
                       capture_output=True, timeout=300)
        # Data written by the original project.
        _docker('exec', f'shop{run}', 'sh', '-c', 'echo from-a > /usr/share/nginx/html/index.html')
        config = yaml.safe_load(subprocess.run(compose + ['-p', original, 'config'], cwd=root,
                                               check=True, capture_output=True, text=True).stdout)
        app = SimpleNamespace(id=1, name=f'shop{run}', root_path=str(root), port=port)
        slot_port = _free_port()
        rendered = sc.render_slot(app, config, 'web', slot_port, 80, {}, with_data_network=False)
        path = root / sc.SLOT_DIR / 'b.yml'
        sc.write(str(path), rendered)
        subprocess.run(compose + ['-p', f'shop{run}-b', '-f', str(path), 'up', '-d'], cwd=root,
                       check=True, capture_output=True, timeout=300)

        deadline = time.time() + 30
        body = None
        while time.time() < deadline:
            try:
                body = urllib.request.urlopen(f'http://127.0.0.1:{slot_port}/', timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        assert body is not None and body.decode().strip() == 'from-a', \
            'slot b did not see the original volume — it got an empty project-prefixed one'
        volumes = _docker('volume', 'ls', '--format', '{{.Name}}').stdout.split()
        assert f'shop{run}-b_uploads' not in volumes
    finally:
        subprocess.run(compose + ['-p', f'shop{run}-b', '-f', str(root / sc.SLOT_DIR / 'b.yml'),
                                  'down'], cwd=root, capture_output=True, timeout=120)
        subprocess.run(compose + ['-p', original, 'down', '-v'], cwd=root, capture_output=True,
                       timeout=120)
