"""Centralized container status aggregator.

Collapses the per-container Docker states of an app/service/database into ONE
deterministic status using a fixed priority hierarchy. The aggregation
(``aggregate_status``) is a pure function with no Docker dependency so it can be
unit-tested in isolation; the ``get_*_status`` helpers wire it to real Docker
data and a short-TTL cache.

Collection is a SINGLE `docker ps` for the whole host, indexed back onto apps by
compose labels (see "Bulk collection" below) and shared between callers by a
short-TTL snapshot — not one `docker compose ps` per app plus one
`docker inspect` per container, which is what made ``/status/apps`` cost ~120
process spawns per request. Anything that changes container state must call
``invalidate()``.

Status vocabulary (the aggregated enum):

    running:healthy    every container running, health checks (if any) passing
    running:unhealthy  running but at least one container reports unhealthy
    degraded           a multi-container set is only partially up
                       (some running, some not) — the set isn't whole
    restarting         at least one container is in a restart loop
    starting           at least one container is starting / health=starting
    exited             nothing is running (all stopped/exited) but containers exist
    unknown            no containers, entity missing, or Docker unavailable

Priority hierarchy (highest precedence first). The aggregate is whichever of
these conditions is true first:

    degraded > restarting > running:unhealthy > starting > running:healthy
    > exited > unknown

Rationale: a partially-up set (``degraded``) is the loudest problem because the
app is wedged between states; an active restart loop is next; an explicit
unhealthy health-check beats a quiet "starting"; a clean all-running set is the
happy path; ``exited`` is a calm, fully-stopped state; ``unknown`` is the floor.
"""

import logging
import os
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ---- Aggregated status constants -------------------------------------------
STATUS_RUNNING_HEALTHY = 'running:healthy'
STATUS_RUNNING_UNHEALTHY = 'running:unhealthy'
STATUS_DEGRADED = 'degraded'
STATUS_RESTARTING = 'restarting'
STATUS_STARTING = 'starting'
STATUS_EXITED = 'exited'
STATUS_UNKNOWN = 'unknown'

# Precedence: index 0 wins. Used both to document and to pick the aggregate.
STATUS_PRECEDENCE = [
    STATUS_DEGRADED,
    STATUS_RESTARTING,
    STATUS_RUNNING_UNHEALTHY,
    STATUS_STARTING,
    STATUS_RUNNING_HEALTHY,
    STATUS_EXITED,
    STATUS_UNKNOWN,
]

# Cache namespace + TTL. Short on purpose so the pill is "live enough" without
# hammering the Docker CLI on every render / list load.
_CACHE_PREFIX = 'container_status'
_CACHE_TTL = 8  # seconds

# TTL for the shared host-wide `docker ps` snapshot (see _get_container_index).
# Deliberately SHORTER than the 5s socket-broadcast tick so the emitter never
# reuses the previous tick's collection — the snapshot only ever collapses a
# burst of near-simultaneous callers (the HTTP route + the emitter + a page
# that renders 30 status pills), never a whole polling interval.
_SNAPSHOT_TTL = 3.0  # seconds

# The snapshot lives in module memory rather than CacheService on purpose: the
# panel runs a single worker process (see CLAUDE.md / docs/ARCHITECTURE.md), it
# must be droppable synchronously by invalidate(), and round-tripping the whole
# container list through JSON would undo part of what we just saved.
_snapshot_lock = threading.Lock()
_snapshot: Dict[str, Any] = {'expires': 0.0, 'index': None}


def _normalize_state(raw):
    """Map a raw docker state/status string to one of: running, restarting,
    starting, exited, unknown. Defensive against None / odd casing."""
    if not raw:
        return 'unknown'
    s = str(raw).strip().lower()
    if s in ('running', 'up'):
        return 'running'
    if s in ('restarting',):
        return 'restarting'
    if s in ('created', 'starting'):
        return 'starting'
    if s in ('exited', 'dead', 'stopped', 'paused', 'removing'):
        return 'exited'
    return 'unknown'


def _normalize_health(raw):
    """Map a raw docker health string to: healthy, unhealthy, starting, none."""
    if not raw:
        return 'none'
    s = str(raw).strip().lower()
    if s == 'healthy':
        return 'healthy'
    if s == 'unhealthy':
        return 'unhealthy'
    if s == 'starting':
        return 'starting'
    if s in ('none', 'no-healthcheck', 'no healthcheck'):
        return 'none'
    return 'none'


