"""Tests for the unified job system (app/jobs)."""
import pytest

from app import db
from app.jobs import registry
from app.jobs.models import Job, ScheduledJob
from app.jobs.service import JobService, ScheduledJobService, GROUP_SLUG, QUEUE_SLUG
from app.jobs.consumer import JobConsumer
from app.jobs.scheduler import JobScheduler
from app.queue_bus.service import QueueBusService
from app.queue_bus.models import QueueMessage


@pytest.fixture(autouse=True)
def reset_jobs(app):
    """Clean broker + handler registry before each test (after create_app has
    registered the built-in handlers)."""
    QueueBusService.reset_broker()
    registry.clear()
    yield
    registry.clear()


def _drain_once(consumer=None):
    """Receive and process exactly one job message; return it processed."""
    consumer = consumer or JobConsumer()
    messages = QueueBusService.receive(GROUP_SLUG, QUEUE_SLUG, max_messages=1)
    assert messages, 'expected a queued job message'
    consumer.process_message(messages[0])
    return messages[0]


class TestEnqueueAndRun:
    def test_enqueue_creates_job_and_message(self, app):
        job = JobService.enqueue('test.noop', {'x': 1})
        assert job.id is not None
        assert job.status == Job.STATUS_PENDING
        assert Job.query.count() == 1
        # A thin {job_id} pointer is on the bus.
        msgs = QueueBusService.list_messages(GROUP_SLUG, QUEUE_SLUG)
        assert len(msgs) == 1
        assert msgs[0]['payload'] == {'job_id': job.id}

    def test_handler_runs_and_succeeds(self, app):
        seen = {}

        @registry.handler('test.ok')
        def _ok(job):
            seen['payload'] = job.get_payload()
            return {'done': True}

        job = JobService.enqueue('test.ok', {'x': 42})
        _drain_once()

        refreshed = Job.query.get(job.id)
        assert refreshed.status == Job.STATUS_SUCCEEDED
        assert refreshed.get_result() == {'done': True}
        assert refreshed.completed_at is not None
        assert seen['payload'] == {'x': 42}

    def test_unregistered_kind_fails_without_retrying(self, app):
        job = JobService.enqueue('test.no_handler', {}, max_attempts=3)
        msg = _drain_once()

        refreshed = Job.query.get(job.id)
        assert refreshed.status == Job.STATUS_FAILED
        assert 'No handler' in (refreshed.error_message or '')
        # Message completed (not left to retry an unroutable job).
        qm = QueueBusService.get_message(GROUP_SLUG, QUEUE_SLUG, msg['id'])
        assert qm['status'] == QueueMessage.STATUS_COMPLETED

    def test_failure_retries_then_dead_letters_to_failed(self, app):
        @registry.handler('test.boom')
        def _boom(job):
            raise RuntimeError('kaboom')

        # max_attempts=1 → the first failure exhausts attempts → dead-letter.
        job = JobService.enqueue('test.boom', {}, max_attempts=1)
        msg = _drain_once()

        refreshed = Job.query.get(job.id)
        assert refreshed.status == Job.STATUS_FAILED
        assert 'kaboom' in (refreshed.error_message or '')
        qm = QueueBusService.get_message(GROUP_SLUG, QUEUE_SLUG, msg['id'])
        assert qm['status'] == QueueMessage.STATUS_DEAD_LETTER

    def test_failure_with_attempts_left_stays_pending(self, app):
        @registry.handler('test.flaky')
        def _flaky(job):
            raise RuntimeError('again')

        job = JobService.enqueue('test.flaky', {}, max_attempts=3)
        _drain_once()

        refreshed = Job.query.get(job.id)
        # Not terminal yet — the queue will redeliver after backoff.
        assert refreshed.status == Job.STATUS_PENDING
        assert refreshed.status not in Job.TERMINAL_STATUSES or refreshed.status == Job.STATUS_PENDING


