"""Models for the unified background-job system.

``Job`` is a persisted unit of work with a stable lifecycle
(``pending -> running -> succeeded | failed | cancelled``). The Queue Bus stays
the transport: enqueuing a Job publishes a thin ``{'job_id': ...}`` message; the
``JobConsumer`` loads the row and dispatches by ``kind``. Retries / backoff /
dead-lettering are inherited from the queue — the Job row mirrors the outcome so
there is a single place to observe all background work.

``ScheduledJob`` is the periodic side: one ticker enqueues a Job for each due
schedule, replacing the per-domain daemon threads that each ran their own
``while True: sleep`` loop.
"""
from datetime import datetime, timedelta
import json
import uuid

from app import db


class Job(db.Model):
    __tablename__ = 'jobs'

    # Run envelope kind (plan 77 E1): rooms/events key on (run_kind, id).
    __run_kind__ = 'job'

    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_SUCCEEDED = 'succeeded'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'

    TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED)

    # The panel's own scheduled checks (monitor, backup scheduler, sync…) fire
    # every minute or so, ~12k runs a day on one box. A successful one records
    # "checked"; it is kept a day, not the full job retention.
    BUILTIN_KIND_PREFIX = 'builtin.'
    TICK_RETENTION_HOURS = 24

    @property
    def is_builtin_tick(self):
        return (self.scheduled_job_id is not None
                and (self.kind or '').startswith(self.BUILTIN_KIND_PREFIX))

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind = db.Column(db.String(80), nullable=False, index=True)
    status = db.Column(db.String(20), default=STATUS_PENDING, nullable=False, index=True)

    payload = db.Column(db.Text, default='{}')
    result = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    attempts = db.Column(db.Integer, default=0, nullable=False)
    max_attempts = db.Column(db.Integer, default=3, nullable=False)
    priority = db.Column(db.Integer, default=0)

    # Loose ownership for filtering ("jobs for this server / app / user / schedule").
    owner_type = db.Column(db.String(40), nullable=True, index=True)
    owner_id = db.Column(db.String(64), nullable=True, index=True)

    # Backreference to the schedule that spawned this job, if periodic (no FK so
    # table create-order never matters).
    scheduled_job_id = db.Column(db.Integer, nullable=True, index=True)

    correlation_id = db.Column(db.String(64), nullable=True, index=True)
    # The queue message currently carrying this job (for ops / debugging).
    queue_message_id = db.Column(db.String(36), nullable=True)

    scheduled_at = db.Column(db.DateTime, nullable=True)  # requested delay target
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def get_payload(self):
        try:
            return json.loads(self.payload) if self.payload else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def set_payload(self, payload):
        self.payload = json.dumps(payload or {})

    def get_result(self):
        try:
            return json.loads(self.result) if self.result else None
        except (TypeError, json.JSONDecodeError):
            return None

    def set_result(self, result):
        """Store a JSON-serializable result; fall back to a repr so a weird
        return value never blocks marking a job done."""
        if result is None:
            self.result = None
            return
        try:
            self.result = json.dumps(result)
        except (TypeError, ValueError):
            self.result = json.dumps({'repr': str(result)[:2000]})

    @property
    def duration(self):
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    def to_dict(self, include_payload=False):
        data = {
            'id': self.id,
            'kind': self.kind,
            'status': self.status,
            'attempts': self.attempts,
            'max_attempts': self.max_attempts,
            'priority': self.priority,
            'owner_type': self.owner_type,
            'owner_id': self.owner_id,
            'scheduled_job_id': self.scheduled_job_id,
            'correlation_id': self.correlation_id,
            'result': self.get_result(),
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'duration': self.duration,
        }
        if include_payload:
            data['payload'] = self.get_payload()
        return data


class ScheduledJob(db.Model):
    __tablename__ = 'scheduled_jobs'

    SCHEDULE_INTERVAL = 'interval'
    SCHEDULE_CRON = 'cron'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(80), unique=True, nullable=False)  # stable upsert key
    kind = db.Column(db.String(80), nullable=False)               # job kind to enqueue
    schedule_kind = db.Column(db.String(20), default=SCHEDULE_INTERVAL, nullable=False)
    interval_seconds = db.Column(db.Integer, nullable=True)
    cron = db.Column(db.String(120), nullable=True)
    payload = db.Column(db.Text, default='{}')
    max_attempts = db.Column(db.Integer, default=1, nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)

    next_run_at = db.Column(db.DateTime, nullable=True, index=True)
    last_run_at = db.Column(db.DateTime, nullable=True)
    last_job_id = db.Column(db.String(36), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def get_payload(self):
        try:
            return json.loads(self.payload) if self.payload else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def set_payload(self, payload):
        self.payload = json.dumps(payload or {})

    def compute_next_run(self, base=None):
        base = base or datetime.utcnow()
        if self.schedule_kind == self.SCHEDULE_CRON and self.cron:
            try:
                from croniter import croniter
                if croniter.is_valid(self.cron):
                    return croniter(self.cron, base).get_next(datetime)
            except ImportError:
                pass
            # Cron invalid / croniter unavailable — back off an hour rather than
            # hot-looping.
            return base + timedelta(hours=1)
        return base + timedelta(seconds=self.interval_seconds or 3600)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'kind': self.kind,
            'schedule_kind': self.schedule_kind,
            'interval_seconds': self.interval_seconds,
            'cron': self.cron,
            'enabled': self.enabled,
            'max_attempts': self.max_attempts,
            'next_run_at': self.next_run_at.isoformat() if self.next_run_at else None,
            'last_run_at': self.last_run_at.isoformat() if self.last_run_at else None,
            'last_job_id': self.last_job_id,
        }
