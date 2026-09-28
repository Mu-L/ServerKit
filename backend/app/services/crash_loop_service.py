"""Crash-loop visibility (plan 86 §E1).

A container with a restart policy that keeps dying looked "running" in the
panel: Docker restarts it each time, and nothing counted. A one-minute
builtin tick now reads each live app's containers' ``RestartCount`` and keeps
a short sample window per container. More than ``RESTARTS`` restarts within
``WINDOW`` marks the app crash-looping and opens an incident through the
monitor incident path; ``WINDOW`` without a restart resolves it.

State lives in one SystemSettings JSON row, keyed by app id, holding only
live apps and samples inside the window, so it stays bounded.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_KEY = 'crash_loop_state'
RESTARTS = 3
WINDOW = timedelta(minutes=10)


def _restart_counts(app) -> Dict[str, int]:
    """``{container name: RestartCount}`` for every container of ``app``."""
    from app.services.docker_service import DockerService
    names = [c.get('name') or c.get('id') for c in DockerService.get_all_app_containers(app)]
    if not names:
        names = [n for n in (getattr(app, 'container_id', None), app.name,
                             f'serverkit-app-{app.id}') if n]
    result = DockerService.run(['inspect', '--format', '{{.Name}} {{.RestartCount}}', *names],
                               timeout=30)
    counts = {}
    # inspect prints the ones it found even when another name is missing.
    for line in (result.get('output') or '').splitlines():
        parts = line.strip().lstrip('/').rsplit(' ', 1)
        if len(parts) == 2 and parts[1].isdigit():
            counts[parts[0]] = int(parts[1])
    return counts


class CrashLoopService:
    restart_counts = staticmethod(_restart_counts)

    @classmethod
    def sweep(cls, now: Optional[datetime] = None) -> Dict:
        from app import db
        from app.models.application import Application
        from app.models.system_settings import SystemSettings

        now = now or datetime.utcnow()
        cutoff = (now - WINDOW).isoformat()
        state = SystemSettings.get(STATE_KEY) or {}
        if not isinstance(state, dict):
            state = {}
        new_state, opened, resolved = {}, [], []

        for app in Application.query_active().filter(Application.app_type == 'docker').all():
            if app.status not in ('running', 'error'):
                continue
            entry = state.get(str(app.id)) or {}
            samples = {name: [s for s in rows if s[0] >= cutoff]
                       for name, rows in (entry.get('samples') or {}).items()}
            for name, count in cls.restart_counts(app).items():
                samples.setdefault(name, []).append([now.isoformat(), count])
            restarts = max((rows[-1][1] - rows[0][1] for rows in samples.values() if rows),
                           default=0)
            looping = restarts >= RESTARTS
            last_restart = entry.get('last_restart')
            if restarts > 0:
                changed = any(len(rows) > 1 and rows[-1][1] > rows[-2][1] for rows in samples.values())
                if changed:
                    last_restart = now.isoformat()

            incident_id = entry.get('incident_id')
            if looping and not incident_id:
                incident_id = cls._open_incident(app, restarts)
                opened.append(app.id)
            elif incident_id and not looping and (not last_restart or last_restart < cutoff):
                cls._resolve_incident(incident_id)
                resolved.append(app.id)
                incident_id = None

            new_state[str(app.id)] = {
                'samples': samples, 'restarts_in_window': restarts,
                'looping': bool(incident_id), 'incident_id': incident_id,
                'last_restart': last_restart,
            }

        if new_state != state:
            SystemSettings.set(STATE_KEY, new_state, value_type='json',
                               description='Crash-loop sample windows (plan 86 §E1)')
            db.session.commit()
        return {'apps': len(new_state), 'opened': opened, 'resolved': resolved}

    @staticmethod
    def _open_incident(app, restarts) -> Optional[int]:
        from app.services.monitor_service import MonitorService
        try:
            incident = MonitorService.create_incident(None, {
                'title': f'{app.name} is crash-looping',
                'status': 'investigating',
                'impact': 'major',
                'body': (f'Its containers restarted {restarts} times within '
                         f'{int(WINDOW.total_seconds() // 60)} minutes. Check the app logs; '
                         'Docker keeps restarting it, so it looks running between crashes.'),
            })
            return incident.id
        except Exception as exc:  # noqa: BLE001 - visibility must not break the sweep
            logger.warning('crash-loop incident for app %s failed: %s', app.id, exc)
            return None

    @staticmethod
    def _resolve_incident(incident_id) -> None:
        from app.services.monitor_service import MonitorService
        try:
            MonitorService.update_incident(incident_id, {
                'status': 'resolved',
                'update_body': f'No restarts for {int(WINDOW.total_seconds() // 60)} minutes.',
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning('crash-loop incident %s resolve failed: %s', incident_id, exc)

    @staticmethod
    def state_for(app) -> Dict:
        from app.models.system_settings import SystemSettings
        state = SystemSettings.get(STATE_KEY) or {}
        return (state.get(str(app.id)) or {}) if isinstance(state, dict) else {}
