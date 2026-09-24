"""Deploy preflight — prove a deploy can work BEFORE anything live is stopped.

Plan 72 B.1. Every redeploy path used to reach its point of no return (a
``compose down`` / ``docker stop``) *before* anything had validated the new
configuration. An invalid compose file, an image that no longer pulls, or a
port somebody else took took the live site down and left it down: nginx 502s
until a human intervened.

The fix is not blue/green (that is B.2/B.3) — it is ordering. Everything that
can be checked while the old containers keep serving is checked here, and every
caller aborts on a blocking finding, at which point the live deployment is
byte-for-byte untouched. Nothing in this module stops, removes, recreates or
rewrites anything; the only side effects are image pulls/builds, which are
additive by construction.

The checks, in the order they run (a broken compose makes the rest meaningless):

1. **Compose validity** — wires the long-existing
   :meth:`DockerService.validate_compose_file` (``docker compose config
   --quiet``), whose only caller until now was a manual API endpoint.
2. **Image resolution** — build (when the project declares ``build:``) or pull,
   to completion. A resolution failure is downgraded to advisory when every
   referenced image is already present locally, so a locally-tagged image that
   was never pushed to a registry keeps deploying exactly as it did before.
3. **Host port conflicts** — reuses ``ManifestApplyService._port_bound`` (the
   same probe behind the manifest-apply ``kind: 'port_conflict'`` blocker) and
   only for ports the *new* configuration wants that the *currently running*
   project does not already publish. Probing a port the deploy's own live
   container holds would fail every ordinary redeploy, so owned ports are
   excluded; when ownership cannot be determined the check is skipped (a false
   abort is worse than a missed warning).
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from app.services.docker_service import DockerService

logger = logging.getLogger(__name__)

BLOCKING = 'error'
ADVISORY = 'warning'

# Finding kinds. ``port_conflict`` deliberately matches the manifest-apply
# blocker kind so the two surfaces speak the same vocabulary.
KIND_COMPOSE_INVALID = 'compose_invalid'
KIND_IMAGE_UNRESOLVED = 'image_unresolved'
KIND_BUILD_FAILED = 'build_failed'
KIND_PORT_CONFLICT = 'port_conflict'

LogFn = Optional[Callable[[str], None]]

#: Appended to every blocking log line. This is the user-visible payoff: the
#: deploy failed and the site is still up.
UNTOUCHED = 'The running deployment was left untouched.'


@dataclass(frozen=True)
class PreflightFinding:
    """One thing the preflight noticed. ``severity`` decides whether it aborts."""

    kind: str
    message: str
    severity: str = BLOCKING
    detail: str = ''

    def to_dict(self) -> Dict[str, str]:
        return {'kind': self.kind, 'message': self.message,
                'severity': self.severity, 'detail': self.detail}


@dataclass
class PreflightResult:
    """Structured pass/fail plus the findings behind it."""

    findings: List[PreflightFinding] = field(default_factory=list)
    checks: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    @property
    def blockers(self) -> List[PreflightFinding]:
        return [f for f in self.findings if f.severity == BLOCKING]

    @property
    def ok(self) -> bool:
        """True when nothing blocking was found — i.e. it is safe to switch over."""
        return not self.blockers

    @property
    def error(self) -> Optional[str]:
        blockers = self.blockers
        return blockers[0].message if blockers else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ok': self.ok,
            'checks': list(self.checks),
            'skipped': list(self.skipped),
            'findings': [f.to_dict() for f in self.findings],
        }

    def add(self, finding: PreflightFinding, log: LogFn = None) -> 'PreflightResult':
        self.findings.append(finding)
        prefix = 'Preflight failed' if finding.severity == BLOCKING else 'Preflight warning'
        suffix = f' {UNTOUCHED}' if finding.severity == BLOCKING else ''
        _emit(log, f'{prefix}: {finding.message}{suffix}')
        return self


def _emit(log: LogFn, message: str) -> None:
    """Push one human-readable line at the caller's log sink. Never raises.

    Deploy jobs pass the run-log callback here, so preflight lines land in the
    Deploy Console next to the compose transcript.
    """
    logger.info(message)
    if log is None:
        return
    try:
        log(message)
    except Exception:  # pragma: no cover - a broken sink must not fail a deploy
        pass


def _first_line(text: Any, limit: int = 400) -> str:
    """Condense a subprocess error blob into one readable line."""
    if not text:
        return ''
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    if not lines:
        return ''
    joined = lines[0] if len(lines) == 1 else ' | '.join(lines[:3])
    return joined[:limit]


# ---------------------------------------------------------------------------
# compose preflight
# ---------------------------------------------------------------------------

def preflight_compose_project(project_path: str, compose_file: str = None, *,
                              resolve_images: bool = True,
                              check_ports: bool = True,
                              log: LogFn = None,
                              label: str = None) -> PreflightResult:
    """Validate a compose project before its live containers are stopped.

    ``compose_file`` may name a candidate file that is not yet the project's
    real compose (that is how a template update validates the *next* compose
    while the current one keeps running).

    Returns a :class:`PreflightResult`; callers must abort on ``not ok`` and
    must not have stopped anything before calling.
    """
    result = PreflightResult()
    what = label or (compose_file or 'docker-compose.yml')
    _emit(log, f'Preflight: validating {what} before stopping the running containers')

    # 1. compose validity -----------------------------------------------------
    try:
        validation = DockerService.validate_compose_file(project_path, compose_file)
    except Exception as exc:  # pragma: no cover - defensive
        validation = {'valid': False, 'error': str(exc)}
    if not validation.get('valid'):
        return result.add(PreflightFinding(
            kind=KIND_COMPOSE_INVALID,
            message=(f'{what} is not a valid compose file — '
                     f'{_first_line(validation.get("error")) or "docker compose config rejected it"}'),
            detail=str(validation.get('error') or ''),
        ), log)
    result.checks.append('compose_config')

    # The parsed config drives both remaining checks; one `docker compose
    # config` for both rather than one each.
    config = _compose_config(project_path, compose_file)

    # 2. image resolution -----------------------------------------------------
    if resolve_images:
        if config is None:
            result.skipped.append('image_resolution')
        else:
            _resolve_images(project_path, compose_file, config, result, log)
            if not result.ok:
                return result

    # 3. port conflicts -------------------------------------------------------
    if check_ports:
        if config is None:
            result.skipped.append('port_conflicts')
        else:
            _check_ports(project_path, config, result, log)
            if not result.ok:
                return result

    _emit(log, 'Preflight passed — safe to switch over')
    return result


def _compose_config(project_path: str, compose_file: str = None) -> Optional[Dict[str, Any]]:
    """Parsed ``docker compose config``, or None when it cannot be read.

    None means "unknown", and every caller of this treats unknown as
    fail-open: an inability to introspect must never abort a deploy that the
    old code would have run.
    """
    try:
        parsed = DockerService.get_compose_config(project_path, compose_file)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug('compose config read failed for %s: %s', project_path, exc)
        return None
    if not parsed.get('success'):
        return None
    config = parsed.get('config')
    return config if isinstance(config, dict) else None


def _resolve_images(project_path: str, compose_file: str, config: Dict[str, Any],
                    result: PreflightResult, log: LogFn) -> None:
    """Build and pull every image the project needs, to completion.

    Services with a ``build:`` section are built (the failure mode git deploys
    actually hit); the rest are pulled. Doing both before the stop is the whole
    point — ``compose up`` would otherwise discover the failure with nothing
    left serving. In a mixed project the pull is issued per service, because a
    whole-project ``compose pull`` also tries to pull the buildable ones and
    fails on images that only exist once built.
    """
    services = config.get('services') or {}
    buildable = [name for name, svc in services.items()
                 if isinstance(svc, dict) and svc.get('build')]
    pullable = {name: svc['image'] for name, svc in services.items()
                if isinstance(svc, dict) and svc.get('image') and not svc.get('build')}

    if buildable:
        _emit(log, f'Preflight: building {len(buildable)} image(s) before the switchover')
        try:
            build = DockerService.compose_build(project_path, compose_file=compose_file)
        except Exception as exc:  # pragma: no cover - defensive
            build = {'success': False, 'error': str(exc)}
        if not build.get('success'):
            result.add(PreflightFinding(
                kind=KIND_BUILD_FAILED,
                message=(f'image build failed — '
                         f'{_first_line(build.get("error")) or "docker compose build returned a non-zero exit"}'),
                detail=str(build.get('error') or ''),
            ), log)
            return
        result.checks.append('image_build')

    images = sorted(set(pullable.values()))
    if not images:
        if not buildable:
            result.skipped.append('image_resolution')
        return

    _emit(log, f'Preflight: pulling {len(images)} image(s) before the switchover')
    pull = _pull_images(project_path, compose_file,
                        list(pullable) if buildable else None)
    if pull.get('success'):
        result.checks.append('image_pull')
        return

    # A pull failure is only fatal when the image genuinely isn't available.
    # Locally-built/tagged images that were never pushed cannot be pulled and
    # deployed fine before this check existed — do not start failing them.
    missing = [ref for ref in images if not _image_present(ref)]
    if not missing:
        result.add(PreflightFinding(
            kind=KIND_IMAGE_UNRESOLVED,
            severity=ADVISORY,
            message=('could not pull images, but every one is already present '
                     f'locally — continuing ({_first_line(pull.get("error"))})'),
            detail=str(pull.get('error') or ''),
        ), log)
        result.checks.append('image_pull')
        return

    result.add(PreflightFinding(
        kind=KIND_IMAGE_UNRESOLVED,
        message=(f'image {missing[0]} could not be resolved — '
                 f'{_first_line(pull.get("error")) or "docker compose pull failed"}'),
        detail=str(pull.get('error') or ''),
    ), log)


def _pull_images(project_path: str, compose_file: str,
                 services: Optional[List[str]]) -> Dict[str, Any]:
    """``compose pull`` for the whole project, or service by service."""
    targets = services if services is not None else [None]
    for service in targets:
        try:
            pull = DockerService.compose_pull(project_path, service=service,
                                              compose_file=compose_file)
        except Exception as exc:  # pragma: no cover - defensive
            pull = {'success': False, 'error': str(exc)}
        if not pull.get('success'):
            return pull
    return {'success': True}


def _image_present(image_ref: str) -> bool:
    """True when ``image_ref`` already exists in the local image store."""
    try:
        return bool(DockerService.image_exists(image_ref))
    except Exception:  # pragma: no cover - defensive
        return False


def _check_ports(project_path: str, config: Dict[str, Any],
                 result: PreflightResult, log: LogFn) -> None:
    """Flag host ports the new config wants that somebody else already holds."""
    desired = _desired_host_ports(config)
    if not desired:
        result.skipped.append('port_conflicts')
        return

    owned = published_host_ports(project_path)
    if owned is None:
        # Cannot tell which ports are ours; probing would flag our own live
        # containers and abort a perfectly good redeploy.
        result.skipped.append('port_conflicts')
        return

    for port in sorted(desired - owned):
        if _port_bound(port):
            result.add(PreflightFinding(
                kind=KIND_PORT_CONFLICT,
                message=(f'host port {port} is already in use by something else — '
                         f'free the port or publish this service on another one'),
                detail=f'port {port} is bound on 0.0.0.0 and is not published by this project',
            ), log)
    if result.ok:
        result.checks.append('port_conflicts')


def _port_bound(port: int) -> bool:
    """Reuse of the manifest-apply port probe (its ``port_conflict`` blocker).

    Called through the class so the existing test seam
    (``ManifestApplyService._port_bound``) keeps working for both surfaces.
    """
    try:
        from app.services.manifest_apply_service import ManifestApplyService
        return bool(ManifestApplyService._port_bound(int(port)))
    except Exception:  # pragma: no cover - defensive; unknown means "not a conflict"
        return False


# ---------------------------------------------------------------------------
# port bookkeeping
# ---------------------------------------------------------------------------

_PORTS_RE = re.compile(r'(?:(?:\d{1,3}(?:\.\d{1,3}){3}|\[[^\]]+\]|::):)?(\d+)(?:-\d+)?->')


def _desired_host_ports(config: Dict[str, Any]) -> Set[int]:
    """Host ports published by the compose config (long and short syntax)."""
    ports: Set[int] = set()
    for svc in (config.get('services') or {}).values():
        if not isinstance(svc, dict):
            continue
        for entry in svc.get('ports') or []:
            port = (_published_from_mapping(entry) if isinstance(entry, dict)
                    else _published_from_short(str(entry)))
            if port is not None:
                ports.add(port)
    return ports


def _published_from_mapping(entry: Dict[str, Any]) -> Optional[int]:
    published = entry.get('published')
    if published in (None, ''):
        return None
    try:
        return int(str(published).split('-')[0])
    except (TypeError, ValueError):
        return None


def _published_from_short(spec: str) -> Optional[int]:
    """`8080:80`, `127.0.0.1:8080:80`, `8080:80/tcp` -> 8080. `80` -> None."""
    spec = spec.strip().split('/')[0]
    parts = spec.split(':')
    if len(parts) < 2:
        return None            # container-only port: docker picks the host port
    candidate = parts[-2]
    if '-' in candidate:
        return None            # a range; fail open rather than guess
    try:
        return int(candidate)
    except ValueError:
        return None


def published_host_ports(project_path: str, compose_file: str = None) -> Optional[Set[int]]:
    """Host ports the project's *currently running* containers publish.

    Returns None when it cannot be determined — callers must then skip the
    port check rather than assume the ports are free.
    """
    try:
        # strict: a failed listing must read as "unknown", not "owns nothing" —
        # otherwise every redeploy flags the app's own ports as taken.
        containers = DockerService.compose_ps(project_path, compose_file, strict=True)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug('compose ps failed for %s: %s', project_path, exc)
        return None
    if containers is None:
        return None

    ports: Set[int] = set()
    for container in containers:
        if not isinstance(container, dict):
            continue
        for publisher in container.get('Publishers') or []:
            if not isinstance(publisher, dict):
                continue
            try:
                published = int(publisher.get('PublishedPort') or 0)
            except (TypeError, ValueError):
                continue
            if published:
                ports.add(published)
        raw = container.get('Ports')
        if isinstance(raw, str):
            ports.update(int(m) for m in _PORTS_RE.findall(raw))
    return ports


# ---------------------------------------------------------------------------
# single-container preflight
# ---------------------------------------------------------------------------

def preflight_image(image_ref: str, *, pull: Callable[[], Dict[str, Any]] = None,
                    log: LogFn = None) -> PreflightResult:
    """Resolve a single container image before its predecessor is stopped.

    The ``docker run`` deploy path has no compose file to validate; its one
    pre-traffic requirement is that the image exists by the time the old
    container is gone. ``pull`` is supplied by the caller (it owns registry
    authentication); without it the image came from the build step that already
    ran, and only local presence is asserted.
    """
    result = PreflightResult()
    if not image_ref:
        return result.add(PreflightFinding(
            kind=KIND_IMAGE_UNRESOLVED,
            message='no image was produced for this deployment'), log)

    if pull is not None:
        _emit(log, f'Preflight: pulling {image_ref} before the switchover')
        try:
            pulled = pull()
        except Exception as exc:  # pragma: no cover - defensive
            pulled = {'success': False, 'error': str(exc)}
        if not pulled.get('success'):
            return result.add(PreflightFinding(
                kind=KIND_IMAGE_UNRESOLVED,
                message=(f'image {image_ref} could not be pulled — '
                         f'{_first_line(pulled.get("error")) or "registry pull failed"}'),
                detail=str(pulled.get('error') or ''),
            ), log)
        result.checks.append('image_pull')
    else:
        result.checks.append('image_from_build')

    _emit(log, f'Preflight passed — {image_ref} is resolved, safe to switch over')
    return result
