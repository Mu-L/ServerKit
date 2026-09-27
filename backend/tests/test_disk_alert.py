"""Plan 85 §D3: the disk warning is on by default and fires once per
crossing, not every tick."""
import pytest

from app.services import disk_alert_service as svc
from app.services.settings_service import SettingsService

GB = 1024 ** 3


@pytest.fixture()
def sent(monkeypatch):
    from app import plugins_sdk
    calls = []
    monkeypatch.setattr(plugins_sdk.notify, 'send',
                        lambda event, to, **kw: calls.append((event, kw.get('severity'),
                                                              kw['data']['percent'])))
    return calls


def test_crossings_notify_once_each_and_reset_below_the_band(app, sent):
    readings = [70, 86, 88, 96, 97, 83, 79, 86]
    levels = [svc.check(usage=(p, 3 * GB)) for p in readings]

    assert levels == ['', 'warning', 'warning', 'critical', 'critical',
                      'critical', '', 'warning']
    assert sent == [('storage.disk_low', 'warning', 86),
                    ('storage.disk_low', 'critical', 96),
                    ('storage.disk_low', 'warning', 86)]


def test_zero_disables_the_alert(app, sent):
    SettingsService.set(svc.PERCENT_SETTING, 0)
    assert svc.check(usage=(99, 0)) == ''
    assert sent == []


def test_the_event_is_in_the_catalog_and_links_somewhere(app):
    from app.notifications import catalog
    meta = catalog.resolve('storage.disk_low', data={'percent': 91})
    assert '91%' in meta['title']
    assert catalog.link_for('storage.disk_low')