def aggregate_status(container_states):
    """Collapse a list of per-container states into one aggregated status.

    Pure function — no Docker calls. This is the single source of truth for the
    precedence hierarchy and is what the tests exercise directly.

    Args:
        container_states: list of dicts, each with at least ``state`` and an
            optional ``health`` (and optional ``name``/``service`` used only for
            human-readable reasons). Example:
                {'name': 'app-db-1', 'state': 'running', 'health': 'unhealthy'}

    Returns:
        dict: {
            'status':   <aggregated enum>,
            'total':    <container count>,
            'healthy':  <count of running + (healthy|none) containers>,
            'reasons':  [<short human strings>],
            'containers': [<normalized per-container dicts>],
        }
    """
    containers = []
    for c in (container_states or []):
        c = c or {}
        state = _normalize_state(c.get('state'))
        health = _normalize_health(c.get('health'))
        containers.append({
            'name': c.get('name') or c.get('service') or c.get('id') or '?',
            'service': c.get('service'),
            'state': state,
            'health': health,
        })

    total = len(containers)
    if total == 0:
        return {
            'status': STATUS_UNKNOWN,
            'total': 0,
            'healthy': 0,
            'reasons': ['no containers'],
            'containers': [],
        }

    running = [c for c in containers if c['state'] == 'running']
    restarting = [c for c in containers if c['state'] == 'restarting']
    starting = [c for c in containers if c['state'] == 'starting']
    # A running container counts as "healthy" unless it explicitly reports
    # unhealthy. No health-check (health='none') is treated as healthy.
    healthy = [c for c in running if c['health'] in ('healthy', 'none')]
    unhealthy = [c for c in running if c['health'] == 'unhealthy']
    health_starting = [c for c in running if c['health'] == 'starting']

    reasons = []

    # --- Apply the precedence hierarchy, highest first ---

    # degraded: a partially-up multi-container set (some running, some not).
    not_running = [c for c in containers if c['state'] != 'running']
    if total > 1 and running and not_running and not restarting:
        down = ', '.join(c['name'] for c in not_running)
        reasons.append(f'partially up — down: {down}')
        status = STATUS_DEGRADED

    # restarting: at least one container is looping.
    elif restarting:
        reasons.append('restarting: ' + ', '.join(c['name'] for c in restarting))
        status = STATUS_RESTARTING

    # running:unhealthy: running but a health check is failing.
    elif unhealthy:
        reasons.append('unhealthy: ' + ', '.join(c['name'] for c in unhealthy))
        status = STATUS_RUNNING_UNHEALTHY

    # starting: a container (or its health check) is still coming up.
    elif starting or health_starting:
        coming_up = starting + health_starting
        reasons.append('starting: ' + ', '.join(c['name'] for c in coming_up))
        status = STATUS_STARTING

    # running:healthy: everything is up and (if checked) healthy.
    elif len(running) == total:
        status = STATUS_RUNNING_HEALTHY

    # unknown: nothing running and every container is in an unrecognized state.
    elif all(c['state'] == 'unknown' for c in containers):
        reasons.append('container state unknown')
        status = STATUS_UNKNOWN

    # exited: containers exist but none are running.
    elif not running:
        reasons.append('all containers stopped')
        status = STATUS_EXITED

    else:
        status = STATUS_UNKNOWN

    return {
        'status': status,
        'total': total,
        'healthy': len(healthy),
        'reasons': reasons,
        'containers': containers,
    }


# ---- Bulk collection -------------------------------------------------------
#
# The naive shape of this module was: one `docker compose ps` subprocess per
# app, plus one `docker inspect` per running container to read its health. At
# ~30 apps x ~3 containers that is ~120 process spawns for a SINGLE /status/apps
# request, repeated on a timer by the socket emitter. Both of those costs are
# avoidable:
#
#   * `docker ps` already knows about every container on the host, and compose
#     stamps each one with project/service/working_dir labels — so one call can
#     be indexed back onto the apps that own the containers.
#   * `docker ps`'s STATUS column already carries the health-check result
#     ("Up 2 minutes (unhealthy)"), which is the only thing the per-container
#     `docker inspect` was being spawned for.
#
# Net: ~120 spawns -> 1 per collection pass, shared between callers by a
# short-TTL snapshot.

