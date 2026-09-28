"""Per-app request metrics from the timed access log (plan 86 §A2).

A fixture log gives exact counts and percentiles; the byte-offset cursor never
counts a line twice across ticks, rotation or truncation; and old rows are
pruned on the same tick that writes new ones (this table class has filled the
disk before — plan 85).
"""
import os
from datetime import datetime, timedelta

import pytest

from app import db
from app.models.app_request_metric import AppRequestMetric
from app.services import request_metrics_service as rms
from app.services.request_metrics_service import RequestMetricsService, parse_line
from tests.factories import make_application

NOW = datetime(2026, 7, 3, 10, 5, 0)


def _line(minute=0, second=0, status=200, nbytes=100, rt=None, cs='-',
          tz='+0000', hour=10):
    base = (f'203.0.113.9 - - [03/Jul/2026:{hour:02d}:{minute:02d}:{second:02d} {tz}] '
            f'"GET / HTTP/1.1" {status} {nbytes} "-" "curl/8"')
    if rt is None:
        return base
    return base + f' rt={rt} urt="{rt}" cs={cs} h=shop.example.com'


def _make_app(name='shop'):
    return make_application(db, name=name)


def _rows(app_id, level):
    return (AppRequestMetric.query
            .filter_by(app_id=app_id, level=level)
            .order_by(AppRequestMetric.bucket).all())


def _write(path, lines, mode='a'):
    with open(path, mode, newline='\n') as fh:
        fh.write(''.join(line + '\n' for line in lines))


# ── parsing ──────────────────────────────────────────────────────────────────

class TestParseLine:
    def test_timed_line(self):
        rec = parse_line(_line(rt='0.250', cs='HIT', nbytes=512, status=404))
        assert rec['status'] == 404
        assert rec['bytes'] == 512
        assert rec['rt_ms'] == pytest.approx(250.0)
        assert rec['cache'] == 'HIT'
        assert rec['time'] == datetime(2026, 7, 3, 10, 0, 0)

    def test_plain_combined_line_counts_without_latency(self):
        rec = parse_line(_line())
        assert rec['status'] == 200 and rec['rt_ms'] is None and rec['cache'] is None

    def test_time_is_normalised_to_utc(self):
        rec = parse_line(_line(hour=12, tz='+0200', rt='0.001'))
        assert rec['time'] == datetime(2026, 7, 3, 10, 0, 0)

    def test_uncached_request_has_no_cache_status(self):
        assert parse_line(_line(rt='0.010', cs='-'))['cache'] is None

    def test_garbage_is_rejected(self):
        assert parse_line('not an access log line') is None


class TestPercentile:
    def test_exact_bucket_bounds(self):
        # 90 fast (<=10ms) + 9 at 200ms + 1 at 3s
        hist = [0] * rms.HIST_LEN
        hist[rms.hist_index(8)] = 90
        hist[rms.hist_index(200)] = 9
        hist[rms.hist_index(3000)] = 1
        assert rms.percentile(hist, 0.50) == 10
        assert rms.percentile(hist, 0.95) == 250
        assert rms.percentile(hist, 0.99) == 250
        assert rms.percentile(hist, 1.0) == 5000

    def test_overflow_reports_the_last_bound(self):
        hist = [0] * rms.HIST_LEN
        hist[rms.hist_index(90_000)] = 1
        assert rms.percentile(hist, 0.5) == rms.LATENCY_BUCKETS_MS[-1]

    def test_empty_histogram(self):
        assert rms.percentile([0] * rms.HIST_LEN, 0.95) is None


# ── rollup + ingest ──────────────────────────────────────────────────────────

FIXTURE = [
    _line(0, 1, 200, 100, '0.008', 'HIT'),
    _line(0, 2, 200, 100, '0.009', 'HIT'),
    _line(0, 3, 200, 100, '0.020', 'MISS'),
    _line(0, 4, 304, 0, '0.001', 'REVALIDATED'),
    _line(0, 5, 404, 50, '0.004', 'BYPASS'),
    _line(0, 6, 502, 10, '0.300', 'STALE'),
    _line(1, 0, 200, 100, '1.200', '-'),
    _line(1, 1, 500, 10, '0.050', 'EXPIRED'),
    _line(1, 2, 200, 100),                      # legacy combined line
    'garbage',
]


