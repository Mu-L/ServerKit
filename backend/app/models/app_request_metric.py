"""Per-app request rollups from the timed nginx access log (plan 86 §A2).

One row per (app, level, bucket). ``level`` is ``minute`` or ``hour``; the
sampler writes both on ingest, so downsampling is just a coarser bucket, not a
separate aggregation pass. Latency is a fixed-bucket histogram
(``RequestMetricsService.LATENCY_BUCKETS_MS``) rather than stored percentiles:
histograms merge exactly, so a minute read across two sampler ticks — and the
hour built from sixty minutes — still yields correct p50/p95/p99 to bucket
resolution. Pruned on every sampler tick (minute rows after 48h, hour rows
after 30 days).
"""
from datetime import datetime

from app import db
from app.models.json_column_mixin import JsonColumnMixin


class AppRequestMetric(JsonColumnMixin, db.Model):
    __tablename__ = 'app_request_metrics'
    __table_args__ = (
        db.UniqueConstraint('app_id', 'level', 'bucket',
                            name='uq_app_request_metrics_app_level_bucket'),
        db.Index('ix_app_request_metrics_level_bucket', 'level', 'bucket'),
    )

    id = db.Column(db.Integer, primary_key=True)
    app_id = db.Column(
        db.Integer,
        db.ForeignKey('applications.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    level = db.Column(db.String(8), nullable=False)
    bucket = db.Column(db.DateTime, nullable=False)

    requests = db.Column(db.Integer, nullable=False, default=0)
    status_2xx = db.Column(db.Integer, nullable=False, default=0)
    status_3xx = db.Column(db.Integer, nullable=False, default=0)
    status_4xx = db.Column(db.Integer, nullable=False, default=0)
    status_5xx = db.Column(db.Integer, nullable=False, default=0)
    bytes_sent = db.Column(db.BigInteger, nullable=False, default=0)

    # Requests that carried a $request_time (timed-format lines only).
    timed_requests = db.Column(db.Integer, nullable=False, default=0)
    latency_sum_ms = db.Column(db.Float, nullable=False, default=0)
    latency_hist_json = db.Column(db.Text)

    cache_hit = db.Column(db.Integer, nullable=False, default=0)
    cache_miss = db.Column(db.Integer, nullable=False, default=0)
    cache_bypass = db.Column(db.Integer, nullable=False, default=0)
    cache_stale = db.Column(db.Integer, nullable=False, default=0)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    # Parent-side relationship so a hard-deleted app takes its rollups with it
    # (the soft-delete purge door cascades through declared relationships).
    application = db.relationship(
        'Application',
        backref=db.backref('request_metrics', cascade='all, delete-orphan',
                           lazy='select'),
    )

    def get_hist(self):
        try:
            return [int(n) for n in self._json_read('latency_hist_json', [], expect=list)]
        except (TypeError, ValueError):
            return []

    def set_hist(self, hist):
        self._json_write('latency_hist_json', [int(n) for n in hist])

    def __repr__(self):
        return f'<AppRequestMetric app={self.app_id} {self.level} {self.bucket}>'