# 'Up 2 minutes (healthy)' / '(unhealthy)' / '(health: starting)'.
_HEALTH_SUFFIX_RE = re.compile(r'\((?:health:\s*)?(healthy|unhealthy|starting)\)', re.IGNORECASE)

# Compose derives a project name from the directory name by lowercasing and
# dropping everything outside [a-z0-9_-] (then trimming leading separators).
_PROJECT_NAME_STRIP_RE = re.compile(r'[^a-z0-9_-]')


def _health_from_status(status: Optional[str]) -> Optional[str]:
    """Pull the health-check result out of a `docker ps` STATUS string.

    Returns None when the container has no health check, which
    ``_normalize_health`` reads as 'none' — same as a failed inspect did.
    """
    if not status:
        return None
    match = _HEALTH_SUFFIX_RE.search(str(status))
    return match.group(1).lower() if match else None


def _norm_path(path: Optional[str]) -> Optional[str]:
    """Normalize a filesystem path for equality comparison.

    Compose records ``project.working_dir`` as an absolute path; the panel
    stores ``root_path``. They can differ only by trailing slash, ``..`` or a
    symlink, so normalize both sides the same way before comparing.
    """
    if not path:
        return None
    try:
        return os.path.normcase(os.path.normpath(str(path).strip()))
    except Exception:
        return None


def _real_path(path: Optional[str]) -> Optional[str]:
    """``_norm_path`` after symlink resolution, or None if it doesn't resolve."""
    if not path:
        return None
    try:
        resolved = os.path.normcase(os.path.realpath(str(path).strip()))
    except Exception:
        return None
    return resolved or None


def _compose_project_name(root_path: Optional[str]) -> Optional[str]:
    """The project name compose would derive from ``root_path``'s basename."""
    if not root_path:
        return None
    base = os.path.basename(os.path.normpath(str(root_path).strip()))
    if not base:
        return None
    return _PROJECT_NAME_STRIP_RE.sub('', base.lower()).lstrip('_-') or None


class _ContainerIndex:
    """One host-wide `docker ps` result, indexed by every key an app can match.

    An Application identifies its containers three ways, in descending order of
    trust:

      1. its compose project directory (``root_path``) — matched against the
         ``com.docker.compose.project.working_dir`` label, which compose sets
         regardless of a custom ``COMPOSE_PROJECT_NAME``;
      2. the compose project NAME derived from that directory — the fallback
         when the working_dir label is absent (older compose) or the project
         was brought up from a different path;
      3. an explicit ``container_id`` for non-compose, single-container apps.

    Anything that matches nothing yields an empty list, which aggregates to
    'unknown' — exactly what an empty `docker compose ps` produced before.
    """

    __slots__ = ('by_dir', 'by_project', 'by_id', 'count')

    def __init__(self, containers: Iterable[Dict[str, Any]]):
        self.by_dir: Dict[str, List[Dict[str, Any]]] = {}
        self.by_project: Dict[str, List[Dict[str, Any]]] = {}
        self.by_id: Dict[str, Dict[str, Any]] = {}
        self.count = 0

        for c in containers or []:
            self.count += 1
            for key in self._dir_keys(c):
                self.by_dir.setdefault(key, []).append(c)

            project = (c.get('project') or '').strip().lower()
            if project:
                self.by_project.setdefault(project, []).append(c)

            cid = (c.get('id') or '').strip()
            name = (c.get('name') or '').strip().lstrip('/')
            if cid:
                self.by_id[cid] = c
                # Apps often store a short id; index the 12-char prefix too.
                self.by_id.setdefault(cid[:12], c)
            if name:
                self.by_id.setdefault(name, c)

    @staticmethod
    def _dir_keys(container: Dict[str, Any]) -> List[str]:
        """Directory keys a container can be found under (normalized + real)."""
        keys = []
        candidates = [container.get('working_dir')]
        # config_files is a comma-separated list of compose file paths; their
        # directory is the project dir when working_dir wasn't stamped.
        for cf in (container.get('config_files') or '').split(','):
            cf = cf.strip()
            if cf:
                candidates.append(os.path.dirname(cf))
        for candidate in candidates:
            for key in (_norm_path(candidate), _real_path(candidate)):
                if key and key not in keys:
                    keys.append(key)
        return keys

    def for_app(self, app) -> List[Dict[str, Any]]:
        """Containers belonging to ``app``, in the trust order documented above."""
        slot_project = _live_slot_project(app)
        if slot_project:
            # A compose app on A/B slots (plan 87): only the live slot's
            # project counts. The standby is not rolled into the status, so a
            # stopped standby never reads as a degraded app.
            return self.by_project.get(slot_project, [])
        root_path = getattr(app, 'root_path', None)
        for key in (_norm_path(root_path), _real_path(root_path)):
            if key and key in self.by_dir:
                return self.by_dir[key]

        project = _compose_project_name(root_path)
        if project and project in self.by_project:
            return self.by_project[project]

        container_id = (getattr(app, 'container_id', None) or '').strip()
        if container_id and container_id in self.by_id:
            return [self.by_id[container_id]]

        return []


