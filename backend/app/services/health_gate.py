"""The deploy health gate (plan 87 §A3).

``_wait_for_health`` used to be advisory: it logged "did not pass" and the
deploy carried on as a success, and it counted a 4xx on the health path as
healthy. This gate is the one every deploy path now waits on, and it RAISES:

* the app must answer ``GET <path>`` with 2xx/3xx — a 4xx fails unless the
  app's settings explicitly allow it (a health path behind auth, say);
* it must do so ``consecutive`` times in a row, so a process that answers once
  and then crashes on its first real request does not pass;
* a container that has a Docker HEALTHCHECK must also report ``healthy``, and
  one that has exited fails at once instead of burning the whole timeout.
"""
import logging
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Seams the tests replace so a gate does not sleep for real.
sleep = time.sleep
clock = time.monotonic


class HealthGateError(RuntimeError):
    """The release never became healthy; ``last`` is the final probe result."""

    def __init__(self, message, last=None):
        super().__init__(message)
        self.last = last


def normalize_path(path: Optional[str]) -> str:
    path = (path or '/').strip() or '/'
    return path if path.startswith('/') else '/' + path


def probe(url: str, host_header: Optional[str] = None, timeout: float = 3.0):
    """One request: ``(status, error)`` — ``status`` is None when nothing answered."""
    request = urllib.request.Request(url, method='GET')
    request.add_header('User-Agent', 'ServerKit-HealthGate/1.0')
    if host_header:
        request.add_header('Host', host_header)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:  # noqa: BLE001 - not listening yet, reset, timeout
        return None, str(exc) or exc.__class__.__name__


def status_passes(status, allow_4xx: bool = False) -> bool:
    if status is None:
        return False
    if 200 <= status < 400:
        return True
    return allow_4xx and 400 <= status < 500


def container_health(container: str):
    """``(running, health)`` from ``docker inspect``; health is '' without a HEALTHCHECK."""
    from app.services.docker_service import DockerService
    result = DockerService.run(
        ['inspect', '--format',
         '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}', container],
        timeout=15)
    if not result.get('success'):
        return None, ''
    status, _, health = (result.get('output') or '').strip().partition('|')
    return status, health


def wait_healthy(port: int, path: Optional[str] = None, *, timeout: float = 120,
                 consecutive: int = 3, allow_4xx: bool = False,
                 host: str = '127.0.0.1', host_header: Optional[str] = None,
                 container: Optional[str] = None, interval: float = 1.0,
                 log: Optional[Callable[[str], None]] = None,
                 _probe=None, _container_health=None,
                 _sleep=None, _clock=None) -> dict:
    """Block until the release passes, or raise :class:`HealthGateError`."""
    do_probe = _probe or probe
    do_health = _container_health or container_health
    _sleep = _sleep or sleep
    _clock = _clock or clock
    url = f'http://{host}:{port}{normalize_path(path)}'
    deadline = _clock() + timeout
    streak = 0
    last = None
    attempts = 0
    while True:
        attempts += 1
        docker_ok = True
        if container:
            state, health = do_health(container)
            if state in ('exited', 'dead'):
                raise HealthGateError(f'Container {container} {state} before it became healthy',
                                      last=state)
            if health == 'unhealthy':
                raise HealthGateError(f'Container {container} reported unhealthy', last=health)
            # A HEALTHCHECK that is still 'starting' is not a pass yet.
            docker_ok = health in ('', 'healthy')

        status, error = do_probe(url, host_header)
        last = status if status is not None else error
        if docker_ok and status_passes(status, allow_4xx):
            streak += 1
            if streak >= consecutive:
                message = f'Health check passed ({url} -> {status}, {streak} in a row)'
                if log:
                    log(message)
                return {'url': url, 'status': status, 'attempts': attempts}
        else:
            streak = 0

        if _clock() >= deadline:
            raise HealthGateError(
                f'Health check did not pass within {int(timeout)}s ({url}; last: {last})',
                last=last)
        _sleep(interval)


def gate_for_app(app, port: Optional[int] = None, **overrides) -> dict:
    """:func:`wait_healthy` with the app's own path and settings."""
    from app.services import deploy_settings
    settings = deploy_settings.effective(app)
    kwargs = {
        'timeout': settings['healthcheck_timeout'],
        'consecutive': settings['healthcheck_consecutive'],
        'allow_4xx': settings['healthcheck_allow_4xx'],
    }
    kwargs.update(overrides)
    return wait_healthy(port or app.port, getattr(app, 'healthcheck_path', None), **kwargs)
