"""Slot deploys for compose apps (plan 87 §C).

Each slot is its own compose project, ``<name>-a`` / ``<name>-b``, rendered
from the app's merged compose config (``docker compose config`` over the base
file plus the ServerKit env override, so variables are interpolated and every
relative path is already absolute). The render:

* strips ``container_name`` — two slots cannot both own one name, and 119 of
  120 templates set it;
* publishes only the web service, on the slot's loopback port;
* drops every other host publish (a second copy would collide);
* PINS every named volume to the name it has today. Without an explicit
  ``name:``, compose prefixes volumes with the project name, and slot B would
  silently start on empty volumes;
* leaves networks per-slot (a shared default network would give two services
  the same DNS name), and joins the shared ``<name>-data`` network.

A stack with stateful services (a database, a cache, a queue — anything but the
web service that mounts a named volume) cannot run twice. Those move ONCE, on
an explicit, previewed split, into a ``<name>-data`` project that both slots
reach over its network by the same service names as before.
"""
import copy
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

SLOT_DIR = '.serverkit-slots'
DATA = 'data'


def project_base(app) -> str:
    """``<name>`` for the ``<name>-a`` / ``-b`` / ``-data`` projects."""
    base = re.sub(r'[^a-z0-9_-]+', '-', (app.name or f'app{app.id}').lower()).strip('-_')
    return base or f'app{app.id}'


def slot_project(app, slot: str) -> str:
    return f'{project_base(app)}-{slot}'


def data_network(app) -> str:
    return f'{project_base(app)}-data'


def original_project(app) -> str:
    """The project name compose derives for the in-place deploy."""
    base = os.path.basename(os.path.normpath(app.root_path or ''))
    return re.sub(r'[^a-z0-9_-]', '', base.lower())


def slot_file(app, slot: str) -> str:
    return os.path.join(app.root_path, SLOT_DIR, f'{slot}.yml')


# ── reading the merged config ────────────────────────────────────────────────

def merged_config(app) -> Tuple[Optional[dict], Optional[str]]:
    """The app's compose config as compose resolves it, with the env override."""
    from app.services.docker_service import DockerService
    cmd = DockerService._compose_cmd_with_overlay(app.root_path, app.compose_file) + ['config']
    result = DockerService.run_compose(cmd, cwd=app.root_path, timeout=120)
    if not result.get('success'):
        return None, result.get('error') or 'docker compose config failed'
    try:
        config = yaml.safe_load(result.get('output') or '')
    except yaml.YAMLError as exc:
        return None, f'could not read the compose config: {exc}'
    if not isinstance(config, dict) or not isinstance(config.get('services'), dict):
        return None, 'the compose config has no services'
    return config, None


def _published(port) -> Tuple[Optional[int], Optional[int]]:
    """``(host, container)`` for a long- or short-syntax port entry."""
    if isinstance(port, dict):
        published, target = port.get('published'), port.get('target')
        try:
            return (int(published) if published not in (None, '') else None,
                    int(target) if target is not None else None)
        except (TypeError, ValueError):
            return None, None
    parts = str(port).split('/')[0].split(':')
    try:
        if len(parts) == 1:
            return None, int(parts[0])
        return int(parts[-2]), int(parts[-1])
    except ValueError:
        return None, None


def web_service(config: dict, port: int) -> Tuple[Optional[str], Optional[int]]:
    """The service that publishes the app's port, and the port it listens on."""
    for name, service in (config.get('services') or {}).items():
        for entry in service.get('ports') or []:
            host, target = _published(entry)
            if host == port:
                return name, target
    return None, None


def _named_volumes(service: dict) -> List[str]:
    names = []
    for mount in service.get('volumes') or []:
        if isinstance(mount, dict) and mount.get('type') == 'volume' and mount.get('source'):
            names.append(mount['source'])
    return names


def classify(config: dict, web: str) -> Tuple[List[str], List[str]]:
    """``(stateless, stateful)`` service names. Stateful = mounts a named
    volume and is not the web service; the web service's volumes are shared
    between slots (and need the operator's word, like a single container)."""
    stateless, stateful = [], []
    for name, service in (config.get('services') or {}).items():
        if name != web and _named_volumes(service):
            stateful.append(name)
        else:
            stateless.append(name)
    return stateless, stateful


# ── eligibility ──────────────────────────────────────────────────────────────

def eligibility(app, config: Optional[dict] = None) -> Dict:
    from app.services import deploy_settings
    reasons, warnings = [], []
    if config is None:
        config, error = merged_config(app)
        if config is None:
            return {'reasons': [f'Could not read the compose file: {error}'], 'warnings': [],
                    'split_needed': False}
    settings = deploy_settings.effective(app)
    # Once on slots, app.port is the live slot's port; the compose file still
    # publishes the port recorded at adoption.
    port = settings['compose_web_port'] or app.port
    web = settings['compose_web'] if settings['compose_web'] in config['services'] else None
    if not web:
        web, _target = web_service(config, port)
    if not web:
        return {'reasons': [f'No service in the compose file publishes port {port}, '
                            'so there is no web service to run twice.'],
                'warnings': [], 'split_needed': False}
    _stateless, stateful = classify(config, web)
    split_needed = bool(stateful) and not settings['compose_data_split']
    if split_needed:
        reasons.append(f"The stack has stateful services ({', '.join(stateful)}) that cannot run "
                       f'twice. They move once into a shared {data_network(app)} project first; '
                       'preview the split in Settings.')
    shared = _named_volumes(config['services'][web])
    if shared:
        names = ', '.join(shared)
        if settings['slot_volumes_confirmed']:
            warnings.append(f'Both slots of {web} mount the same volumes ({names}) for a few '
                            'seconds around each switch. You confirmed the app handles it.')
        else:
            reasons.append(f'Both slots of {web} would mount the same volumes ({names}) for a '
                           'few seconds around each switch; confirm the app handles it to enable.')
    return {'reasons': reasons, 'warnings': warnings, 'split_needed': split_needed,
            'web': web, 'stateful': stateful}