class TestSample:
    def test_fixture_log_gives_exact_counts_and_percentiles(self, app, tmp_path):
        site = _make_app()
        _write(tmp_path / 'shop.access.log', FIXTURE, 'w')

        stats = RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)

        assert stats['lines'] == 10 and stats['unparsed'] == 1
        m0, m1 = _rows(site.id, 'minute')
        assert m0.bucket == datetime(2026, 7, 3, 10, 0)
        assert (m0.requests, m0.status_2xx, m0.status_3xx, m0.status_4xx,
                m0.status_5xx) == (6, 3, 1, 1, 1)
        assert (m0.cache_hit, m0.cache_miss, m0.cache_bypass,
                m0.cache_stale) == (3, 1, 1, 1)
        assert m0.bytes_sent == 360
        assert (m1.requests, m1.timed_requests, m1.status_5xx,
                m1.cache_miss) == (3, 2, 1, 1)

        (hour,) = _rows(site.id, 'hour')
        assert hour.bucket == datetime(2026, 7, 3, 10, 0)
        assert hour.requests == 9 and hour.timed_requests == 8

        summary = RequestMetricsService.series(site.id, '1h', now=NOW)['summary']
        assert summary['requests'] == 9
        # timed latencies (ms): 1, 4, 8, 9, 20, 50, 300, 1200
        assert summary['p50_ms'] == 10
        assert summary['p95_ms'] == 2500
        assert summary['avg_ms'] == pytest.approx(1592 / 8)
        # (HIT+REVALIDATED 3 + STALE 1) / (4 + MISS/EXPIRED 2)
        assert summary['cache_hit_ratio'] == pytest.approx(4 / 6)
        assert summary['error_rate'] == pytest.approx(2 / 9)

    def test_a_second_tick_reads_only_new_lines(self, app, tmp_path):
        site = _make_app()
        log = tmp_path / 'shop.access.log'
        _write(log, [_line(0, 1, rt='0.010')], 'w')
        RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)
        RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)
        _write(log, [_line(0, 30, rt='0.010')])
        RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)

        (m0,) = _rows(site.id, 'minute')
        assert m0.requests == 2
        assert sum(m0.get_hist()) == 2

    def test_a_half_written_line_waits_for_the_next_tick(self, app, tmp_path):
        site = _make_app()
        log = tmp_path / 'shop.access.log'
        full = _line(0, 1, rt='0.010')
        with open(log, 'w', newline='\n') as fh:
            fh.write(full + '\n' + full[:20])
        RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)
        assert _rows(site.id, 'minute')[0].requests == 1

        with open(log, 'a', newline='\n') as fh:
            fh.write(full[20:] + '\n')
        RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)
        assert _rows(site.id, 'minute')[0].requests == 2

    def test_rotation_finishes_the_old_file_then_reads_the_new_one(self, app, tmp_path):
        site = _make_app()
        log = tmp_path / 'shop.access.log'
        _write(log, [_line(0, 1)], 'w')
        RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)
        _write(log, [_line(0, 2)])                   # written before rotation
        os.replace(log, str(log) + '.1')
        _write(log, [_line(0, 3), _line(0, 4)], 'w')

        RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)
        assert _rows(site.id, 'minute')[0].requests == 4

    def test_truncation_restarts_from_zero(self, app, tmp_path):
        site = _make_app()
        log = tmp_path / 'shop.access.log'
        _write(log, [_line(0, 1), _line(0, 2), _line(0, 3)], 'w')
        RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)
        with open(log, 'r+') as fh:                  # copytruncate
            fh.truncate(0)
        _write(log, [_line(0, 4)])
        RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)
        assert _rows(site.id, 'minute')[0].requests == 4

    def test_a_backlog_bigger_than_the_cap_skips_to_the_newest_lines(
            self, app, tmp_path, monkeypatch):
        site = _make_app()
        line = _line(0, 1)
        monkeypatch.setattr(rms, 'MAX_READ_BYTES', len(line) * 3)
        _write(tmp_path / 'shop.access.log', [line] * 10, 'w')

        stats = RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)

        assert stats['skipped_bytes'] > 0
        # The cap window holds 3 lines; the first is partial and dropped.
        assert _rows(site.id, 'minute')[0].requests == 2

    def test_an_app_without_a_log_is_a_clean_no_op(self, app, tmp_path):
        _make_app()
        stats = RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)
        assert stats['apps'] == 0 and stats['rows'] == 0


