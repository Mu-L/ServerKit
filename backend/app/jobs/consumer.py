"""The generic job consumer — the single worker that runs every enqueued Job.

Mirrors the webhook / notification consumers: a daemon thread polls the
``serverkit-system/jobs`` queue, but the per-message work is delegated to the
handler registered for the job's ``kind``. Retry / backoff / dead-lettering come
from the Queue Bus; this consumer maps the queue outcome back onto the Job row.
"""
import logging
import threading
import time
from datetime import datetime

from app import db
from app.jobs import registry
from app.jobs.models import Job
from app.jobs.service import GROUP_SLUG, QUEUE_SLUG, QUEUE_CONFIG
from app.queue_bus.service import QueueBusService

logger = logging.getLogger(__name__)

_job_consumer = None


class JobConsumer:
    """Polls the queue bus for job messages and runs each via its handler."""

    def __init__(self, app=None, poll_interval_seconds=1, batch_size=5):
        self.app = app
        self.running = False
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size

    def start(self):
        if self.running:
            return
        self.running = True
        self._ensure_queue()
        thread = threading.Thread(target=self._run, daemon=True, name='job-consumer')
        thread.start()
        logger.info('Job consumer started')

    def stop(self):
        self.running = False

    def _ensure_queue(self):
        with self.app.app_context():
            QueueBusService.ensure_queue(GROUP_SLUG, QUEUE_SLUG, config=QUEUE_CONFIG)

    def _run(self):
        while self.running:
            try:
                with self.app.app_context():
                    self.process_batch()
            except Exception as e:
                logger.error(f'Job consumer error: {e}')
            time.sleep(self.poll_interval_seconds)

    def process_batch(self):
        messages = QueueBusService.receive(
            GROUP_SLUG, QUEUE_SLUG,
            visibility_timeout_ms=QUEUE_CONFIG['visibility_timeout_ms'],
            max_messages=self.batch_size,
        )
        for message in messages:
            try:
                self.process_message(message)
            except Exception as e:  # pragma: no cover - defensive, per-message isolation
                logger.error(f'Job message {message.get("id")} crashed the consumer: {e}')
        return len(messages)

    def process_message(self, message):
        """Run one job. Safe to call directly (the test-suite does)."""
        payload = message.get('payload') or {}
        job_id = payload.get('job_id')
        if not job_id:
            QueueBusService.complete(GROUP_SLUG, QUEUE_SLUG, message['id'])
            return

        job = Job.query.get(job_id)
        if not job:
            QueueBusService.complete(GROUP_SLUG, QUEUE_SLUG, message['id'])
            return

        # Honor a cancellation requested before the job was picked up.
        if job.status == Job.STATUS_CANCELLED:
            QueueBusService.complete(GROUP_SLUG, QUEUE_SLUG, message['id'])
            return

        handler = registry.get(job.kind)
        if handler is None:
            # An unroutable job can never succeed — fail it without burning
            # retries, and complete the message so it doesn't loop.
            job.status = Job.STATUS_FAILED
            job.error_message = f'No handler registered for kind {job.kind!r}'
            job.completed_at = datetime.utcnow()
            db.session.commit()
            QueueBusService.complete(GROUP_SLUG, QUEUE_SLUG, message['id'])
            self._emit(job, 'job.failed')
            return

        job.status = Job.STATUS_RUNNING
        job.started_at = job.started_at or datetime.utcnow()
        job.attempts = message.get('attempts') or job.attempts or 0
        job.queue_message_id = message['id']
        db.session.commit()

        # Live run envelope (plan 77 E1): unified jobs stream through the same
        # RunLogStream seam as deploys — run_log/run_status into run_job_<id>,
        # resumable over GET /api/v1/runs/job/<id>/logs?after_id. Handlers can
        # log through stream_for(job) themselves; these frames bracket the run.
        from app.services.run_log_service import stream_for
        stream = stream_for(job)
        stream.log('info', f'Job started: {job.kind} (attempt {job.attempts + 1})')
        stream.flush()
        stream.emit_status()

        try:
            result = handler(job)
            job = Job.query.get(job_id)  # handler may have committed/expired the row
            job.set_result(result)
            job.status = Job.STATUS_SUCCEEDED
            job.completed_at = datetime.utcnow()
            job.error_message = None
            db.session.commit()
            QueueBusService.complete(GROUP_SLUG, QUEUE_SLUG, message['id'])
            stream.log('info', f'Job succeeded: {job.kind}')
            stream.close(Job.STATUS_SUCCEEDED)
            # A successful builtin tick is already on the Jobs page; a system
            # event per tick only doubled the rows. Failures still emit.
            if not job.is_builtin_tick:
                self._emit(job, 'job.succeeded')
        except Exception as e:
            logger.error(f'Job {job_id} ({job.kind}) failed: {e}')
            try:
                db.session.rollback()
            except Exception:
                pass
            stream.log('error', f'Job failed: {e}')
            self._fail(job_id, message, str(e))
            stream.close('failed')

    def _fail(self, job_id, message, error):
        outcome = None
        try:
            outcome = QueueBusService.fail(
                GROUP_SLUG, QUEUE_SLUG, message['id'], error_message=(error or '')[:500],
            )
        except Exception as e:
            logger.error(f'Failed to mark job message failed: {e}')

        job = Job.query.get(job_id)
        if job is None:
            return
        job.error_message = (error or '')[:2000]
        if outcome and outcome.get('status') == 'dead_letter':
            job.status = Job.STATUS_FAILED
            job.completed_at = datetime.utcnow()
            db.session.commit()
            self._emit(job, 'job.failed')
        else:
            # The queue will redeliver after a backoff; reflect that as pending.
            job.status = Job.STATUS_PENDING
            db.session.commit()

    @staticmethod
    def _emit(job, event_type):
        """Best-effort telemetry — never let observability break a job."""
        try:
            from app.services.telemetry_service import TelemetryService
            TelemetryService.emit(
                source='jobs',
                event_type=event_type,
                message=f'Job {event_type.split(".")[-1]}: {job.kind}',
                severity='error' if event_type == 'job.failed' else 'info',
                correlation_id=job.correlation_id,
                payload={'job_id': job.id, 'kind': job.kind, 'attempts': job.attempts},
                commit=True,
            )
        except Exception:
            pass


def start_job_consumer(app):
    """Start the singleton job consumer thread (skipped under testing config)."""
    global _job_consumer
    if _job_consumer is not None:
        return
    if app.config.get('ENV') == 'testing' or app.config.get('TESTING'):
        return
    consumer = JobConsumer(app)
    consumer.start()
    _job_consumer = consumer


def stop_job_consumer():
    global _job_consumer
    if _job_consumer is not None:
        _job_consumer.stop()
        _job_consumer = None
