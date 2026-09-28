"""Per-app request rate, errors and latency from nginx access logs (plan 86 §A2).

A one-minute builtin tick (``builtin.request_metrics``) tails each live app's
``/var/log/nginx/<app>.access.log`` from a stored byte offset. It never
re-parses a whole day, and it rolls the new lines up into
:class:`AppRequestMetric` rows at two levels:

  * ``minute`` rows, kept ``MINUTE_RETENTION_HOURS``
  * ``hour`` rows, kept ``HOUR_RETENTION_DAYS``

Both levels are written on ingest, and old rows are pruned on the same tick,
so the table's size is bounded from day one. Tables like this one filled the
disk before (plan 85).

Lines in the timed format (``NginxService.TIMED_LOG_FORMAT``) contribute
latency and cache status. Plain combined lines, from vhosts written before the
format existed, still count toward requests, status classes and bytes.

Cursor handling:
  * offsets live in one SystemSettings JSON row, keyed by path, with the
    file's inode;
  * on rotation (the inode changed), the tail of ``<log>.1`` is finished first
    when it is still the file we were reading, then the new file is read from 0;
  * on truncation (``copytruncate``) reading restarts from 0;
  * one tick reads at most ``MAX_READ_BYTES`` per file. A larger backlog skips
    ahead to the newest data, because a first run on a busy box must not
    stall the scheduler;
  * only complete lines are consumed. A half-written last line waits for the
    next tick.
"""
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone

from app import db

logger = logging.getLogger(__name__)

CURSOR_SETTING_KEY = 'request_metrics_cursors'

MINUTE_RETENTION_HOURS = 48
HOUR_RETENTION_DAYS = 30
MAX_READ_BYTES = 16 * 1024 * 1024

# Upper bounds (ms) of the latency histogram buckets; one extra overflow
# bucket follows the last bound. Percentiles are reported as the upper bound
# of the bucket the rank falls in.
LATENCY_BUCKETS_MS = (1, 2, 5, 10, 25, 50, 100, 250, 500,
                      1000, 2500, 5000, 10000, 30000)
HIST_LEN = len(LATENCY_BUCKETS_MS) + 1

_LINE_RE = re.compile(
    r'^\S+ \S+ \S+ \[(?P<time>[^\]]+)\] '
    r'"[^"]*" (?P<status>\d{3}) (?P<bytes>\d+|-)'
)
_TIMED_TAIL_RE = re.compile(
    r' rt=(?P<rt>[\d.]+|-) urt="[^"]*" cs=(?P<cs>\S+) h=\S+\s*$')

_MONTHS = {m: i for i, m in enumerate(
    ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), start=1)}

# $upstream_cache_status → the four counters kept per row.
_CACHE_CLASS = {
    'HIT': 'cache_hit', 'REVALIDATED': 'cache_hit',
    'MISS': 'cache_miss', 'EXPIRED': 'cache_miss',
    'BYPASS': 'cache_bypass',
    'STALE': 'cache_stale', 'UPDATING': 'cache_stale',
}

_COUNTERS = ('requests', 'status_2xx', 'status_3xx', 'status_4xx',
             'status_5xx', 'bytes_sent', 'timed_requests',
             'cache_hit', 'cache_miss', 'cache_bypass', 'cache_stale')

PERIODS = {
    '1h': ('minute', timedelta(hours=1)),
    '24h': ('minute', timedelta(hours=24)),
    '7d': ('hour', timedelta(days=7)),
    '30d': ('hour', timedelta(days=30)),
}


def _parse_time(raw):
    """``'03/Jul/2026:10:00:07 +0200'`` → naive UTC datetime, or None."""
    try:
        day, mon, rest = raw.split('/', 2)
        year, hh, mm, ss_tz = rest.split(':', 3)
        ss, tz = ss_tz.split(' ')
        sign = -1 if tz[0] == '-' else 1
        offset = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
        local = datetime(int(year), _MONTHS[mon], int(day),
                         int(hh), int(mm), int(ss))
        return local - offset
    except (ValueError, KeyError, IndexError):
        return None