# ── retention ────────────────────────────────────────────────────────────────

def test_old_rows_are_pruned_on_the_same_tick(app, tmp_path):
    site = _make_app()
    old_minute = NOW - timedelta(hours=rms.MINUTE_RETENTION_HOURS, minutes=1)
    fresh_minute = NOW - timedelta(hours=1)
    old_hour = NOW - timedelta(days=rms.HOUR_RETENTION_DAYS, hours=1)
    fresh_hour = NOW - timedelta(days=rms.HOUR_RETENTION_DAYS - 1)
    for level, bucket in (('minute', old_minute), ('minute', fresh_minute),
                          ('hour', old_hour), ('hour', fresh_hour),
                          ('hour', old_minute)):     # 48h-old hour row stays
        db.session.add(AppRequestMetric(app_id=site.id, level=level,
                                        bucket=bucket, requests=1))
    db.session.commit()

    stats = RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)

    assert stats['pruned'] == 2
    assert [r.bucket for r in _rows(site.id, 'minute')] == [fresh_minute]
    assert sorted(r.bucket for r in _rows(site.id, 'hour')) == [fresh_hour, old_minute]


def test_the_sampler_is_a_builtin_tick():
    """Once a minute: it must get the 24 h tick retention, not 14-30 days."""
    from app.jobs.builtin_handlers import _BUILTINS
    (row,) = [b for b in _BUILTINS if b[2] == 'request-metrics']
    assert row[0].startswith('builtin.') and row[3] == 60


# ── read side + API ──────────────────────────────────────────────────────────

def test_series_is_zero_filled(app, tmp_path):
    site = _make_app()
    _write(tmp_path / 'shop.access.log', FIXTURE, 'w')
    RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)

    data = RequestMetricsService.series(site.id, '1h', now=NOW)
    assert len(data['points']) == 60
    assert data['points'][-1]['t'].startswith('2026-07-03T10:05')
    by_t = {p['t'][:16]: p for p in data['points']}
    assert by_t['2026-07-03T10:00']['requests'] == 6
    assert by_t['2026-07-03T10:02']['requests'] == 0

    assert len(RequestMetricsService.series(site.id, '7d', now=NOW)['points']) == 168


def test_series_rejects_an_unknown_period(app):
    with pytest.raises(ValueError):
        RequestMetricsService.series(1, '5y')


def test_api(app, client, auth_headers):
    site = _make_app()
    ok = client.get(f'/api/v1/bandwidth/apps/{site.id}/requests?period=1h',
                    headers=auth_headers)
    assert ok.status_code == 200
    assert ok.get_json()['summary']['requests'] == 0

    bad = client.get(f'/api/v1/bandwidth/apps/{site.id}/requests?period=5y',
                     headers=auth_headers)
    assert bad.status_code == 400

    missing = client.get('/api/v1/bandwidth/apps/999999/requests',
                         headers=auth_headers)
    assert missing.status_code == 404


def test_hard_deleting_an_app_takes_its_rollups(app, tmp_path):
    """SQLite doesn't enforce ON DELETE CASCADE here; the ORM relationship must."""
    from app.models.application import Application
    site = _make_app()
    _write(tmp_path / 'shop.access.log', FIXTURE, 'w')
    RequestMetricsService.sample(log_dir=str(tmp_path), now=NOW)
    assert AppRequestMetric.query.filter_by(app_id=site.id).count() > 0
    db.session.delete(Application.query.get(site.id))
    db.session.commit()
    assert AppRequestMetric.query.filter_by(app_id=site.id).count() == 0
