"""Plan 85 A4: history tables that nothing pruned are aged out, finished rows
only, and the rows the UI reads as "current" survive."""
from datetime import datetime, timedelta

from sqlalchemy import text

from app import db
from app.services import history_retention_service as svc
from app.services.settings_service import SettingsService

NOW = datetime.utcnow()
OLD = NOW - timedelta(days=120)
NEW = NOW - timedelta(days=5)


def _sql(statement, **params):
    return db.session.execute(text(statement), params)


def _count(table, where='1=1', **params):
    return _sql(f'SELECT COUNT(*) FROM {table} WHERE {where}', **params).scalar()


def _app_id(name):
    from tests.factories import make_application
    return make_application(db, name=name).id


def test_every_spec_runs_against_the_real_schema(app):
    # A typo in a table or column name would only surface as a warning on a
    # live box; on an empty database every spec must simply delete nothing.
    tables = {spec[0] for spec in svc.SPECS}
    from sqlalchemy import inspect
    assert tables <= set(inspect(db.engine).get_table_names())
    assert svc.prune() == {}


def test_old_audit_rows_go_and_recent_ones_stay(app):
    _sql("INSERT INTO audit_logs (action, created_at) VALUES ('old', :t)", t=OLD)
    _sql("INSERT INTO audit_logs (action, created_at) VALUES ('new', :t)", t=NEW)
    db.session.commit()

    assert svc.prune(now=NOW) == {'audit_logs': 1}
    assert [r[0] for r in _sql('SELECT action FROM audit_logs')] == ['new']


def test_zero_keeps_everything(app):
    _sql("INSERT INTO audit_logs (action, created_at) VALUES ('old', :t)", t=OLD)
    db.session.commit()
    SettingsService.set(svc.AUDIT_SETTING, 0)

    assert svc.prune(now=NOW) == {}
    assert _count('audit_logs') == 1


def test_a_notification_goes_only_after_its_deliveries(app):
    for key, created in (('old', OLD), ('new', NEW)):
        _sql("INSERT INTO notifications (event_key, title, created_at) VALUES (:k, :k, :t)",
             k=key, t=created)
    old_id = _sql("SELECT id FROM notifications WHERE event_key = 'old'").scalar()
    new_id = _sql("SELECT id FROM notifications WHERE event_key = 'new'").scalar()
    # The old notification still has a recent delivery: it must stay.
    _sql("INSERT INTO notification_deliveries (notification_id, channel, created_at) "
         "VALUES (:n, 'email', :t)", n=old_id, t=NEW)
    _sql("INSERT INTO notification_deliveries (notification_id, channel, created_at) "
         "VALUES (:n, 'email', :t)", n=new_id, t=OLD)
    db.session.commit()

    svc.prune(now=NOW)
    assert _count('notifications') == 2
    assert _count('notification_deliveries') == 1


def test_deployment_jobs_keep_the_newest_per_app_and_anything_live(app):
    app_id = _app_id('retention-app')
    rows = (
        ('ancient-done', 'succeeded', OLD - timedelta(days=10)),
        ('old-running', 'running', OLD - timedelta(days=5)),
        ('newest-but-old', 'failed', OLD),
    )
    for job_id, status, created in rows:
        _sql("INSERT INTO deployment_jobs (id, kind, status, app_id, created_at) "
             "VALUES (:i, 'deploy', :s, :a, :t)", i=job_id, s=status, a=app_id, t=created)
        _sql("INSERT INTO deployment_job_logs (job_id, message, created_at) "
             "VALUES (:i, 'line', :t)", i=job_id, t=NEW)   # lines newer than the job
    db.session.commit()

    svc.prune(now=NOW)
    left = {r[0] for r in _sql('SELECT id FROM deployment_jobs')}
    assert left == {'old-running', 'newest-but-old'}
    assert _count('deployment_job_logs', "job_id = 'ancient-done'") == 0
    assert _count('deployment_job_logs') == 2


def test_a_running_cron_run_is_never_pruned(app):
    _sql("INSERT INTO cron_runs (job_id, status, created_at) VALUES ('a', 'running', :t)", t=OLD)
    _sql("INSERT INTO cron_runs (job_id, status, created_at) VALUES ('b', 'success', :t)", t=OLD)
    db.session.commit()

    svc.prune(now=NOW)
    assert [r[0] for r in _sql('SELECT job_id FROM cron_runs')] == ['a']


def test_the_latest_image_check_per_app_survives(app):
    app_id = _app_id('image-app')
    for checked in (OLD - timedelta(days=1), OLD):
        _sql("INSERT INTO image_update_checks (application_id, image_ref, update_available, checked_at) "
             "VALUES (:a, 'nginx:latest', 0, :t)", a=app_id, t=checked)
    db.session.commit()

    svc.prune(now=NOW)
    assert _count('image_update_checks') == 1
    assert _sql('SELECT checked_at FROM image_update_checks').scalar().startswith(str(OLD.date()))


def test_the_retention_tick_runs_history_even_with_telemetry_off(app):
    from app.jobs.builtin_handlers import run_telemetry_retention

    _sql("INSERT INTO audit_logs (action, created_at) VALUES ('old', :t)", t=OLD)
    db.session.commit()
    SettingsService.set('telemetry.retention_days', 0)

    result = run_telemetry_retention()
    assert result == {'deleted': 1, 'by_table': {'audit_logs': 1}}