class TestCancelAndRetry:
    def test_cancel_prevents_execution(self, app):
        ran = {'count': 0}

        @registry.handler('test.cancelme')
        def _h(job):
            ran['count'] += 1

        job = JobService.enqueue('test.cancelme', {})
        JobService.cancel(job.id)
        assert Job.query.get(job.id).status == Job.STATUS_CANCELLED

        _drain_once()  # consumer should skip the cancelled job
        assert ran['count'] == 0
        assert Job.query.get(job.id).status == Job.STATUS_CANCELLED

    def test_retry_requeues_failed_job(self, app):
        job = JobService.enqueue('test.no_handler', {}, max_attempts=1)
        _drain_once()
        assert Job.query.get(job.id).status == Job.STATUS_FAILED

        # Now register a handler and retry.
        @registry.handler('test.no_handler')
        def _now_ok(job):
            return 'recovered'

        JobService.retry(job.id)
        assert Job.query.get(job.id).status == Job.STATUS_PENDING
        _drain_once()
        assert Job.query.get(job.id).status == Job.STATUS_SUCCEEDED
        assert Job.query.get(job.id).get_result() == 'recovered'


class TestScheduler:
    def test_ensure_is_idempotent(self, app):
        a = ScheduledJobService.ensure('nightly', 'test.x', interval_seconds=60)
        b = ScheduledJobService.ensure('nightly', 'test.x', interval_seconds=120)
        assert a.id == b.id
        assert ScheduledJob.query.count() == 1
        assert ScheduledJob.query.get(a.id).interval_seconds == 120

    def test_tick_fires_due_schedule_and_advances(self, app):
        # startup_delay 0 → next_run_at = now → immediately due.
        scheduled = ScheduledJobService.ensure(
            'due-now', 'test.tick', interval_seconds=3600, startup_delay_seconds=0)
        before = scheduled.next_run_at

        fired = JobScheduler().tick()
        assert fired == 1

        # A job was enqueued for the schedule, and next_run advanced past now.
        job = Job.query.filter_by(kind='test.tick').first()
        assert job is not None
        assert job.owner_type == 'schedule'
        assert job.owner_id == 'due-now'
        refreshed = ScheduledJob.query.get(scheduled.id)
        assert refreshed.next_run_at > before
        assert refreshed.last_job_id == job.id
        # No longer due.
        assert JobScheduler().tick() == 0

    def test_disabled_schedule_does_not_fire(self, app):
        scheduled = ScheduledJobService.ensure(
            'off', 'test.tick', interval_seconds=60, startup_delay_seconds=0)
        ScheduledJobService.set_enabled(scheduled.id, False)
        assert JobScheduler().tick() == 0


class TestBuiltins:
    def test_register_and_seed(self, app):
        from app.jobs import builtin_handlers
        builtin_handlers.register_builtin_handlers()
        kinds = registry.registered_kinds()
        assert 'builtin.auto_sync' in kinds
        assert 'builtin.health_check' in kinds
        assert 'builtin.backup_scheduler' in kinds
        assert 'builtin.extension_updates' in kinds
        assert 'builtin.security_feed' in kinds
        assert 'builtin.job_retention' in kinds
        assert 'builtin.monitor_check' in kinds
        assert 'builtin.restore_point_retention' in kinds
        # Reclaims the disk a soft delete no longer frees: an Application
        # tombstone holds its data volumes and source tree until purge, and
        # nothing else calls purge_expired on a schedule.
        assert 'builtin.recycle_retention' in kinds
        # Bounds the telemetry stream. queue_messages/system_events grow a row
        # per scheduler tick and api_usage_logs one per request; unpruned that
        # is ~11 MB/day of database forever, which is what filled a 25 GB host
        # from routine updates alone (each update copies the DB twice).
        assert 'builtin.telemetry_retention' in kinds
        # Fleet alert thresholds. The evaluation existed and had no caller, so
        # thresholds set on the Fleet page were never checked against anything.
        assert 'builtin.fleet_thresholds' in kinds
        assert len([k for k in kinds if k.startswith('builtin.')]) == 17

        builtin_handlers.seed_builtin_schedules()
        # 17 builtin.* schedules (incl. restore-point/job/telemetry retention,
        # the monitor sweep, security-feed check, recycle-bin retention and the
        # fleet threshold check) + login-link/SSO reapers + drift/FIM/bandwidth
        # sweeps + the host doctor sweep AND the fleet doctor sweep (plan 26)
        # + the setup-health nag (plan 22).
        assert ScheduledJob.query.count() == 25
        # Seeding twice doesn't duplicate.
        builtin_handlers.seed_builtin_schedules()
        assert ScheduledJob.query.count() == 25


