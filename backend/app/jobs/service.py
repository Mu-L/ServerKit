"""Producer + management API for the unified job system.

``JobService`` is the one entry point to enqueue work; ``ScheduledJobService``
manages periodic schedules. Both are non-blocking: they persist a row and (for
jobs) publish a thin pointer onto the Queue Bus.
"""
import logging
from datetime import datetime, timedelta

from app import db
from app.jobs.models import Job, ScheduledJob
from app.queue_bus.service import QueueBusService

logger = logging.getLogger(__name__)

GROUP_SLUG = 'serverkit-system'
QUEUE_SLUG = 'jobs'
# Jobs can run longer than a webhook POST, so give them a wider visibility
# window before the queue considers a message abandoned.
QUEUE_CONFIG = {'max_attempts': 3, 'visibility_timeout_ms': 120000}


def _new_correlation_id():
    try:
        from app.services.telemetry_service import generate_correlation_id
        return generate_correlation_id()
    except Exception:
        import uuid
        return uuid.uuid4().hex


class JobService:

    @classmethod
    def enqueue(cls, kind, payload=None, max_attempts=3, priority=0, delay_ms=0,
                owner_type=None, owner_id=None, correlation_id=None):
        """Persist a Job and publish it onto the Queue Bus. Returns the Job."""
        job = Job(
            kind=kind,
            status=Job.STATUS_PENDING,
            max_attempts=max_attempts,
            priority=priority,
            owner_type=owner_type,
            owner_id=str(owner_id) if owner_id is not None else None,
            correlation_id=correlation_id or _new_correlation_id(),
        )
        job.set_payload(payload or {})
        if delay_ms:
            job.scheduled_at = datetime.utcnow() + timedelta(milliseconds=delay_ms)
        db.session.add(job)
        db.session.commit()
        cls._publish(job, delay_ms=delay_ms)
        return job

    @classmethod
    def _publish(cls, job, delay_ms=0):
        QueueBusService.ensure_queue(GROUP_SLUG, QUEUE_SLUG, config=QUEUE_CONFIG)
        msg = QueueBusService.send(
            GROUP_SLUG, QUEUE_SLUG, {'job_id': job.id},
            priority=job.priority or 0, delay_ms=delay_ms,
            max_attempts=job.max_attempts,
        )
        if isinstance(msg, dict):
            job.queue_message_id = msg.get('id')
            db.session.commit()
        return msg

    @classmethod
    def get(cls, job_id):
        return Job.query.get(job_id)

    @staticmethod
    def _filtered_query(status=None, kind=None, owner_type=None, owner_id=None, q=None):
        query = Job.query
        if status:
            query = query.filter_by(status=status)
        if kind:
            query = query.filter_by(kind=kind)
        if owner_type:
            query = query.filter_by(owner_type=owner_type)
        if owner_id is not None:
            query = query.filter_by(owner_id=str(owner_id))
        if q and str(q).strip():
            from sqlalchemy import func, or_
            like = f'%{str(q).strip().lower()}%'
            query = query.filter(or_(
                func.lower(Job.kind).like(like),
                func.lower(Job.owner_type).like(like),
                func.lower(Job.owner_id).like(like),
            ))
        return query

    @classmethod
    def list(cls, status=None, kind=None, owner_type=None, owner_id=None, q=None, limit=50, offset=0):
        query = cls._filtered_query(status, kind, owner_type, owner_id, q)
        return query.order_by(Job.created_at.desc()).limit(limit).offset(offset).all()

    @classmethod
    def count(cls, status=None, kind=None, owner_type=None, owner_id=None, q=None):
        """Total rows matching the same filters as ``list`` (before paging)."""
        return cls._filtered_query(status, kind, owner_type, owner_id, q).count()

    @classmethod
    def cancel(cls, job_id):
        """Mark a pending/running job cancelled. Cannot interrupt a handler that
        is already executing — it stops future attempts and is skipped at the
        next queue pickup."""
        job = Job.query.get(job_id)
        if not job:
            return None
        if job.status in (Job.STATUS_PENDING, Job.STATUS_RUNNING):
            job.status = Job.STATUS_CANCELLED
            job.completed_at = datetime.utcnow()
            db.session.commit()
        return job

    @classmethod
    def retry(cls, job_id):
        """Re-enqueue a failed/cancelled job as a fresh attempt."""
        job = Job.query.get(job_id)
        if not job:
            return None
        if job.status not in (Job.STATUS_FAILED, Job.STATUS_CANCELLED):
            return job
        job.status = Job.STATUS_PENDING
        job.error_message = None
        job.started_at = None
        job.completed_at = None
        db.session.commit()
        cls._publish(job)
        return job

    @classmethod
    def stats(cls):
        from sqlalchemy import func
        by_status = dict(
            db.session.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
        )
        by_kind = dict(
            db.session.query(Job.kind, func.count(Job.id)).group_by(Job.kind).all()
        )
        return {'by_status': by_status, 'by_kind': by_kind, 'total': sum(by_status.values())}

    @classmethod
    def cleanup_old(cls, max_age_seconds=86400):
        """Delete terminal jobs whose completion is older than the window."""
        cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
        ids = [row[0] for row in (
            db.session.query(Job.id)
            .filter(Job.status.in_(Job.TERMINAL_STATUSES))
            .filter(Job.completed_at.isnot(None))
            .filter(Job.completed_at < cutoff)
            .all())]
        deleted = cls._delete_with_logs(ids)
        db.session.commit()
        return deleted

    @staticmethod
    def _delete_with_logs(ids):
        """Delete jobs and their run log lines together. A job's lines live in
        ``run_log_entries`` keyed by its id; deleting only the job strands
        them there forever, since nothing else prunes that table."""
        if not ids:
            return 0
        from app.models.run_log import RunLogEntry
        from app.queue_bus.models import QueueMessage
        (RunLogEntry.query
         .filter(RunLogEntry.run_kind == 'job', RunLogEntry.run_id.in_(ids))
         .delete(synchronize_session=False))
        # The job's completed queue message goes with it; left behind it would
        # wait for the (longer) telemetry window.
        message_ids = [row[0] for row in (
            db.session.query(Job.queue_message_id)
            .filter(Job.id.in_(ids), Job.queue_message_id.isnot(None))
            .all())]
        if message_ids:
            (QueueMessage.query
             .filter(QueueMessage.id.in_(message_ids),
                     QueueMessage.status == QueueMessage.STATUS_COMPLETED)
             .delete(synchronize_session=False))
        return (Job.query
                .filter(Job.id.in_(ids))
                .delete(synchronize_session=False))

    @classmethod
    def prune_orphan_logs(cls, older_than, batch_size=5000):
        """Delete job log lines whose job no longer exists. Boxes that pruned
        jobs before their lines were deleted with them carry these orphans;
        only lines older than ``older_than`` are considered, so a line written
        just before its job row commits is never mistaken for one."""
        from sqlalchemy import exists
        from app.models.run_log import RunLogEntry
        deleted = 0
        while True:
            ids = [row[0] for row in (
                db.session.query(RunLogEntry.id)
                .filter(RunLogEntry.run_kind == 'job')
                .filter(RunLogEntry.created_at < older_than)
                .filter(~exists().where(Job.id == RunLogEntry.run_id))
                .limit(batch_size)
                .all())]
            if not ids:
                break
            deleted += (RunLogEntry.query
                        .filter(RunLogEntry.id.in_(ids))
                        .delete(synchronize_session=False))
            db.session.commit()
            if len(ids) < batch_size:
                break
        return deleted

    @classmethod
    def prune_terminal(cls, retention_days=14, batch_size=5000):
        """Prune accumulated terminal jobs so scheduler-tick rows don't grow
        without bound. Succeeded/cancelled jobs are kept ``retention_days``;
        failed jobs 3x as long (they're worth keeping longer for diagnosis).
        Queued/running rows are never touched. Deletes in batches so a huge
        backlog doesn't lock the table in one statement. Returns the count.
        """
        from sqlalchemy import func
        now = datetime.utcnow()
        age = func.coalesce(Job.completed_at, Job.created_at)
        specs = [
            ((Job.STATUS_SUCCEEDED, Job.STATUS_CANCELLED),
             now - timedelta(days=retention_days)),
            ((Job.STATUS_FAILED,),
             now - timedelta(days=retention_days * 3)),
        ]
        # Successful builtin ticks go after a day (see Job.is_builtin_tick).
        tick = ((Job.STATUS_SUCCEEDED,),
                now - timedelta(hours=Job.TICK_RETENTION_HOURS),
                (Job.scheduled_job_id.isnot(None),
                 Job.kind.startswith(Job.BUILTIN_KIND_PREFIX)))
        specs = [tick] + [(statuses, cutoff, ()) for statuses, cutoff in specs]
        deleted = 0
        for statuses, cutoff, extra in specs:
            while True:
                ids = [row[0] for row in (
                    db.session.query(Job.id)
                    .filter(Job.status.in_(statuses))
                    .filter(age < cutoff)
                    .filter(*extra)
                    .limit(batch_size)
                    .all())]
                if not ids:
                    break
                deleted += cls._delete_with_logs(ids)
                db.session.commit()
                if len(ids) < batch_size:
                    break
        cls.prune_orphan_logs(now - timedelta(days=retention_days), batch_size)
        return deleted


