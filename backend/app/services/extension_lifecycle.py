"""Extension runtime lifecycle: data models + declarative jobs.

Beyond hot-loading a blueprint, an extension can own database tables and
background jobs declared in its manifest. This module creates/tears those down in
lockstep with install / uninstall / enable / disable so a plugin no longer just
gets raw ``db`` and prays, and a disabled plugin's scheduled work actually pauses
(status-guard parity for background jobs).

Manifest surface:

    "models": "models:register",        # app.plugins.<slug>.models:register(db)
    "jobs": [                            # background job handlers
        {"kind": "myext.reindex", "handler": "jobs:reindex"}
    ],
    "schedules": [                       # recurring jobs
        {"name": "myext-nightly", "kind": "myext.reindex", "cron": "0 3 * * *"}
    ]

Data-model tables SHOULD be named ``ext_<slug>_*`` (dashes → underscores); purge
on uninstall drops exactly those.
"""
import importlib
import logging

logger = logging.getLogger(__name__)


def _table_prefix(slug):
    return f"ext_{slug.replace('-', '_')}_"


def _import_target(slug, target):
    """Resolve a ``module:func`` target under app.plugins.<slug>."""
    if not target or ':' not in str(target):
        return None
    module_name, func_name = str(target).split(':', 1)
    mod = importlib.import_module(f'app.plugins.{slug}.{module_name}')
    return getattr(mod, func_name, None)


# --------------------------------------------------------------------------- #
# Data models (#24)
# --------------------------------------------------------------------------- #

def register_models(plugin, manifest):
    """Import the plugin's model module (registering its SQLAlchemy tables on the
    metadata as a side effect) and create any missing tables. Best-effort."""
    target = (manifest or {}).get('models')
    if not target:
        return
    from app import db
    try:
        fn = _import_target(plugin.slug, target)
        if callable(fn):
            fn(db)
    except Exception as e:
        logger.warning(f'Model registration failed for {plugin.slug}: {e}')
        return
    try:
        db.create_all()
        logger.info(f'Ensured data-model tables for extension {plugin.slug}')
    except Exception as e:
        logger.warning(f'create_all after {plugin.slug} model register failed: {e}')


def purge_models(plugin):
    """Drop the plugin's ``ext_<slug>_*`` tables. Called on uninstall --purge."""
    from app import db
    from sqlalchemy import inspect, text
    prefix = _table_prefix(plugin.slug)
    try:
        tables = inspect(db.engine).get_table_names()
    except Exception as e:
        logger.warning(f'Could not inspect tables to purge {plugin.slug}: {e}')
        return 0
    dropped = 0
    for table in tables:
        if table.startswith(prefix):
            try:
                db.session.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
                dropped += 1
            except Exception as e:
                logger.warning(f'Failed dropping {table}: {e}')
    if dropped:
        db.session.commit()
        logger.info(f'Purged {dropped} table(s) for extension {plugin.slug}')
    return dropped


# --------------------------------------------------------------------------- #
# Declarative jobs (#29)
# --------------------------------------------------------------------------- #

def _schedule_names(manifest):
    return [s.get('name') for s in (manifest or {}).get('schedules') or []
            if isinstance(s, dict) and s.get('name')]


def register_jobs(plugin, manifest):
    """Register the plugin's job handlers and (enabled) schedules."""
    from app.jobs.sdk import JobsSdk
    sdk = JobsSdk()

    for job in (manifest or {}).get('jobs') or []:
        if not isinstance(job, dict):
            continue
        kind = job.get('kind')
        handler_ref = job.get('handler')
        if not kind or not handler_ref:
            continue
        try:
            handler = _import_target(plugin.slug, handler_ref)
            if callable(handler):
                sdk.register(kind, handler, replace=True)
        except Exception as e:
            logger.warning(f'Job handler {handler_ref} for {plugin.slug} failed: {e}')

    for sched in (manifest or {}).get('schedules') or []:
        if not isinstance(sched, dict) or not sched.get('name') or not sched.get('kind'):
            continue
        try:
            sdk.schedule(
                sched['name'], sched['kind'],
                interval_seconds=sched.get('interval_seconds'),
                cron=sched.get('cron'),
                payload=sched.get('payload'),
            )
        except Exception as e:
            logger.warning(f"Schedule {sched.get('name')} for {plugin.slug} failed: {e}")