def parse_line(line):
    """One access-log line → dict, or None when it isn't a combined line.

    Keys: ``time`` (naive UTC), ``status``, ``bytes``, ``rt_ms`` (None for
    plain combined lines or ``-``), ``cache`` (upper-cased status or None).
    """
    match = _LINE_RE.match(line)
    if not match:
        return None
    when = _parse_time(match.group('time'))
    if when is None:
        return None
    raw_bytes = match.group('bytes')
    out = {
        'time': when,
        'status': int(match.group('status')),
        'bytes': int(raw_bytes) if raw_bytes.isdigit() else 0,
        'rt_ms': None,
        'cache': None,
    }
    tail = _TIMED_TAIL_RE.search(line)
    if tail:
        rt = tail.group('rt')
        if rt != '-':
            out['rt_ms'] = float(rt) * 1000.0
        cs = tail.group('cs').upper()
        out['cache'] = cs if cs != '-' else None
    return out


def hist_index(ms):
    for i, bound in enumerate(LATENCY_BUCKETS_MS):
        if ms <= bound:
            return i
    return len(LATENCY_BUCKETS_MS)


def percentile(hist, p):
    """Upper bucket bound holding the ``p`` quantile (0 < p <= 1), or None.

    A rank in the overflow bucket reports the last bound — "at least this".
    """
    total = sum(hist)
    if total <= 0:
        return None
    rank = max(1, math.ceil(p * total))
    seen = 0
    for i, count in enumerate(hist):
        seen += count
        if seen >= rank:
            return LATENCY_BUCKETS_MS[min(i, len(LATENCY_BUCKETS_MS) - 1)]
    return LATENCY_BUCKETS_MS[-1]


def _floor(when, level):
    if level == 'hour':
        return when.replace(minute=0, second=0, microsecond=0)
    return when.replace(second=0, microsecond=0)


def _empty_acc():
    acc = {k: 0 for k in _COUNTERS}
    acc['latency_sum_ms'] = 0.0
    acc['hist'] = [0] * HIST_LEN
    return acc


def _add_line(acc, rec):
    acc['requests'] += 1
    cls = rec['status'] // 100
    if 2 <= cls <= 5:
        acc[f'status_{cls}xx'] += 1
    acc['bytes_sent'] += rec['bytes']
    if rec['rt_ms'] is not None:
        acc['timed_requests'] += 1
        acc['latency_sum_ms'] += rec['rt_ms']
        acc['hist'][hist_index(rec['rt_ms'])] += 1
    counter = _CACHE_CLASS.get(rec['cache'] or '')
    if counter:
        acc[counter] += 1


