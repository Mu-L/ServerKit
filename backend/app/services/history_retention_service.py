"""Age out history tables that nothing else prunes (plan 85 A4).

Every table here records something that already happened — an audit entry,
a delivered notification, a finished cron run — and grew forever: no handler
deleted from any of them. They are pruned by age from the same 6-hour tick
as telemetry (``builtin.telemetry_retention``).

Rules each spec encodes:

- Only finished rows go: a running cron run, a pending delivery or a live
  deployment job is never touched, whatever its age.
- The newest row of a series that the UI reads as "current" is kept (an
  app's latest deployment job, its latest image update check).
- Children before parents, and a parent only once it has no children left.

``audit_log_retention_days`` (default 90) covers the audit log; everything
else follows ``history.retention_days`` (default 90). ``0`` disables either.
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

AUDIT_SETTING = 'audit_log_retention_days'
HISTORY_SETTING = 'history.retention_days'
DEFAULT_DAYS = 90

_DEPLOY_DONE = "('succeeded', 'failed', 'cancelled', 'rolled_back')"
# A deployment job is kept while it is its app's newest one — that row is the
# app's "last deploy" — and while it still has log lines (they go first).
_DEPLOY_JOB_PRUNABLE = (
    f"status IN {_DEPLOY_DONE} AND created_at < "
    "(SELECT MAX(d2.created_at) FROM deployment_jobs d2 "
    "WHERE d2.app_id = deployment_jobs.app_id)"
)

# (table, time column or None, extra WHERE clause or '', setting key), in
# deletion order.
SPECS = (
    ('audit_logs', 'created_at', '', AUDIT_SETTING),
    ('error_logs', 'last_seen', '', HISTORY_SETTING),
    ('notification_deliveries', 'created_at', '', HISTORY_SETTING),
    ('notifications', 'created_at',
     'NOT EXISTS (SELECT 1 FROM notification_deliveries nd '
     'WHERE nd.notification_id = notifications.id)', HISTORY_SETTING),
    # Lines follow their job, not their own age: a long job's last lines can
    # be newer than the cutoff, and would then pin the job forever.
    ('deployment_job_logs', None,
     'job_id IN (SELECT id FROM deployment_jobs WHERE '
     + _DEPLOY_JOB_PRUNABLE + ' AND created_at < :cutoff)', HISTORY_SETTING),
    ('deployment_jobs', 'created_at',
     _DEPLOY_JOB_PRUNABLE + ' AND NOT EXISTS (SELECT 1 FROM deployment_job_logs dl '
     'WHERE dl.job_id = deployment_jobs.id)', HISTORY_SETTING),
    ('cron_runs', 'created_at', "status <> 'running'", HISTORY_SETTING),
    ('webhook_logs', 'received_at', '', HISTORY_SETTING),
    ('webhook_deliveries', 'received_at',
     "status IN ('filtered', 'forwarded', 'failed')", HISTORY_SETTING),
    ('event_deliveries', 'created_at', "status IN ('success', 'failed')", HISTORY_SETTING),
    ('image_update_checks', 'checked_at',
     'checked_at < (SELECT MAX(c2.checked_at) FROM image_update_checks c2 '
     'WHERE c2.application_id = image_update_checks.application_id)', HISTORY_SETTING),
    ('workflow_logs', 'timestamp', '', HISTORY_SETTING),
    ('server_onboarding_logs', 'created_at', '', HISTORY_SETTING),
    ('sandbox_runs', 'created_at', "status <> 'running'", HISTORY_SETTING),
)


def _days(key):
    from app.services.settings_service import SettingsService
    try:
        return int(SettingsService.get(key, DEFAULT_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_DAYS


def _delete(table, clause, params, sqlite, batch_size):
    from sqlalchemy import text
    from app import db
    if not sqlite:
        # Other engines have no portable rowid; one statement, one commit.
        result = db.session.execute(text(f'DELETE FROM {table} WHERE {clause}'), params)
        db.session.commit()
        return result.rowcount or 0
    removed = 0
    while True:
        # Batch on rowid so a large backlog never locks the table in one go.
        result = db.session.execute(text(
            f'DELETE FROM {table} WHERE rowid IN '
            f'(SELECT rowid FROM {table} WHERE {clause} LIMIT {int(batch_size)})'
        ), params)
        db.session.commit()
        if not result.rowcount:
            break
        removed += result.rowcount
        if result.rowcount < batch_size:
            break
    return removed


def prune(now=None, batch_size=5000):
    """Delete aged history rows. Returns ``{table: rows_deleted}`` for every
    table that lost rows. A table missing from this database is skipped."""
    from sqlalchemy import inspect
    from app import db

    now = now or datetime.utcnow()
    present = set(inspect(db.engine).get_table_names())
    sqlite = db.engine.dialect.name == 'sqlite'
    days = {key: _days(key) for key in (AUDIT_SETTING, HISTORY_SETTING)}

    deleted = {}
    for table, time_col, extra, key in SPECS:
        if table not in present or days[key] <= 0:
            continue
        cutoff = now - timedelta(days=days[key])
        clause = ' AND '.join(part for part in (
            f'{time_col} < :cutoff' if time_col else '', extra) if part)
        try:
            removed = _delete(table, clause, {'cutoff': cutoff}, sqlite, batch_size)
        except Exception as exc:
            # One table's schema drift must not stop the rest from pruning.
            db.session.rollback()
            logger.warning('History retention skipped %s: %s', table, exc)
            continue
        if removed:
            deleted[table] = removed
    return deleted
