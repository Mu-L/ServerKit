"""Bottleneck hints from measured signals (plan 86 §A5). Every signal is
scripted through the service's seams; nothing here touches Docker."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.bottleneck_hints_service import BottleneckHintsService as H

NOW = datetime(2026, 7, 3, 12, 0)


def _app(**kw):
    return SimpleNamespace(**{'id': 1, 'name': 'shop', 'container_id': None,
                              'micro_cache_enabled': False, **kw})


def _metrics(p95=1500, requests=1000, ratio=None, points=()):
    return {'summary': {'requests': requests, 'p95_ms': p95, 'cache_hit_ratio': ratio},
            'points': list(points)}


@pytest.fixture
def signals(monkeypatch):
    state = {'metrics': _metrics(), 'cpu': {}, 'dbs': [], 'share': None, 'windows': [],
             'loop': {}}
    monkeypatch.setattr(H, '_crash_loop', staticmethod(lambda app: state['loop']))
    monkeypatch.setattr(H, '_metrics', staticmethod(lambda app: state['metrics']))
    monkeypatch.setattr(H, 'cpu_percent', staticmethod(lambda name: state['cpu'].get(name)))
    monkeypatch.setattr(H, '_databases', staticmethod(lambda app: state['dbs']))
    monkeypatch.setattr(H, '_top_query_share', staticmethod(lambda db_app, engine: state['share']))
    monkeypatch.setattr(H, '_deploy_windows', staticmethod(lambda app, since: state['windows']))
    return state


def _ids(app=None):
    return [h['id'] for h in H.hints(app or _app(), now=NOW)]


def test_a_busy_app_with_an_idle_database_points_at_the_page_cache(signals):
    signals['cpu'] = {'shop': 95, 'pg': 5}
    signals['dbs'] = [(SimpleNamespace(name='pg'), {'protocol': 'postgresql'})]
    (hint,) = H.hints(_app(), now=NOW)
    assert hint['id'] == 'app_bound'
    assert hint['action']['target'] == 'settings/cache'
    assert 'not offered' in hint['hint'], 'never suggests replicas'
    assert hint['failure_mode']


def test_a_busy_database_with_a_dominant_query_points_at_a_cache_or_index(signals):
    signals['cpu'] = {'shop': 30, 'pg': 92}
    signals['dbs'] = [(SimpleNamespace(name='pg'), {'protocol': 'postgresql'})]
    signals['share'] = 0.7
    (hint,) = H.hints(_app(), now=NOW)
    assert hint['id'] == 'database_bound'
    assert '70%' in hint['signal'] and 'index' in hint['hint']


def test_fast_apps_get_no_performance_hint(signals):
    signals['metrics'] = _metrics(p95=120)
    signals['cpu'] = {'shop': 99}
    assert _ids() == []


def test_too_little_traffic_says_nothing(signals):
    signals['metrics'] = _metrics(requests=20)
    signals['cpu'] = {'shop': 99}
    assert _ids() == []


def test_errors_inside_a_deploy_window_point_at_slot_deploys(signals):
    signals['metrics'] = _metrics(p95=50, points=[
        {'t': '2026-07-03T11:00:00+00:00', 'status_5xx': 4},
        {'t': '2026-07-03T09:00:00+00:00', 'status_5xx': 1},
    ])
    signals['windows'] = [(NOW - timedelta(hours=1, minutes=2), NOW - timedelta(minutes=55))]
    (hint,) = H.hints(_app(), now=NOW)
    assert hint['id'] == 'deploy_errors' and hint['signal'].startswith('4 server errors')


def test_errors_outside_deploys_are_not_blamed_on_deploys(signals):
    signals['metrics'] = _metrics(p95=50, points=[{'t': '2026-07-03T09:00:00+00:00', 'status_5xx': 3}])
    signals['windows'] = [(NOW - timedelta(minutes=10), NOW - timedelta(minutes=5))]
    assert _ids() == []


def test_a_low_hit_ratio_only_matters_with_the_micro_cache_on(signals):
    signals['metrics'] = _metrics(p95=50, ratio=0.1)
    assert _ids() == []
    assert _ids(_app(micro_cache_enabled=True)) == ['low_hit_ratio']


def test_the_app_cpu_falls_back_through_its_container_names(signals):
    signals['cpu'] = {'serverkit-app-1': 90}
    assert _ids() == ['app_bound']


def test_api(client, auth_headers, monkeypatch):
    from tests.factories import make_application
    from app import db
    site = make_application(db, name='hinted')
    monkeypatch.setattr(H, 'hints', classmethod(lambda cls, app, now=None: [{'id': 'x'}]))
    resp = client.get(f'/api/v1/apps/{site.id}/hints', headers=auth_headers)
    assert resp.status_code == 200 and resp.get_json() == {'hints': [{'id': 'x'}]}


def test_databases_are_the_sql_engines_the_app_references(app, tmp_path, monkeypatch):
    import json
    from app import db
    from app.models.deployment import Deployment
    from app.services.env_service import EnvService
    from app.services.template_service import TemplateService
    from tests.factories import make_application
    records = {}
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'repos': [], 'installed': records}))
    rows = {}
    for name, template_id in (('pg', 'postgresql'), ('cache', 'redis'), ('web', None)):
        root = tmp_path / name
        root.mkdir()
        if template_id:
            (root / '.serverkit-template.json').write_text(json.dumps(
                {'template_id': template_id, 'variables': {'DB_PASSWORD': 'x'}}))
        rows[name] = make_application(db, name=name, root_path=str(root))
        if template_id:
            records[str(rows[name].id)] = {'template_id': template_id}
    for key, svc in (('DATABASE_URL', 'pg'), ('REDIS_URL', 'cache')):
        EnvService.set_env_reference(rows['web'].id, key,
                                     {'kind': 'service', 'service': svc, 'property': 'url'})

    found = H._databases(rows['web'])
    assert [(a.name, e['protocol']) for a, e in found] == [('pg', 'postgresql')]

    started = datetime.utcnow() - timedelta(hours=1)
    db.session.add(Deployment(app_id=rows['web'].id, version=1, status='success',
                              deploy_started_at=started,
                              deploy_completed_at=started + timedelta(minutes=2)))
    db.session.commit()
    (window,) = H._deploy_windows(rows['web'], datetime.utcnow() - timedelta(hours=24))
    assert window == (started, started + timedelta(minutes=2) + timedelta(minutes=5))


def test_every_hint_carries_params_for_a_translated_ui(signals):
    signals['cpu'] = {'shop': 95}
    (hint,) = H.hints(_app(), now=NOW)
    assert hint['params'] == {'p95_ms': 1500, 'cpu': 95}


def test_a_crash_loop_comes_first_even_without_traffic(signals):
    signals['metrics'] = _metrics(requests=0)
    signals['loop'] = {'looping': True, 'restarts_in_window': 5}
    (hint,) = H.hints(_app(), now=NOW)
    assert hint['id'] == 'crash_loop' and hint['params'] == {'restarts': 5}