# ── rendering ────────────────────────────────────────────────────────────────

def _pin_volumes(config: dict, keep: Optional[set] = None) -> dict:
    volumes = {}
    for name, spec in (config.get('volumes') or {}).items():
        if keep is not None and name not in keep:
            continue
        spec = dict(spec or {})
        # compose config already emits the resolved name (<project>_<vol>);
        # keeping it verbatim is exactly the pin. Say so if it ever doesn't.
        if not spec.get('name') and not spec.get('external'):
            logger.warning('volume %s has no resolved name; slots would not share it', name)
        volumes[name] = spec
    return volumes


def _used_volumes(services: dict) -> set:
    used = set()
    for service in services.values():
        used.update(_named_volumes(service))
    return used


def render_slot(app, config: dict, web: str, host_port: int, container_port: int,
                image_tags: Dict[str, str], with_data_network: bool) -> dict:
    """The compose project for one slot (see the module docstring)."""
    _stateless, stateful = classify(config, web)
    split = with_data_network
    services = {}
    for name, service in (config.get('services') or {}).items():
        if split and name in stateful:
            continue
        service = copy.deepcopy(service)
        service.pop('container_name', None)
        if name == web:
            service['ports'] = [f'127.0.0.1:{host_port}:{container_port}']
        else:
            service.pop('ports', None)
        if name in image_tags:
            # `build` + `image`: compose builds under this immutable tag (a
            # cache hit after the preflight build), then `up --no-build` runs
            # exactly that image, and a rollback can find it again.
            service['image'] = image_tags[name]
        if split and isinstance(service.get('depends_on'), dict):
            service['depends_on'] = {k: v for k, v in service['depends_on'].items()
                                     if k not in stateful}
            if not service['depends_on']:
                service.pop('depends_on')
        if split and 'network_mode' not in service:
            networks = service.get('networks')
            if not isinstance(networks, dict):
                networks = {n: None for n in (networks or ['default'])}
            networks.setdefault('default', None)
            networks[data_network(app)] = None
            service['networks'] = networks
        services[name] = service
    rendered = {'services': services}
    volumes = _pin_volumes(config, keep=_used_volumes(services))
    if volumes:
        rendered['volumes'] = volumes
    networks = {}
    for name, spec in (config.get('networks') or {}).items():
        spec = dict(spec or {})
        if not spec.get('external'):
            spec.pop('name', None)       # per-slot: never share a default network
        networks[name] = spec
    if split:
        networks[data_network(app)] = {'external': True, 'name': data_network(app)}
    if networks:
        rendered['networks'] = networks
    return rendered


def render_data(app, config: dict, web: str) -> dict:
    """The shared ``<name>-data`` project: the stateful services, on a named
    network the slots join, with their volumes pinned to today's names."""
    _stateless, stateful = classify(config, web)
    services = {}
    for name in stateful:
        service = copy.deepcopy(config['services'][name])
        service.pop('container_name', None)
        service.pop('depends_on', None)
        services[name] = service
    rendered = {'services': services}
    volumes = _pin_volumes(config, keep=_used_volumes(services))
    if volumes:
        rendered['volumes'] = volumes
    rendered['networks'] = {'default': {'name': data_network(app)}}
    return rendered


def write(path: str, rendered: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('# Rendered by ServerKit for A/B slot deploys (plan 87). Do not edit:\n'
                 '# it is regenerated from the app\'s compose file on every deploy.\n')
        yaml.safe_dump(rendered, fh, sort_keys=False, default_flow_style=False)


# ── running slot projects ────────────────────────────────────────────────────

def compose(project: str, files: List[str], *args, cwd=None, timeout=None) -> Dict:
    from app.services.docker_service import DockerService
    cmd = DockerService._get_compose_cmd() + ['-p', project]
    for f in files:
        cmd += ['-f', f]
    return DockerService.run_compose(cmd + list(args), cwd=cwd, timeout=timeout)


def project_containers(project: str, service: Optional[str] = None) -> List[Dict]:
    """Containers of a compose project by label (running or not)."""
    import json
    from app.services.docker_service import DockerService
    args = ['ps', '-a', '--filter', f'label=com.docker.compose.project={project}']
    if service:
        args += ['--filter', f'label=com.docker.compose.service={service}']
    result = DockerService.run(args + ['--format', '{{json .}}'], timeout=30)
    rows = []
    for line in (result.get('output') or '').splitlines() if result.get('success') else []:
        try:
            c = json.loads(line)
        except ValueError:
            continue
        labels = c.get('Labels') or ''
        svc = next((part.split('=', 1)[1] for part in labels.split(',')
                    if part.startswith('com.docker.compose.service=')), None)
        rows.append({'id': c.get('ID'), 'name': c.get('Names'), 'service': svc or service,
                     'state': (c.get('State') or '').lower()})
    return rows
