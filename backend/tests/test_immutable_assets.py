"""Opt-in long caching of fingerprinted assets (plan 86 §B2). The real-nginx
half (hashed names come back immutable, look-alikes do not) is in
test_real_nginx_vhost_syntax.py."""
import re

import pytest

from app import db
from app.models.domain import Domain
from app.services.nginx_service import NginxService
from app.services.site_domain_service import SiteDomainService
from tests.factories import make_application


def _render(**kw):
    return NginxService.render_site_config('shop', domains=['shop.lvh.me'], **kw)['config']


def test_off_by_default():
    assert 'immutable' not in _render(app_type='docker', port=8300)


def test_the_block_proxies_like_location_root_and_pins_the_asset():
    cfg = _render(app_type='docker', port=8300, immutable_assets=True)
    block = cfg[cfg.index('location ~* "'):]
    assert 'proxy_pass http://127.0.0.1:8300;' in block
    assert 'add_header Cache-Control "public, max-age=31536000, immutable";' in block
    assert 'expires' not in block, 'a second Cache-Control header would conflict'
    assert cfg.index('    location / {') < cfg.index('location ~* "')


def test_micro_cache_still_anchors_on_location_root():
    cfg = _render(app_type='docker', port=8300, immutable_assets=True, micro_cache=True)
    root = cfg[cfg.index('    location / {'):cfg.index('location ~* "')]
    assert 'proxy_cache serverkit_microcache;' in root
    assert cfg.count('proxy_cache serverkit_microcache;') == 1


@pytest.mark.parametrize('app_type,kw', [('static', {'root_path': '/srv/x'}),
                                          ('php', {'root_path': '/srv/x'})])
def test_only_proxied_sites_get_it(app_type, kw):
    assert 'immutable' not in _render(app_type=app_type, immutable_assets=True, **kw)


@pytest.mark.parametrize('path,pinned', [
    ('/assets/index-DQK8aLC1.js', True),
    ('/static/js/main.3f2a9b1c.chunk.js', False),   # hash not right before the extension
    ('/static/css/main.3f2a9b1c.css', True),
    ('/fonts/inter-4f8b2c1d.woff2', True),
    ('/assets/app-settings.js', False),
    ('/assets/abcdefgh.js', False),                 # 8 letters, no digit: not a hash
    ('/assets/logo.svg', False),
    ('/app.js', False),
])
def test_the_pattern(path, pinned):
    assert bool(re.search(NginxService.IMMUTABLE_ASSET_PATTERN, path, re.I)) is pinned


def test_the_flag_rides_the_shared_vhost_kwargs(app):
    site = make_application(db, name='assets-kw', port=8300)
    db.session.add(Domain(name='assets-kw.lvh.me', application_id=site.id, is_primary=True))
    site.immutable_assets = True
    db.session.commit()
    kwargs, _warn = SiteDomainService.app_vhost_kwargs(site)
    assert kwargs['immutable_assets'] is True


def test_api(client, auth_headers, monkeypatch, app):
    site = make_application(db, name='assets-api', port=8300)
    monkeypatch.setattr(SiteDomainService, 'write_app_vhost',
                        classmethod(lambda cls, a, force_type=None: {'nginx': {'success': True}}))
    url = f'/api/v1/apps/{site.id}/immutable-assets'
    resp = client.put(url, json={'enabled': True}, headers=auth_headers)
    assert resp.status_code == 200 and resp.get_json()['immutable_assets'] is True
    assert client.put(url, json={'enabled': 1}, headers=auth_headers).status_code == 400