class RequestMetricsService:

    # ------------------------------------------------------------------ #
    # Reading new lines
    # ------------------------------------------------------------------ #

    @staticmethod
    def _read_from(path, offset):
        """Complete lines of ``path`` after ``offset``.

        Returns ``(lines, new_offset, skipped_bytes)``. At most
        ``MAX_READ_BYTES`` are read; a bigger backlog jumps ahead to the
        newest data and drops the partial line it lands in.
        """
        try:
            size = os.path.getsize(path)
            skipped = 0
            start = offset
            if size - start > MAX_READ_BYTES:
                skipped = size - MAX_READ_BYTES - start
                start = size - MAX_READ_BYTES
            with open(path, 'rb') as fh:
                fh.seek(start)
                data = fh.read(size - start)
        except OSError:
            return [], offset, 0
        if skipped:
            nl = data.find(b'\n')
            if nl < 0:
                return [], start, skipped
            skipped += nl + 1
            start += nl + 1
            data = data[nl + 1:]
        end = data.rfind(b'\n')
        if end < 0:
            return [], start, skipped
        chunk = data[:end + 1]
        lines = chunk.decode('utf-8', errors='replace').splitlines()
        return lines, start + len(chunk), skipped

    @classmethod
    def read_new_lines(cls, path, cursor):
        """New lines of one log since ``cursor`` (``{'ino', 'off'}`` or None).

        Returns ``(lines, new_cursor, skipped_bytes)``. ``new_cursor`` is None
        when the log does not exist.
        """
        try:
            st = os.stat(path)
        except OSError:
            return [], None, 0
        lines = []
        skipped = 0
        offset = 0
        if cursor:
            if cursor.get('ino') == st.st_ino:
                offset = int(cursor.get('off') or 0)
                if st.st_size < offset:      # copytruncate
                    offset = 0
            else:
                # Rotated: finish the old file if it is still at <log>.1.
                rotated = path + '.1'
                try:
                    if os.stat(rotated).st_ino == cursor.get('ino'):
                        old, _off, skip = cls._read_from(
                            rotated, int(cursor.get('off') or 0))
                        lines.extend(old)
                        skipped += skip
                except OSError:
                    pass
        new, new_off, skip = cls._read_from(path, offset)
        lines.extend(new)
        skipped += skip
        return lines, {'ino': st.st_ino, 'off': new_off}, skipped

    # ------------------------------------------------------------------ #
    # Ingest
    # ------------------------------------------------------------------ #

    @classmethod
    def rollup(cls, lines):
        """Lines → ``{(level, bucket): acc}`` plus an unparsable count."""
        out = {}
        bad = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            rec = parse_line(line)
            if rec is None:
                bad += 1
                continue
            for level in ('minute', 'hour'):
                key = (level, _floor(rec['time'], level))
                acc = out.get(key)
                if acc is None:
                    acc = out[key] = _empty_acc()
                _add_line(acc, rec)
        return out, bad

    @classmethod
    def _merge_into_db(cls, app_id, rolled):
        from app.models.app_request_metric import AppRequestMetric

        if not rolled:
            return 0
        buckets = {b for (_l, b) in rolled}
        existing = {
            (row.level, row.bucket): row
            for row in AppRequestMetric.query.filter(
                AppRequestMetric.app_id == app_id,
                AppRequestMetric.bucket.in_(buckets)).all()
        }
        for (level, bucket), acc in rolled.items():
            row = existing.get((level, bucket))
            if row is None:
                row = AppRequestMetric(app_id=app_id, level=level, bucket=bucket)
                for key in _COUNTERS:
                    setattr(row, key, 0)
                row.latency_sum_ms = 0.0
                db.session.add(row)
            for key in _COUNTERS:
                setattr(row, key, int(getattr(row, key) or 0) + acc[key])
            row.latency_sum_ms = float(row.latency_sum_ms or 0) + acc['latency_sum_ms']
            hist = row.get_hist()
            if len(hist) != HIST_LEN:
                hist = [0] * HIST_LEN
            row.set_hist([a + b for a, b in zip(hist, acc['hist'])])
        return len(rolled)

    @classmethod
    def prune(cls, now=None):
        from app.models.app_request_metric import AppRequestMetric

        now = now or datetime.utcnow()
        pruned = AppRequestMetric.query.filter(
            AppRequestMetric.level == 'minute',
            AppRequestMetric.bucket < now - timedelta(hours=MINUTE_RETENTION_HOURS),
        ).delete(synchronize_session=False)
        pruned += AppRequestMetric.query.filter(
            AppRequestMetric.level == 'hour',
            AppRequestMetric.bucket < now - timedelta(days=HOUR_RETENTION_DAYS),
        ).delete(synchronize_session=False)
        return int(pruned or 0)

    @classmethod
    def sample(cls, log_dir=None, now=None):
        """One tick: read every live app's new log lines, roll up, prune."""
        from app.models.application import Application
        from app.models.system_settings import SystemSettings
        from app.services.nginx_service import NginxService

        log_dir = log_dir or NginxService.LOG_DIR
        cursors = SystemSettings.get(CURSOR_SETTING_KEY) or {}
        if not isinstance(cursors, dict):
            cursors = {}
        new_cursors = {}
        stats = {'apps': 0, 'lines': 0, 'unparsed': 0, 'rows': 0,
                 'skipped_bytes': 0}

        for app_row in Application.query_active().all():
            path = os.path.join(log_dir, f'{app_row.name}.access.log')
            lines, cursor, skipped = cls.read_new_lines(path, cursors.get(path))
            if cursor is None:
                continue
            new_cursors[path] = cursor
            stats['apps'] += 1
            stats['skipped_bytes'] += skipped
            if not lines:
                continue
            rolled, bad = cls.rollup(lines)
            stats['lines'] += len(lines)
            stats['unparsed'] += bad
            stats['rows'] += cls._merge_into_db(app_row.id, rolled)

        stats['pruned'] = cls.prune(now)
        # Only live apps' cursors are kept, so a deleted app's entry goes too.
        if new_cursors != cursors:
            SystemSettings.set(CURSOR_SETTING_KEY, new_cursors, value_type='json',
                               description='Request-metrics log offsets (plan 86 §A2)')
        db.session.commit()
        if stats['skipped_bytes']:
            logger.info('request metrics skipped %d backlog bytes',
                        stats['skipped_bytes'])
        return stats

    # ------------------------------------------------------------------ #
    # Read side
    # ------------------------------------------------------------------ #

    @staticmethod
    def live_app(app_id):
        """The live (not soft-deleted) application, or None."""
        from app.models.application import Application
        return Application.query_active().filter_by(id=app_id).first()

    @classmethod
    def series(cls, app_id, period='24h', now=None):
        """Zero-filled series plus window totals for one app.

        Hit ratio counts STALE/UPDATING as served from cache and leaves BYPASS
        out: a bypassed request was never cacheable.
        """
        from app.models.app_request_metric import AppRequestMetric

        if period not in PERIODS:
            from app.exceptions import ValidationError
            raise ValidationError(f'period must be one of {", ".join(PERIODS)}')
        level, span = PERIODS[period]
        now = now or datetime.utcnow()
        step = timedelta(hours=1) if level == 'hour' else timedelta(minutes=1)
        end = _floor(now, level)
        start = end - span + step

        rows = {
            row.bucket: row
            for row in AppRequestMetric.query.filter(
                AppRequestMetric.app_id == int(app_id),
                AppRequestMetric.level == level,
                AppRequestMetric.bucket >= start,
                AppRequestMetric.bucket <= end).all()
        }

        totals = _empty_acc()
        points = []
        at = start
        while at <= end:
            row = rows.get(at)
            hist = row.get_hist() if row else []
            if len(hist) != HIST_LEN:
                hist = [0] * HIST_LEN
            point = {'t': at.replace(tzinfo=timezone.utc).isoformat()}
            for key in ('requests', 'status_4xx', 'status_5xx'):
                point[key] = int(getattr(row, key) or 0) if row else 0
            point['p95_ms'] = percentile(hist, 0.95)
            points.append(point)
            if row:
                for key in _COUNTERS:
                    totals[key] += int(getattr(row, key) or 0)
                totals['latency_sum_ms'] += float(row.latency_sum_ms or 0)
                totals['hist'] = [a + b for a, b in zip(totals['hist'], hist)]
            at += step

        cached = totals['cache_hit'] + totals['cache_stale']
        cacheable = cached + totals['cache_miss']
        timed = totals['timed_requests']
        summary = {k: totals[k] for k in _COUNTERS}
        summary.update({
            'error_rate': (totals['status_5xx'] / totals['requests']
                           if totals['requests'] else None),
            'avg_ms': totals['latency_sum_ms'] / timed if timed else None,
            'p50_ms': percentile(totals['hist'], 0.50),
            'p95_ms': percentile(totals['hist'], 0.95),
            'p99_ms': percentile(totals['hist'], 0.99),
            'cache_hit_ratio': cached / cacheable if cacheable else None,
        })
        return {'app_id': int(app_id), 'period': period, 'level': level,
                'points': points, 'summary': summary}