def _set_schedules_enabled(manifest, enabled):
    from app.jobs.service import ScheduledJobService
    from app.jobs.models import ScheduledJob
    names = set(_schedule_names(manifest))
    if not names:
        return
    for row in ScheduledJob.query.filter(ScheduledJob.name.in_(names)).all():
        try:
            ScheduledJobService.set_enabled(row.id, enabled)
        except Exception as e:
            logger.warning(f'Could not toggle schedule {row.name}: {e}')


def pause_jobs(plugin, manifest):
    """Disable a plugin's scheduled jobs (called on plugin disable)."""
    _set_schedules_enabled(manifest, False)


def resume_jobs(plugin, manifest):
    """Re-enable a plugin's scheduled jobs (called on plugin enable)."""
    _set_schedules_enabled(manifest, True)


def remove_jobs(plugin, manifest):
    """Delete a plugin's scheduled-job rows (called on uninstall)."""
    from app import db
    from app.jobs.models import ScheduledJob
    names = set(_schedule_names(manifest))
    if not names:
        return
    try:
        ScheduledJob.query.filter(ScheduledJob.name.in_(names)).delete(
            synchronize_session=False)
        db.session.commit()
    except Exception as e:
        logger.warning(f'Could not remove schedules for {plugin.slug}: {e}')


# --------------------------------------------------------------------------- #
# Core hooks (plan 52 D4 — hook inversion)
# --------------------------------------------------------------------------- #

def register_capabilities(plugin, manifest):
    """Invoke the extension's core-hook registration function.

    Manifest surface::

        "core_hooks": "core_hooks:register"     # zero-arg function under
                                                # app.plugins.<slug>

    The function registers the extension's entries into core-owned
    registration seams (backup target kinds, event-catalog types, template
    providers, …) so core keeps each engine while the extension supplies the
    entry — an absent extension means the feature is absent, gracefully.

    Called at install AND on every boot for active plugins (right beside
    register_models/register_jobs), so the function MUST be idempotent.
    Because it runs per boot rather than per module import, a seam registry
    cleared since the last boot is refilled. Best-effort: a failure logs and
    never blocks the load.
    """
    target = (manifest or {}).get('core_hooks')
    if not target:
        return
    try:
        fn = _import_target(plugin.slug, target)
        if callable(fn):
            fn()
    except Exception as e:
        logger.warning(f'Core-hook registration failed for {plugin.slug}: {e}')


def unregister_capabilities(plugin):
    """Drop a plugin's core-seam registrations (disable/uninstall, audit F1).

    The mirrors of :func:`register_capabilities`: every seam registry tracks
    its registrant slug (backup kinds, event types, template providers), so
    teardown removes exactly THIS plugin's entries. The extension's modules
    stay in sys.modules (same as its blueprints) — but nothing may keep
    EXECUTING through a core seam once the extension is disabled or
    uninstalled. Best-effort; never raises into the caller.
    """
    slug = plugin.slug
    try:
        from app.services import backup_kind_registry
        backup_kind_registry.unregister(slug)
    except Exception as e:
        logger.warning(f'Backup-kind teardown failed for {slug}: {e}')
    try:
        from app.services import event_service
        event_service.unregister_event_types(slug)
        event_service.unregister_listeners(slug)
    except Exception as e:
        logger.warning(f'Event-type teardown failed for {slug}: {e}')
    try:
        from app.services.template_service import TemplateService
        TemplateService.unregister_template_provider(slug)
    except Exception as e:
        logger.warning(f'Template-provider teardown failed for {slug}: {e}')
