"""Name an app's likely bottleneck from what the panel measures (plan 86 §A5).

A rule-based doctor, no AI: it reads the app's request metrics (§A2), its
container's CPU, and, for a PostgreSQL / MySQL it reaches through
``fromService``, that database's CPU and top queries (§A3). Each hint names
the component that fits the signal, links to where it is set up, and states
that component's failure mode. Nothing is ever added automatically.

Each hint carries ``id`` + ``params`` (the numbers) for a translated UI, and
English ``signal`` / ``hint`` / ``failure_mode`` text for API and CLI callers.

Rules (all over the last 24 h):

  slow + app busy + database idle  → page cache (micro-cache) or a bigger box
  slow + database busy + one query dominates → a cache, or an index
  5xx inside a deploy window        → A/B slot deploys (plan 87)
  micro-cache on, low hit ratio     → the pages vary too much, or the TTL is short

Not covered, deliberately: "large static share of bytes" and "slow POST
endpoints" need per-path and per-method data that the rollups do not keep.
"""
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

SLOW_P95_MS = 1000
BUSY_CPU_PERCENT = 80
IDLE_CPU_PERCENT = 20
DOMINANT_QUERY_SHARE = 0.5
MIN_REQUESTS = 100
LOW_HIT_RATIO = 0.3
DEPLOY_WINDOW = timedelta(minutes=5)


def _cpu_percent(container: str) -> Optional[float]:
    from app.services.docker_service import DockerService
    stats = DockerService.get_container_stats(container) or {}
    raw = str(stats.get('CPUPerc') or '').rstrip('%')
    try:
        return float(raw)
    except ValueError:
        return None


