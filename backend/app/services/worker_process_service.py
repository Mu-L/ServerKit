"""Procfile worker processes for build-pack apps (plan 86 §C3).

A build-pack deploy runs one container, ``serverkit-app-<id>``, from the
image it built, and only the Procfile ``web:`` line used to matter. Every other
process line (``worker:``, ``scheduler:``, ...) now runs as a sibling
container of the same image, ``serverkit-app-<id>-<process>``, with the app's
env and volumes, a restart policy and no port (a worker serves no traffic and
gets no domain). ``release:`` is a one-off phase in the Procfile convention,
not a long-running process, so it is never started here.

The Procfile in the app's source is read at deploy time; a line removed from
it stops its container on the next deploy.
"""
import logging
import re
import shlex
from collections import OrderedDict
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Procfile process types that are not long-running siblings.
NOT_A_WORKER = ('web', 'release')
_LINE = re.compile(r'^\s*([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$')


def parse_procfile(text: str) -> "OrderedDict[str, str]":
    """``name -> command`` for every Procfile line, first definition wins."""
    processes = OrderedDict()
    for raw in (text or '').splitlines():
        if raw.lstrip().startswith('#'):
            continue
        match = _LINE.match(raw)
        if match and match.group(1).lower() not in processes:
            processes[match.group(1).lower()] = match.group(2)
    return processes


def worker_processes(text: str) -> "OrderedDict[str, str]":
    return OrderedDict((k, v) for k, v in parse_procfile(text).items()
                       if k not in NOT_A_WORKER)


def read_workers(root_path: Optional[str]) -> "OrderedDict[str, str]":
    import os
    if not root_path:
        return OrderedDict()
    try:
        with open(os.path.join(root_path, 'Procfile'), 'r', encoding='utf-8') as fh:
            return worker_processes(fh.read())
    except OSError:
        return OrderedDict()


def container_prefix(app) -> str:
    return f'serverkit-app-{app.id}-'


def container_name(app, process: str) -> str:
    return container_prefix(app) + re.sub(r'[^a-z0-9_-]', '-', process.lower())


class WorkerProcessService:

    @staticmethod
    def existing(app) -> List[str]:
        """Names of this app's worker containers, running or not."""
        from app.services.docker_service import DockerService
        prefix = container_prefix(app)
        result = DockerService.run(['ps', '-a', '--filter', f'name=^{prefix}',
                                    '--format', '{{.Names}}'], timeout=30)
        if not result.get('success'):
            return []
        return [n.strip() for n in (result.get('output') or '').splitlines()
                if n.strip().startswith(prefix)]

    @classmethod
    def deploy(cls, app, image: str, env: Dict[str, str], volumes: List[str],
               log: Optional[Callable[[str], None]] = None) -> Dict:
        """Replace this app's workers with one container per Procfile process.

        Best-effort per worker: a worker that fails to start is reported, but
        it never fails the deploy of the web process that is already live.
        """
        from app.services.docker_service import DockerService

        wanted = read_workers(app.root_path)
        for name in cls.existing(app):
            DockerService.stop_container(name)
            DockerService.remove_container(name)

        started, failed = [], {}
        for process, command in wanted.items():
            name = container_name(app, process)
            if log:
                log(f'Starting worker {process}: {command}')
            result = DockerService.run_container(
                image=image, name=name, env=env or None, volumes=volumes or None,
                restart_policy='unless-stopped',
                # Through a shell, like the web CMD: Procfile lines use $VARS,
                # && and pipes.
                command=f'sh -c {shlex.quote(command)}',
                detach=True,
            )
            if result.get('success'):
                started.append(process)
                connect_shared_network(app, name)
            else:
                failed[process] = result.get('error') or 'failed to start'
                logger.warning('worker %s for app %s failed: %s', process, app.id, failed[process])
        return {'started': started, 'failed': failed}

    @classmethod
    def each(cls, app, action: str) -> None:
        """start / stop / restart every worker container (best-effort)."""
        from app.services.docker_service import DockerService
        fn = {'start': DockerService.start_container, 'stop': DockerService.stop_container,
              'restart': DockerService.restart_container}[action]
        for name in cls.existing(app):
            try:
                fn(name)
            except Exception as exc:  # noqa: BLE001 - the web process decides the outcome
                logger.warning('worker %s %s failed: %s', name, action, exc)


def connect_shared_network(app, container: str) -> None:
    """Join a `docker run` container to the shared service network when the app
    offers or uses a connectable service (plan 86 §C1). Compose apps get this
    from their managed override; single-container apps get it here."""
    try:
        from app.services.docker_service import DockerService
        from app.services.service_connection_service import (
            SHARED_NETWORK, ServiceConnectionService)
        if not ServiceConnectionService.needs_shared_network(app):
            return
        DockerService.ensure_network(SHARED_NETWORK)
        DockerService.run(['network', 'connect', SHARED_NETWORK, container], timeout=30)
    except Exception as exc:  # noqa: BLE001 - never fail a deploy over it
        logger.warning('shared network join failed for %s: %s', container, exc)
