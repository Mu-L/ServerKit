"""Crash-loop visibility (plan 86 §E1): restart counts over a window, one
incident per loop, resolved after a quiet window."""
from datetime import datetime, timedelta

import pytest

from app import db
from app.models.status_page import StatusIncident
from app.services import crash_loop_service as cls_mod
from app.services.crash_loop_service import CrashLoopService
from tests.factories import make_application

T0 = datetime(2026, 7, 3, 12, 0)


@pytest.fixture
def restarts(app, monkeypatch):
    counts = {}
    monkeypatch.setattr(CrashLoopService, 'restart_counts',
                        staticmethod(lambda app_row: dict(counts.get(app_row.id, {}))))
    return counts


def _tick(minutes):
    return CrashLoopService.sweep(now=T0 + timedelta(minutes=minutes))


def test_three_restarts_in_ten_minutes_open_one_incident(restarts):
    site = make_application(db, name='flappy', status='running')
    for minute, count in ((0, 0), (1, 1), (2, 2)):
        restarts[site.id] = {'flappy': count}
        assert _tick(minute)['opened'] == []
    restarts[site.id] = {'flappy': 3}
    assert _tick(3)['opened'] == [site.id]
    restarts[site.id] = {'flappy': 5}
    assert _tick(4)['opened'] == [], 'one incident per loop'
    (incident,) = StatusIncident.query.all()
    assert incident.title == 'flappy is crash-looping' and incident.status == 'investigating'
    assert CrashLoopService.state_for(site)['looping'] is True


def test_a_quiet_window_resolves_it(restarts):
    site = make_application(db, name='flappy', status='running')
    for minute, count in ((0, 0), (1, 3)):
        restarts[site.id] = {'flappy': count}
        _tick(minute)
    assert _tick(6)['resolved'] == [], 'a restart inside the window keeps it open'
    assert _tick(12)['resolved'] == [site.id]
    (incident,) = StatusIncident.query.all()
    assert incident.status == 'resolved'
    assert CrashLoopService.state_for(site)['looping'] is False


def test_slow_restarts_are_not_a_loop(restarts):
    site = make_application(db, name='steady', status='running')
    for minute, count in ((0, 0), (15, 1), (30, 2), (45, 3)):
        restarts[site.id] = {'steady': count}
        assert _tick(minute)['opened'] == []
    assert StatusIncident.query.count() == 0


def test_workers_count_too(restarts):
    site = make_application(db, name='web', status='running')
    restarts[site.id] = {'web': 0, f'serverkit-app-{site.id}-worker': 0}
    _tick(0)
    restarts[site.id] = {'web': 0, f'serverkit-app-{site.id}-worker': 4}
    assert _tick(2)['opened'] == [site.id]


def test_state_holds_only_live_apps_and_recent_samples(restarts):
    site = make_application(db, name='web', status='running')
    restarts[site.id] = {'web': 0}
    _tick(0)
    _tick(30)
    samples = CrashLoopService.state_for(site)['samples']['web']
    assert len(samples) == 1, 'samples older than the window are dropped'
    site.status = 'stopped'
    db.session.commit()
    _tick(31)
    assert CrashLoopService.state_for(site) == {}


def test_the_sweep_is_a_builtin_tick():
    from app.jobs.builtin_handlers import _BUILTINS
    (row,) = [b for b in _BUILTINS if b[2] == 'crash-loop']
    assert row[0] == 'builtin.crash_loop' and row[3] == 60