class ScheduledJobService:

    @classmethod
    def ensure(cls, name, kind, interval_seconds=None, cron=None, payload=None,
               max_attempts=1, startup_delay_seconds=0, enabled=True):
        """Idempotently create/update a periodic schedule keyed by ``name``.

        Updates cadence/kind on an existing row but PRESERVES its
        ``next_run_at`` / ``last_run_at`` and the admin's ``enabled`` toggle so a
        restart doesn't reset the clock. On first creation, seeds ``next_run_at``
        honoring ``startup_delay_seconds``.
        """
        scheduled = ScheduledJob.query.filter_by(name=name).first()
        created = scheduled is None
        if created:
            scheduled = ScheduledJob(name=name)
            db.session.add(scheduled)
        scheduled.kind = kind
        scheduled.schedule_kind = ScheduledJob.SCHEDULE_CRON if cron else ScheduledJob.SCHEDULE_INTERVAL
        scheduled.interval_seconds = interval_seconds
        scheduled.cron = cron
        scheduled.max_attempts = max_attempts
        if payload is not None:
            scheduled.set_payload(payload)
        if created:
            scheduled.enabled = enabled
            scheduled.next_run_at = datetime.utcnow() + timedelta(seconds=startup_delay_seconds or 0)
        db.session.commit()
        return scheduled

    @classmethod
    def due(cls, now=None):
        now = now or datetime.utcnow()
        return (ScheduledJob.query
                .filter(ScheduledJob.enabled.is_(True))
                .filter(ScheduledJob.next_run_at.isnot(None))
                .filter(ScheduledJob.next_run_at <= now)
                .all())

    @classmethod
    def fire(cls, scheduled, now=None):
        """Enqueue a Job for one schedule and advance its ``next_run_at``."""
        now = now or datetime.utcnow()
        job = JobService.enqueue(
            scheduled.kind,
            payload=scheduled.get_payload(),
            max_attempts=scheduled.max_attempts,
            owner_type='schedule',
            owner_id=scheduled.name,
        )
        job.scheduled_job_id = scheduled.id
        scheduled.last_run_at = now
        scheduled.last_job_id = job.id
        scheduled.next_run_at = scheduled.compute_next_run(now)
        db.session.commit()
        return job

    @classmethod
    def list(cls):
        return ScheduledJob.query.order_by(ScheduledJob.name.asc()).all()

    @classmethod
    def run_now(cls, scheduled_job_id):
        scheduled = ScheduledJob.query.get(scheduled_job_id)
        if not scheduled:
            return None
        return cls.fire(scheduled)

    @classmethod
    def set_enabled(cls, scheduled_job_id, enabled):
        scheduled = ScheduledJob.query.get(scheduled_job_id)
        if not scheduled:
            return None
        scheduled.enabled = bool(enabled)
        if enabled and not scheduled.next_run_at:
            scheduled.next_run_at = datetime.utcnow()
        db.session.commit()
        return scheduled