class TestApi:
    def test_list_and_get_via_api(self, client, auth_headers, app):
        job = JobService.enqueue('test.api', {'hello': 'world'})

        resp = client.get('/api/v1/jobs', headers=auth_headers)
        assert resp.status_code == 200
        listed = next(j for j in resp.get_json()['jobs'] if j['id'] == job.id)
        assert listed['can_cancel'] is True
        assert listed['can_retry'] is False

        resp = client.get(f'/api/v1/jobs/{job.id}', headers=auth_headers)
        assert resp.status_code == 200
        detail = resp.get_json()['job']
        assert detail['payload'] == {'hello': 'world'}
        assert detail['can_cancel'] is True
        assert detail['can_retry'] is False

        job.status = Job.STATUS_FAILED
        db.session.commit()
        failed = client.get(f'/api/v1/jobs/{job.id}', headers=auth_headers).get_json()['job']
        assert failed['can_cancel'] is False
        assert failed['can_retry'] is True

    def test_stats_endpoint(self, client, auth_headers, app):
        JobService.enqueue('test.api', {})
        resp = client.get('/api/v1/jobs/stats', headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()['total'] >= 1

    def test_cancel_via_api(self, client, auth_headers, app):
        job = JobService.enqueue('test.api', {})
        resp = client.post(f'/api/v1/jobs/{job.id}/cancel', headers=auth_headers)
        assert resp.status_code == 200
        payload = resp.get_json()['job']
        assert payload['status'] == Job.STATUS_CANCELLED
        assert payload['can_cancel'] is False
        assert payload['can_retry'] is True

    def test_scheduled_listing(self, client, auth_headers, app):
        ScheduledJobService.ensure('api-sched', 'test.x', interval_seconds=60)
        resp = client.get('/api/v1/jobs/scheduled', headers=auth_headers)
        assert resp.status_code == 200
        assert any(s['name'] == 'api-sched' for s in resp.get_json()['scheduled'])

    def test_list_returns_total_and_paging_metadata(self, client, auth_headers, app):
        for i in range(5):
            JobService.enqueue('test.page', {'i': i})
        resp = client.get('/api/v1/jobs?limit=2&offset=0', headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert len(body['jobs']) == 2
        assert body['total'] == 5
        assert body['limit'] == 2 and body['offset'] == 0

    def test_q_filters_over_kind_and_owner(self, client, auth_headers, app):
        JobService.enqueue('backup.run', {}, owner_type='schedule', owner_id='nightly')
        JobService.enqueue('workflow.execute', {}, owner_type='workflow', owner_id='42')

        # Match by kind fragment.
        resp = client.get('/api/v1/jobs?q=backup', headers=auth_headers)
        body = resp.get_json()
        assert body['total'] == 1
        assert body['jobs'][0]['kind'] == 'backup.run'

        # Match by owner_id fragment (case-insensitive).
        resp = client.get('/api/v1/jobs?q=NIGHTLY', headers=auth_headers)
        assert resp.get_json()['total'] == 1

        # Match by owner_type fragment spanning both would be broader; a specific
        # term isolates one row.
        resp = client.get('/api/v1/jobs?q=workflow', headers=auth_headers)
        assert resp.get_json()['total'] == 1


class TestJobRetention:
    def test_prune_removes_old_terminal_keeps_fresh_and_running(self, app):
        from datetime import datetime, timedelta

        now = datetime.utcnow()

        def _job(kind, status, completed_delta_days):
            j = Job(kind=kind, status=status)
            if completed_delta_days is not None:
                j.completed_at = now - timedelta(days=completed_delta_days)
            j.created_at = now - timedelta(days=(completed_delta_days or 0))
            db.session.add(j)
            return j

        old_ok = _job('t.ok', Job.STATUS_SUCCEEDED, 30)       # > 14d → pruned
        old_cancel = _job('t.c', Job.STATUS_CANCELLED, 20)    # > 14d → pruned
        fresh_ok = _job('t.ok', Job.STATUS_SUCCEEDED, 2)      # < 14d → kept
        old_failed = _job('t.f', Job.STATUS_FAILED, 30)       # < 42d → kept
        ancient_failed = _job('t.f', Job.STATUS_FAILED, 60)   # > 42d → pruned
        running = _job('t.r', Job.STATUS_RUNNING, None)       # never touched
        pending = _job('t.p', Job.STATUS_PENDING, None)       # never touched
        db.session.commit()

        # Capture ids before pruning — accessing .id on a deleted ORM object
        # (synchronize_session=False) would trigger a failed row refresh.
        ids = {name: j.id for name, j in {
            'old_ok': old_ok, 'old_cancel': old_cancel, 'fresh_ok': fresh_ok,
            'old_failed': old_failed, 'ancient_failed': ancient_failed,
            'running': running, 'pending': pending,
        }.items()}

        deleted = JobService.prune_terminal(retention_days=14)
        assert deleted == 3

        survivors = {j.id for j in Job.query.all()}
        assert ids['fresh_ok'] in survivors
        assert ids['old_failed'] in survivors
        assert ids['running'] in survivors
        assert ids['pending'] in survivors
        assert ids['old_ok'] not in survivors
        assert ids['old_cancel'] not in survivors
        assert ids['ancient_failed'] not in survivors

    def test_prune_deletes_the_jobs_log_lines_and_old_orphans(self, app):
        # A pruned job's run log lines used to stay behind forever: a 25 GB
        # box carried 540k orphaned rows (~160 MB with the index).
        from datetime import datetime, timedelta
        from app.models.run_log import RunLogEntry

        now = datetime.utcnow()
        old = Job(kind='t.ok', status=Job.STATUS_SUCCEEDED,
                  completed_at=now - timedelta(days=30), created_at=now - timedelta(days=30))
        fresh = Job(kind='t.ok', status=Job.STATUS_SUCCEEDED,
                    completed_at=now - timedelta(days=1), created_at=now - timedelta(days=1))
        db.session.add_all([old, fresh])
        db.session.commit()
        old_id, fresh_id = old.id, fresh.id

        def _line(run_id, days, kind='job'):
            db.session.add(RunLogEntry(run_kind=kind, run_id=run_id, message='x',
                                       created_at=now - timedelta(days=days)))
        _line(old_id, 30)
        _line(fresh_id, 1)
        _line('gone-long-ago', 40)            # orphan from an earlier prune
        _line('not-committed-yet', 0)         # too new to call an orphan
        _line('gone-long-ago', 40, kind='sandbox')  # another kind's lines
        db.session.commit()

        JobService.prune_terminal(retention_days=14)

        left = {(r.run_kind, r.run_id) for r in RunLogEntry.query.all()}
        assert left == {('job', fresh_id), ('job', 'not-committed-yet'),
                        ('sandbox', 'gone-long-ago')}

    def test_retention_handler_respects_zero_disable(self, app, monkeypatch):
        from app.jobs import builtin_handlers
        from app.services.settings_service import SettingsService

        monkeypatch.setattr(SettingsService, 'get',
                            staticmethod(lambda key, default=None: 0 if key == 'jobs.retention_days' else default))
        called = {'n': 0}
        monkeypatch.setattr(JobService, 'prune_terminal',
                            classmethod(lambda cls, **kw: called.__setitem__('n', called['n'] + 1)))
        assert builtin_handlers.run_job_retention() is None
        assert called['n'] == 0


class TestDeploymentInstallHandler:
    """Phase 5 — template installs run as a 'deploy.install' unified job rather
    than a one-off thread. run_job is patched so these exercise the wiring, not
    real Docker/templates."""

    def test_register_jobs_adds_handler(self, app):
        from app.services.deployment_job_service import DeploymentJobService, JOB_KIND
        DeploymentJobService.register_jobs()
        assert JOB_KIND == 'deploy.install'
        assert registry.is_registered('deploy.install')

    def test_handler_success_marks_job_succeeded(self, app, monkeypatch):
        from app.services.deployment_job_service import DeploymentJobService
        calls = {}

        def fake_run_job(job_id):
            calls['job_id'] = job_id
            return {'success': True, 'app_id': 7, 'app_name': 'blog'}

        monkeypatch.setattr(DeploymentJobService, 'run_job', staticmethod(fake_run_job))
        DeploymentJobService.register_jobs()

        unified = JobService.enqueue('deploy.install', {'deployment_job_id': 'dep-1'}, max_attempts=1)
        _drain_once()

        refreshed = Job.query.get(unified.id)
        assert refreshed.status == Job.STATUS_SUCCEEDED
        assert refreshed.get_result()['app_id'] == 7
        assert calls['job_id'] == 'dep-1'

    def test_handler_failure_marks_job_failed(self, app, monkeypatch):
        from app.services.deployment_job_service import DeploymentJobService

        monkeypatch.setattr(DeploymentJobService, 'run_job',
                            staticmethod(lambda job_id: {'success': False, 'error': 'compose boom'}))
        DeploymentJobService.register_jobs()

        unified = JobService.enqueue('deploy.install', {'deployment_job_id': 'dep-2'}, max_attempts=1)
        _drain_once()

        refreshed = Job.query.get(unified.id)
        assert refreshed.status == Job.STATUS_FAILED
        assert 'compose boom' in (refreshed.error_message or '')

    def test_install_template_enqueues_job_instead_of_thread(self, app, monkeypatch):
        from app.services.deployment_job_service import DeploymentJobService
        from app.services.template_service import TemplateService
        from app.models.deployment_job import DeploymentJob

        plan = {
            'app_name': 'demo', 'app_path': '/tmp/serverkit-nonexistent-demo-xyz',
            'port': 8123, 'template_name': 'nginx', 'template_id': 'nginx',
            'steps': [{'type': 'log', 'name': 'step 1'}],
        }
        monkeypatch.setattr(
            TemplateService, 'build_install_plan',
            staticmethod(lambda **kw: {'success': True, 'plan': plan, 'app_path': plan['app_path']}))

        ran = {'called': False}

        def fake_run_job(job_id):
            ran['called'] = True
            return {'success': True}

        monkeypatch.setattr(DeploymentJobService, 'run_job', staticmethod(fake_run_job))
        DeploymentJobService.register_jobs()

        result = DeploymentJobService.install_template(
            template_id='nginx', app_name='demo', user_variables={}, user_id=1,
            server_id='local', wait=False,
        )
        assert result['success'] is True
        dep_id = result['job_id']

        # The DeploymentJob exists and is still pending — proof it was enqueued,
        # not run inline on a thread (no consumer runs under testing config).
        dep = DeploymentJob.query.get(dep_id)
        assert dep is not None and dep.status == 'pending'
        assert ran['called'] is False

        # A unified deploy.install job points back at it, with shared correlation.
        unified = Job.query.filter_by(kind='deploy.install', owner_id=dep_id).first()
        assert unified is not None
        assert unified.get_payload() == {'deployment_job_id': dep_id}
        assert unified.max_attempts == 1
        assert unified.correlation_id == dep.correlation_id


class TestBackupScheduleHandler:
    """Phase 7 — scheduled backups run as 'backup.run' unified jobs, enqueued by
    the builtin.backup_scheduler tick (replacing the orphaned daemon loop).
    BackupService internals are patched so these exercise the wiring."""

    def test_register_jobs_adds_handler(self, app):
        from app.services.backup_service import BackupService, BACKUP_JOB_KIND
        BackupService.register_jobs()
        assert BACKUP_JOB_KIND == 'backup.run'
        assert registry.is_registered('backup.run')

    def test_check_schedules_enqueues_only_due_enabled(self, app, monkeypatch):
        from datetime import datetime
        from app.services.backup_service import BackupService

        now_hm = datetime.now().strftime('%H:%M')
        cfg = {'enabled': True, 'schedules': [
            {'id': 'b1', 'enabled': True, 'schedule_time': now_hm, 'days': ['daily'], 'name': 'nightly'},
            {'id': 'b2', 'enabled': False, 'schedule_time': now_hm, 'days': ['daily'], 'name': 'off'},
        ]}
        monkeypatch.setattr(BackupService, 'get_config', staticmethod(lambda: cfg))
        monkeypatch.setattr(BackupService, 'save_config', staticmethod(lambda c: None))
        monkeypatch.setattr(BackupService, 'cleanup_old_backups', staticmethod(lambda *a, **k: None))

        BackupService.check_backup_schedules()

        due = Job.query.filter_by(kind='backup.run').all()
        assert len(due) == 1
        assert due[0].owner_id == 'b1'
        assert due[0].get_payload() == {'schedule_id': 'b1'}
        assert due[0].max_attempts == 1

    def test_check_schedules_inert_when_disabled(self, app, monkeypatch):
        from datetime import datetime
        from app.services.backup_service import BackupService

        now_hm = datetime.now().strftime('%H:%M')
        cfg = {'enabled': False, 'schedules': [
            {'id': 'b1', 'enabled': True, 'schedule_time': now_hm, 'days': ['daily'], 'name': 'nightly'},
        ]}
        monkeypatch.setattr(BackupService, 'get_config', staticmethod(lambda: cfg))
        BackupService.check_backup_schedules()
        assert Job.query.filter_by(kind='backup.run').count() == 0

    def test_handler_success_and_failure(self, app, monkeypatch):
        from app.services.backup_service import BackupService

        monkeypatch.setattr(BackupService, '_run_scheduled_backup', staticmethod(lambda sched: None))
        BackupService.register_jobs()

        # Success: schedule ends up 'success'.
        monkeypatch.setattr(BackupService, 'get_config',
                            staticmethod(lambda: {'schedules': [{'id': 'b1', 'name': 'n', 'last_status': 'success'}]}))
        ok = JobService.enqueue('backup.run', {'schedule_id': 'b1'}, max_attempts=1)
        _drain_once()
        assert Job.query.get(ok.id).status == Job.STATUS_SUCCEEDED

        # Failure: schedule ends up 'failed' -> handler raises -> job failed.
        monkeypatch.setattr(BackupService, 'get_config',
                            staticmethod(lambda: {'schedules': [{'id': 'b2', 'name': 'n', 'last_status': 'failed'}]}))
        bad = JobService.enqueue('backup.run', {'schedule_id': 'b2'}, max_attempts=1)
        _drain_once()
        assert Job.query.get(bad.id).status == Job.STATUS_FAILED


# TestWorkflowEventDispatch removed in plan 45 Phase 4: the React-Flow Workflow
# Builder engine (WorkflowEngine / WorkflowEventBus) was retired. Its four event
# emitters were ported to EventService (health.check_failed, git.push,
# monitor.high_cpu/high_memory); those are covered in test_tramo_panel_core.py.