def _live_slot_project(app) -> Optional[str]:
    if not (getattr(app, 'slot_deploys_enabled', False) and getattr(app, 'active_slot', None)
            and getattr(app, 'compose_file', None)):
        return None
    try:
        from app.models.app_slot import AppSlot
        row = AppSlot.query.filter_by(application_id=app.id, slot=app.active_slot).first()
    except Exception:  # noqa: BLE001 - no app context / table: fall back to the usual mapping
        return None
    return (row.project_name or '').lower() or None if row else None


def _collect_container_index() -> _ContainerIndex:
    """Run the ONE `docker ps` and index it. Never raises."""
    from app.services.docker_service import DockerService
    try:
        containers = DockerService.list_compose_containers() or []
    except Exception as e:
        logger.warning('Bulk container collection failed: %s', e)
        containers = []
    return _ContainerIndex(containers)


def _get_container_index(use_cache: bool = True) -> _ContainerIndex:
    """The shared host-wide container snapshot, collected at most once per TTL.

    This is what makes the HTTP route and the socket emitter share a single
    collection pass instead of each triggering their own.
    """
    if not use_cache:
        return _collect_container_index()

    now = time.monotonic()
    with _snapshot_lock:
        if _snapshot['index'] is not None and _snapshot['expires'] > now:
            return _snapshot['index']

    # Collected outside the lock: a slow/hung docker must not serialize every
    # caller behind it. A rare duplicate collection is cheaper than a stall.
    index = _collect_container_index()
    with _snapshot_lock:
        _snapshot['index'] = index
        _snapshot['expires'] = time.monotonic() + _SNAPSHOT_TTL
    return index


def invalidate(application_id=None) -> None:
    """Drop cached status so the next read re-collects from Docker.

    MUST be called by anything that changes container state (start / stop /
    restart / deploy). A cached status that survives an explicit user action is
    worse than a slow one: the UI would show the pre-action state and look
    broken. Passing ``application_id`` also clears that app's cached result;
    the host snapshot is always dropped because one app's compose up/down
    changes rows other apps are read from.
    """
    with _snapshot_lock:
        _snapshot['index'] = None
        _snapshot['expires'] = 0.0

    from app.services.cache_service import CacheService
    try:
        if application_id is None:
            CacheService.delete_pattern(f'{_CACHE_PREFIX}:*')
        else:
            CacheService.delete(f'{_CACHE_PREFIX}:app:{application_id}')
    except Exception:  # cache is best-effort; the snapshot drop already landed
        pass


