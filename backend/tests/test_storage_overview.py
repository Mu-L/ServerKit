"""Plan 85 §D1: the Storage page's read-only overview."""
import json

import pytest

from app.services import storage_overview_service as svc


@pytest.mark.parametrize('text,expected', [
    ('9.308GB', 9_308_000_000), ('81.92kB', 81_920), ('0B', 0),
    ('433.6MB (37%)', 433_600_000), ('', None), ('n/a', None),
])
def test_docker_sizes_parse(text, expected):
    assert svc.parse_docker_size(text) == expected


def test_docker_usage_reads_system_df(monkeypatch):
    from app.services import disk_reclaim_service
    lines = '\n'.join(json.dumps(row) for row in (
        {'Type': 'Images', 'Size': '9.308GB', 'Reclaimable': '1.2GB (12%)'},
        {'Type': 'Local Volumes', 'Size': '1.167GB', 'Reclaimable': '433.6MB (37%)'},
        {'Type': 'Build Cache', 'Size': '1.204GB', 'Reclaimable': '1.204GB'},
    ))
    monkeypatch.setattr(disk_reclaim_service, '_run', lambda *a, **k: (True, lines, ''))
    usage = svc.docker_usage()
    assert usage['images'] == {'bytes': 9_308_000_000, 'reclaimable': 1_200_000_000}
    assert usage['build_cache']['bytes'] == 1_204_000_000


def test_no_docker_is_an_empty_breakdown_not_an_error(monkeypatch):
    from app.services import disk_reclaim_service
    monkeypatch.setattr(disk_reclaim_service, '_run', lambda *a, **k: (False, '', 'not found'))
    assert svc.docker_usage() == {}


def test_overview_endpoint_is_admin_only_and_reports_retention(client, auth_headers, monkeypatch):
    from app.services import disk_reclaim_service
    monkeypatch.setattr(disk_reclaim_service, '_run', lambda *a, **k: (False, '', ''))

    assert client.get('/api/v1/system/storage').status_code == 401
    resp = client.get('/api/v1/system/storage', headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['profile'] in ('small', 'standard')
    assert set(svc.RETENTION_KEYS) <= set(body['retention'])
    assert body['disk']['total'] and isinstance(body['items'], list)