class BottleneckHintsService:
    # Seams (tests replace these).
    cpu_percent: Callable[[str], Optional[float]] = staticmethod(_cpu_percent)

    @staticmethod
    def _metrics(app) -> Dict:
        from app.services.request_metrics_service import RequestMetricsService
        return RequestMetricsService.series(app.id, '24h')

    @staticmethod
    def _databases(app) -> List:
        """SQL engines this app reaches through fromService."""
        from app.models import EnvironmentVariable
        from app.services.env_reference_service import EnvReferenceResolver
        from app.services.service_connection_service import _template_for
        from app.services.template_service import TemplateService

        found = {}
        for ev in EnvironmentVariable.query.filter_by(application_id=app.id).all():
            ref = ev.get_reference() if ev.value_from else None
            if not ref or ref.get('kind') != 'service':
                continue
            sibling = EnvReferenceResolver.find_sibling_app(app, ref.get('service'))
            if sibling is None:
                continue
            engine = TemplateService.engine_metadata(_template_for(sibling) or {}) or {}
            if engine.get('protocol') in ('postgresql', 'mysql'):
                found[sibling.id] = (sibling, engine)
        return list(found.values())

    @staticmethod
    def _top_query_share(db_app, engine) -> Optional[float]:
        from app.services.db_insights_service import DbInsightsService
        from app.services.service_connection_service import ServiceConnectionService
        props = (ServiceConnectionService.spec(db_app) or {}).get('properties', {})
        target = {'engine': engine['protocol'], 'container': db_app.name,
                  'user': props.get('username') or engine.get('admin_user'),
                  'password': props.get('password'),
                  'database': props.get('database') or None}
        insights = DbInsightsService.insights(target)
        queries = insights.get('top_queries') or []
        total = sum(q.get('total_ms') or 0 for q in queries)
        return (queries[0].get('total_ms') or 0) / total if total else None

    @staticmethod
    def _deploy_windows(app, since) -> List:
        from app.models.deployment import Deployment
        rows = Deployment.query.filter(Deployment.app_id == app.id,
                                       Deployment.created_at >= since).all()
        return [(d.deploy_started_at or d.created_at,
                 (d.deploy_completed_at or d.deploy_started_at or d.created_at) + DEPLOY_WINDOW)
                for d in rows]

    @classmethod
    def _app_cpu(cls, app) -> Optional[float]:
        """CPU of the app's container: the recorded id, then a template's
        ``container_name: ${APP_NAME}``, then a build-pack deploy's name."""
        for name in (getattr(app, 'container_id', None), app.name, f'serverkit-app-{app.id}'):
            if name:
                cpu = cls.cpu_percent(name)
                if cpu is not None:
                    return cpu
        return None

    # ------------------------------------------------------------------

    @classmethod
    def hints(cls, app, now: Optional[datetime] = None) -> List[Dict]:
        now = now or datetime.utcnow()
        metrics = cls._metrics(app)
        summary = metrics.get('summary') or {}
        hints: List[Dict] = []
        if (summary.get('requests') or 0) < MIN_REQUESTS:
            return hints

        p95 = summary.get('p95_ms')
        if p95 is not None and p95 >= SLOW_P95_MS:
            app_cpu = cls._app_cpu(app)
            databases = cls._databases(app)
            db_cpu = {db_app.name: cls.cpu_percent(db_app.name) for db_app, _ in databases}
            busiest_db = max((v for v in db_cpu.values() if v is not None), default=None)

            if app_cpu is not None and app_cpu >= BUSY_CPU_PERCENT and (
                    busiest_db is None or busiest_db <= IDLE_CPU_PERCENT):
                hints.append({
                    'id': 'app_bound',
                    'params': {'p95_ms': round(p95), 'cpu': round(app_cpu)},
                    'signal': f'p95 {p95:.0f} ms while the app uses {app_cpu:.0f}% CPU '
                              'and its database is idle',
                    'hint': 'The app itself is the bottleneck. Cache whole pages in front '
                            'of it, or give it a bigger box. More copies of the app are '
                            'not offered: one live copy per app.',
                    'action': {'label': 'Micro-cache', 'target': 'settings/cache'},
                    'failure_mode': 'A cached page can be stale for up to its lifetime.',
                })
            for db_app, engine in databases:
                cpu = db_cpu.get(db_app.name)
                if cpu is None or cpu < BUSY_CPU_PERCENT:
                    continue
                share = cls._top_query_share(db_app, engine)
                repeated = share is not None and share >= DOMINANT_QUERY_SHARE
                hints.append({
                    'id': 'database_bound',
                    'params': {'p95_ms': round(p95), 'cpu': round(cpu), 'database': db_app.name,
                               'share': round(share * 100) if repeated else None},
                    'signal': (f'p95 {p95:.0f} ms while {db_app.name} uses {cpu:.0f}% CPU'
                               + (f'; one query takes {share:.0%} of its time' if repeated else '')),
                    'hint': ('The database is the bottleneck. '
                             + ('The same query dominates: cache its result, or add an '
                                'index for it (see the query in Insights).' if repeated else
                                'Look at its top queries for a missing index, or cache '
                                'what is read most.')),
                    'action': {'label': 'Attach cache', 'target': 'overview#attachments'},
                    'failure_mode': 'Cached data can be stale; invalidate on write.',
                })

        spikes = [p for p in metrics.get('points') or [] if (p.get('status_5xx') or 0) > 0]
        if spikes:
            windows = cls._deploy_windows(app, now - timedelta(hours=24))
            during = [p for p in spikes if any(
                start <= datetime.fromisoformat(p['t'].replace('Z', '+00:00')).replace(tzinfo=None) <= end
                for start, end in windows)]
            if during:
                errors = sum(p['status_5xx'] for p in during)
                hints.append({
                    'id': 'deploy_errors',
                    'params': {'errors': errors},
                    'signal': f'{errors} server errors during deploys in the last 24 h',
                    'hint': 'Visitors hit the gap while the old container stops and the '
                            'new one starts. A/B slot deploys (plan 87) keep the old copy '
                            'serving until the new one passes its health check.',
                    'action': None,
                    'failure_mode': 'Two copies run briefly during the switch; a migration '
                                    'must work with both.',
                })

        ratio = summary.get('cache_hit_ratio')
        if getattr(app, 'micro_cache_enabled', False) and ratio is not None and ratio < LOW_HIT_RATIO:
            hints.append({
                'id': 'low_hit_ratio',
                'params': {'ratio': round(ratio * 100)},
                'signal': f'micro-cache hit ratio {ratio:.0%}',
                'hint': 'Most cacheable requests miss. The pages may vary per visitor '
                        '(cookies, query strings bypass the cache), or the lifetime is '
                        'too short for the traffic.',
                'action': {'label': 'Micro-cache', 'target': 'settings/cache'},
                'failure_mode': 'A longer lifetime serves older pages.',
            })
        return hints