def _states_from_index_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`docker ps` rows -> the {id,name,service,state,health} shape."""
    states = []
    for c in rows or []:
        state = (c.get('state') or '').strip()
        if not state:
            # Very old daemons don't expose a State column in `docker ps`; the
            # STATUS text ("Up 2 minutes", "Exited (0) ...") still tells us.
            status_text = (c.get('status') or '').strip().lower()
            if status_text.startswith('up'):
                state = 'running'
            elif status_text:
                state = status_text.split()[0]
        states.append({
            'id': c.get('id') or c.get('name'),
            'name': (c.get('name') or '').lstrip('/') or None,
            'service': c.get('service') or None,
            'state': state or None,
            'health': _health_from_status(c.get('status')),
        })
    return states


def _gather_remote_app_container_states(app) -> List[Dict[str, Any]]:
    """Container states for an app that lives on a remote (agent) server.

    Remote hosts are NOT part of the local `docker ps` snapshot, so they go
    through ``RemoteDockerService``. Gated on the agent actually being
    connected: ``send_command`` blocks up to 30s waiting for a reply, and this
    runs inside the 5s status-broadcast loop, so an offline server must cost
    nothing rather than stall every other app's status.
    """
    from app.services.agent_registry import agent_registry
    from app.services.remote_docker_service import RemoteDockerService

    try:
        if not agent_registry.get_agent(app.server_id):
            return []
    except Exception:
        return []

    target = os.path.join(app.root_path, app.compose_file or 'docker-compose.yml') \
        if getattr(app, 'root_path', None) else None
    if not target:
        return []

    try:
        result = RemoteDockerService.compose_ps(app.server_id, target) or {}
    except Exception as e:
        logger.warning('Remote status for app %s failed: %s', getattr(app, 'id', '?'), e)
        return []
    if not result.get('success'):
        return []

    states = []
    for c in (result.get('data') or []):
        status = c.get('Status') or c.get('status')
        states.append({
            'id': c.get('ID') or c.get('id') or c.get('Name') or c.get('name'),
            'name': c.get('Name') or c.get('name') or c.get('Names'),
            'service': c.get('Service') or c.get('service'),
            'state': c.get('State') or c.get('state'),
            'health': _health_from_status(status) or (c.get('Health') or c.get('health')),
        })
    return states


def _gather_app_container_states(app, index: Optional[_ContainerIndex] = None):
    """Collect per-container {name, service, state, health} dicts for an app.

    Local apps are answered from the shared host-wide snapshot (zero additional
    subprocesses); remote apps go through the agent. Wrapped in try/except so a
    Docker outage degrades to an empty list (→ 'unknown') rather than raising.
    """
    if getattr(app, 'server_id', None):
        return _gather_remote_app_container_states(app)

    if index is None:
        index = _get_container_index()

    try:
        rows = index.for_app(app)
    except Exception as e:
        logger.warning('Failed to resolve containers for app %s: %s',
                       getattr(app, 'id', '?'), e)
        return []

    if rows:
        return _states_from_index_rows(rows)

    # Nothing in the snapshot. For a compose app that is the honest answer
    # (`docker compose ps` would print nothing either). But an app pinned to a
    # bare container_id that the snapshot didn't carry — e.g. a stopped
    # container, which `docker ps` hides — still deserves its old inspect path.
    container_id = (getattr(app, 'container_id', None) or '').strip()
    if container_id and not getattr(app, 'root_path', None):
        return _gather_single_container_state(container_id)
    return []


def _gather_single_container_state(container_id: str) -> List[Dict[str, Any]]:
    """Fallback for one explicitly-identified container.

    ONE `docker inspect` — state and health both come out of the same payload,
    where the old code spent two spawns (``get_container_state`` and the health
    lookup each ran their own inspect of the same container).
    """
    from app.services.docker_service import DockerService
    try:
        info = DockerService.get_container(container_id)
    except Exception as e:
        logger.warning('Failed to read container %s state: %s', container_id, e)
        return []
    if not info:
        return []
    state = info.get('State') or {}
    return [{
        'id': container_id,
        'name': (info.get('Name') or container_id).lstrip('/'),
        'service': 'main',
        'state': state.get('Status'),
        'health': (state.get('Health') or {}).get('Status'),
    }]


def get_app_status(application_id, use_cache=True, index: Optional[_ContainerIndex] = None):
    """Aggregated status for a single Application.

    Loads the app, gathers its containers, aggregates, and caches the result for
    a short TTL. Fully defensive: a missing app or a Docker outage returns a
    well-formed 'unknown' result rather than raising.

    ``index`` lets a bulk caller (``list_app_statuses``) hand in the host-wide
    snapshot it already collected, so N apps cost one collection pass in total.

    Returns:
        dict: aggregate_status() output plus 'app_id' and 'kind'.
    """
    from app.services.cache_service import CacheService

    cache_key = f'{_CACHE_PREFIX}:app:{application_id}'
    if use_cache:
        cached = CacheService.get(cache_key)
        if cached is not None:
            return cached

    result = {
        'status': STATUS_UNKNOWN,
        'total': 0,
        'healthy': 0,
        'reasons': [],
        'containers': [],
        'app_id': application_id,
        'kind': 'app',
    }

    try:
        from app.models import Application
        app = Application.query_active().filter_by(id=application_id).first()
    except Exception as e:
        logger.warning('Failed to load application %s: %s', application_id, e)
        app = None

    if not app:
        result['reasons'] = ['application not found']
        return result

    # use_cache=False means "give me the truth right now" — it has to bypass the
    # shared snapshot too, or an 8s-stale answer would just come from 3s-stale
    # data instead of Docker.
    if index is None:
        index = _get_container_index(use_cache=use_cache)
    states = _gather_app_container_states(app, index=index)
    agg = aggregate_status(states)
    result.update(agg)
    result['app_id'] = application_id
    result['kind'] = 'app'

    if use_cache:
        try:
            CacheService.set(cache_key, result, ttl=_CACHE_TTL)
        except Exception:
            pass
    return result


def get_service_status(service_id, use_cache=True):
    """Aggregated status for a managed service.

    Services in this codebase are modeled as Applications (managed_by /
    compose), so this reuses the app path. Kept as a distinct entry point so
    callers/UI can speak in service terms and so the implementation can diverge
    later without changing the API surface.
    """
    result = get_app_status(service_id, use_cache=use_cache)
    result = dict(result)
    result['kind'] = 'service'
    result['service_id'] = service_id
    return result


def get_database_status(database_id, container_id=None, use_cache=True):
    """Aggregated status for a database.

    Databases don't share the Application container model uniformly. When a
    concrete ``container_id`` is supplied we aggregate it directly; otherwise we
    return a well-formed 'unknown' (best-effort, never raises).
    """
    result = {
        'status': STATUS_UNKNOWN,
        'total': 0,
        'healthy': 0,
        'reasons': ['database container not resolvable'],
        'containers': [],
        'database_id': database_id,
        'kind': 'database',
    }
    if not container_id:
        return result

    try:
        # One inspect, not two: state and health live in the same payload.
        states = _gather_single_container_state(container_id)
        if not states:
            # An unresolvable container still counts as one unknown member —
            # same output the two-inspect version produced on a failed lookup.
            states = [{'id': container_id, 'name': container_id,
                       'state': None, 'health': None}]
        agg = aggregate_status(states)
        result.update(agg)
        result['database_id'] = database_id
        result['kind'] = 'database'
    except Exception as e:
        logger.warning('Failed to resolve database %s status: %s', database_id, e)
    return result


def list_app_statuses():
    """Lightweight status summary for every application.

    Returns a list of {app_id, status, total, healthy} suitable for the list
    endpoint and the socket change-detection snapshot. Never raises.

    Cost: ONE `docker ps` for every local app on the host (shared with any other
    caller inside the snapshot TTL), plus one agent round-trip per *online*
    remote server's apps. The previous shape was one `docker compose ps` per app
    plus one `docker inspect` per running container.
    """
    summaries = []
    try:
        from app.models import Application
        # query_active: a tombstone has no containers left, so it would emit a
        # permanently-'unknown' row the socket snapshot could never clear.
        apps = Application.query_active().all()
    except Exception as e:
        logger.warning('Failed to list applications for status: %s', e)
        return summaries

    # Collect once, up front, and pass it down — the whole point of the round.
    index = _get_container_index()

    for app in apps:
        full = get_app_status(app.id, index=index)
        summaries.append({
            'app_id': app.id,
            'status': full.get('status', STATUS_UNKNOWN),
            'total': full.get('total', 0),
            'healthy': full.get('healthy', 0),
        })
    return summaries


# In-memory snapshot of the last emitted statuses, keyed by app_id → status
# string. Used by the socket emitter to emit only on change. Lives in this
# module (single-worker panel) so the emitter stays stateless.
_last_app_statuses = {}


def get_changed_app_statuses():
    """Return only the app statuses that changed since the last call.

    Compares the current per-app aggregated status against the in-memory
    snapshot and updates the snapshot. The socket emitter calls this on its
    interval and emits only the deltas (and drops vanished apps).

    Returns:
        list: changed {app_id, status, total, healthy} summaries.
    """
    changed = []
    current = list_app_statuses()
    seen = set()
    for summary in current:
        app_id = summary['app_id']
        seen.add(app_id)
        if _last_app_statuses.get(app_id) != summary['status']:
            _last_app_statuses[app_id] = summary['status']
            changed.append(summary)

    # Drop apps that disappeared so a re-created id re-emits.
    for gone in [aid for aid in _last_app_statuses if aid not in seen]:
        _last_app_statuses.pop(gone, None)

    return changed
