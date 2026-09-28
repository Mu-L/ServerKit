"""Per-site micro-cache toggle (task #21) — renderer, zone snippet, kwargs
plumbing, API, and drift consistency.

Proving points:
- render_site_config(micro_cache=True) emits the cache directives + every
  bypass condition (method/query/cookies/paths) and the X-SK-Cache header;
  disabled renders are byte-identical to before the feature
- fastcgi_cache for PHP sites, proxy_cache for proxied ones, no-op for static
- only ONE proxy_cache_bypass directive (folded into the template's
  $http_upgrade bypass — nginx rejects duplicates)
- the conf.d zone snippet is written once and is idempotent
- SiteDomainService.app_vhost_kwargs carries micro_cache from the app row, so
  the write path and the drift re-render agree (an enabled cache is never
  reported as drift)
- PUT /apps/<id>/micro-cache saves + republishes when the app has domains,
  save-only note when not; POST .../purge wipes the shared cache dir
  (Linux-guarded); both require auth
"""
import shutil
import os
import pytest

from app import db
from app.models import Application, User
from app.models.domain import Domain
from app.services.nginx_service import NginxService
from app.services.site_domain_service import SiteDomainService
from subprocess_stub import FakeProc


BYPASS_MARKERS = [
    'set $sk_skip_cache 0;',
    '$request_method !~ ^(GET|HEAD)$',        # non-GET/HEAD
    '$query_string != ""',                    # any query string
    'wordpress_logged_in_',                   # cookie bypasses
    'wp-postpass',
    'woocommerce_cart_hash',
    'woocommerce_items_in_cart',
    'comment_author',
    'PHPSESSID',
    '^/(wp-admin|wp-login|admin|login|cart|checkout|my-account)',  # path bypasses
    'add_header X-SK-Cache $upstream_cache_status;',
]


# ── renderer ─────────────────────────────────────────────────────────────────

def test_php_site_gets_fastcgi_cache_with_all_bypasses():
    res = NginxService.render_site_config(
        'blog', 'php', ['blog.lvh.me'], root_path='/srv/blog', micro_cache=True)
    assert res['success'] is True
    cfg = res['config']
    assert 'fastcgi_cache serverkit_microcache_php;' in cfg
    assert 'fastcgi_cache_valid 200 301 10s;' in cfg
    assert 'fastcgi_cache_key $scheme://$host$request_uri;' in cfg
    assert ('fastcgi_cache_use_stale error timeout updating http_500 http_503;'
            in cfg)
    assert 'fastcgi_cache_background_update on;' in cfg
    assert 'fastcgi_cache_lock on;' in cfg
    assert 'fastcgi_cache_bypass $sk_skip_cache;' in cfg
    assert 'fastcgi_no_cache $sk_skip_cache;' in cfg
    for marker in BYPASS_MARKERS:
        assert marker in cfg, marker
    # PHP path never references the proxy zone.
    assert 'proxy_cache serverkit_microcache;' not in cfg


def test_docker_site_gets_proxy_cache_with_folded_bypass():
    res = NginxService.render_site_config(
        'shop', 'docker', ['shop.lvh.me'], port=8300, micro_cache=True)
    assert res['success'] is True
    cfg = res['config']
    assert 'proxy_cache serverkit_microcache;' in cfg
    assert 'proxy_cache_valid 200 301 10s;' in cfg
    # Keyed on the site's host (the proxy default is the upstream port), so
    # one site's entries can be purged alone (plan 86 §B3).
    assert 'proxy_cache_key $scheme://$host$request_uri;' in cfg
    assert ('proxy_cache_use_stale error timeout updating http_500 http_502 '
            'http_503;' in cfg)
    assert 'proxy_cache_background_update on;' in cfg
    assert 'proxy_cache_lock on;' in cfg
    assert 'proxy_no_cache $sk_skip_cache;' in cfg
    for marker in BYPASS_MARKERS:
        assert marker in cfg, marker
    # nginx rejects duplicate proxy_cache_bypass directives — the skip var is
    # folded into the template's existing $http_upgrade bypass.
    assert cfg.count('proxy_cache_bypass') == 1
    assert 'proxy_cache_bypass $sk_skip_cache $http_upgrade;' in cfg


def test_python_site_gets_proxy_cache():
    res = NginxService.render_site_config(
        'api', 'flask', ['api.lvh.me'], root_path='/srv/api', port=5001, micro_cache=True)
    assert res['success'] is True
    assert 'proxy_cache serverkit_microcache;' in res['config']


def test_disabled_render_is_unchanged():
    plain = NginxService.render_site_config('shop', 'docker', ['shop.lvh.me'], port=8300)
    off = NginxService.render_site_config('shop', 'docker', ['shop.lvh.me'], port=8300,
                                          micro_cache=False)
    assert plain['config'] == off['config']
    for token in ('serverkit_microcache', 'sk_skip_cache', 'X-SK-Cache'):
        assert token not in plain['config']


def test_static_site_is_a_noop_even_when_enabled():
    on = NginxService.render_site_config('docs', 'static', ['docs.lvh.me'],
                                         root_path='/srv/docs', micro_cache=True)
    off = NginxService.render_site_config('docs', 'static', ['docs.lvh.me'],
                                          root_path='/srv/docs')
    assert on['config'] == off['config']
    assert 'serverkit_microcache' not in on['config']


def test_ssl_wrap_keeps_cache_in_app_block_only():
    res = NginxService.render_site_config(
        'shop', 'docker', ['shop.lvh.me'], port=8300, micro_cache=True,
        ssl_cert='/c/fullchain.pem', ssl_key='/c/privkey.pem')
    cfg = res['config']
    assert 'return 301 https://$server_name$request_uri;' in cfg
    assert cfg.count('proxy_cache serverkit_microcache;') == 1
    assert cfg.count('set $sk_skip_cache 0;') == 1
    # The skip block lives in the 443 app block, after the redirect block.
    assert cfg.index('return 301') < cfg.index('set $sk_skip_cache 0;')
    # Cache directives are scheme-agnostic — nothing forces HTTPS.
    assert 'listen 443 ssl http2;' in cfg


# ── zone snippet ─────────────────────────────────────────────────────────────

def test_zone_snippet_declares_shared_zones():
    snippet = NginxService.MICROCACHE_ZONE_SNIPPET
    assert 'keys_zone=serverkit_microcache:10m' in snippet
    assert 'keys_zone=serverkit_microcache_php:10m' in snippet
    assert 'max_size=256m' in snippet
    assert 'inactive=10m' in snippet
    assert snippet.count('/var/cache/nginx/serverkit-microcache') == 2


def _tee_to_disk(fake):
    """Make the scripted `tee` actually write, so idempotence is observable.

    Scripted through the shared seam (plan 75 §G7) rather than by patching
    `nginx_service.run_privileged`: the write now goes through
    `write_privileged_file`, and a stub pinned to one module's imported name
    would silently stop intercepting it — which is exactly how a test keeps a
    bypass route alive.
    """
    import os as _os

    def write(argv, kwargs):
        path = argv[2] if argv[1] == '-a' else argv[1]
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, 'a' if argv[1] == '-a' else 'w') as fh:
            fh.write(kwargs.get('input', ''))
        return FakeProc()

    fake.when(['tee'], write)
    return fake


def test_ensure_cache_zone_writes_once_and_is_idempotent(tmp_path, fake_subprocess, monkeypatch):
    conf_dir = tmp_path / 'nginx'
    monkeypatch.setattr(NginxService, 'NGINX_CONF_DIR', str(conf_dir))
    fake_subprocess.script(['mkdir'])
    _tee_to_disk(fake_subprocess)

    first = NginxService.ensure_cache_zone()
    assert first['success'] is True and first['changed'] is True
    conf_path = conf_dir / 'conf.d' / 'serverkit-microcache.conf'
    assert conf_path.read_text() == NginxService.MICROCACHE_ZONE_SNIPPET
    assert len([c for c in fake_subprocess.commands() if c[0] == 'tee']) == 1

    second = NginxService.ensure_cache_zone()
    assert second['success'] is True and second['changed'] is False
    assert len([c for c in fake_subprocess.commands() if c[0] == 'tee']) == 1  # no rewrite


def test_create_site_ensures_zone_when_enabled(monkeypatch):
    ensured = []
    writes = []
    monkeypatch.setattr(NginxService, 'ensure_cache_zone',
                        classmethod(lambda cls: ensured.append(True) or {'success': True}))
    monkeypatch.setattr(
        NginxService,
        'write_vhost',
        classmethod(lambda cls, name, content: writes.append((name, content)) or {
            'success': True,
            'path': f'/etc/nginx/sites-available/{name}',
        }),
    )

    res = NginxService.create_site('shop', 'docker', ['shop.lvh.me'],
                                   root_path=None, port=8300, micro_cache=True)
    assert res['success'] is True
    assert ensured == [True]
    assert writes[0][0] == 'shop'
    assert 'proxy_cache serverkit_microcache;' in writes[0][1]

    # Disabled writes never touch the zone.
    NginxService.create_site('shop', 'docker', ['shop.lvh.me'],
                             root_path=None, port=8300)
    assert ensured == [True]
    assert len(writes) == 2


# ── kwargs plumbing / drift consistency ──────────────────────────────────────

def _mk_owner(username='mcowner'):
    from werkzeug.security import generate_password_hash
    user = User(email=f'{username}@test.local', username=username,
                password_hash=generate_password_hash('x'),
                role=User.ROLE_ADMIN, is_active=True)
    db.session.add(user)
    db.session.commit()
    return user


def _mk_site(user_id, name='mcshop', host='mcshop.lvh.me', micro_cache=False, **kw):
    a = Application(name=name, app_type='docker', user_id=user_id, port=8300,
                    micro_cache_enabled=micro_cache, **kw)
    db.session.add(a)
    db.session.commit()
    if host:
        db.session.add(Domain(name=host, is_primary=True, application_id=a.id))
        db.session.commit()
    return a


def test_app_vhost_kwargs_carries_micro_cache_flag(app):
    user = _mk_owner()
    on = _mk_site(user.id, name='mc-on', host='mc-on.lvh.me', micro_cache=True)
    off = _mk_site(user.id, name='mc-off', host='mc-off.lvh.me', micro_cache=False)

    kwargs_on, warn = SiteDomainService.app_vhost_kwargs(on)
    assert warn is None
    assert kwargs_on['micro_cache'] is True

    kwargs_off, _ = SiteDomainService.app_vhost_kwargs(off)
    assert kwargs_off['micro_cache'] is False


def test_drift_expected_render_includes_micro_cache(app):
    """Drift renders the expected vhost through app_vhost_kwargs — with the
    flag on, the expected content carries the cache directives, so the file
    the write path produced compares in_sync."""
    from app.services.drift_service import _nginx_render_expected

    user = _mk_owner('mcdrift')
    site = _mk_site(user.id, name='mc-drift', host='mc-drift.lvh.me', micro_cache=True)

    expected = _nginx_render_expected(site.id)
    [(path, content)] = expected.items()
    assert path.endswith('mc-drift')
    assert 'proxy_cache serverkit_microcache;' in content
    assert 'set $sk_skip_cache 0;' in content

    # And it matches the renderer output for the same kwargs byte-for-byte
    # (same pipeline both ways).
    kwargs, _ = SiteDomainService.app_vhost_kwargs(site)
    assert content == NginxService.render_site_config(**kwargs)['config']


def test_model_round_trip_and_to_dict(app):
    user = _mk_owner('mcmodel')
    site = _mk_site(user.id, name='mc-model', host=None, micro_cache=True)
    reloaded = Application.query.get(site.id)
    assert reloaded.micro_cache_enabled is True
    assert reloaded.to_dict()['micro_cache_enabled'] is True

    plain = _mk_site(user.id, name='mc-plain', host=None)
    assert plain.to_dict()['micro_cache_enabled'] is False


# ── API ──────────────────────────────────────────────────────────────────────

def _api_site(host='api-mc.lvh.me', name='api-mc'):
    owner = User.query.filter_by(username='testadmin').first()
    return _mk_site(owner.id, name=name, host=host)


def test_api_put_published_saves_and_rewrites_vhost(client, auth_headers, app, monkeypatch):
    site = _api_site()
    calls = []
    monkeypatch.setattr(SiteDomainService, 'write_app_vhost',
                        classmethod(lambda cls, a, force_type=None:
                                    calls.append(a.id) or {'nginx': {'success': True}, 'warning': None}))

    resp = client.put(f'/api/v1/apps/{site.id}/micro-cache', headers=auth_headers,
                      json={'enabled': True})
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data['micro_cache_enabled'] is True
    assert data['applied'] is True
    assert 'note' not in data
    assert calls == [site.id]
    assert Application.query.get(site.id).micro_cache_enabled is True

    # Toggle back off — vhost rewritten again without the directives.
    resp = client.put(f'/api/v1/apps/{site.id}/micro-cache', headers=auth_headers,
                      json={'enabled': False})
    assert resp.status_code == 200
    assert resp.get_json()['micro_cache_enabled'] is False
    assert calls == [site.id, site.id]
    assert Application.query.get(site.id).micro_cache_enabled is False


def test_api_put_unpublished_is_save_only(client, auth_headers, app, monkeypatch):
    site = _api_site(host=None, name='api-mc-nodom')
    monkeypatch.setattr(SiteDomainService, 'write_app_vhost',
                        classmethod(lambda cls, a, force_type=None:
                                    (_ for _ in ()).throw(AssertionError('must not write'))))

    resp = client.put(f'/api/v1/apps/{site.id}/micro-cache', headers=auth_headers,
                      json={'enabled': True})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['applied'] is False
    assert 'published' in data['note']
    assert Application.query.get(site.id).micro_cache_enabled is True


def test_api_put_surfaces_write_warning(client, auth_headers, app, monkeypatch):
    site = _api_site(name='api-mc-warn', host='api-mc-warn.lvh.me')
    monkeypatch.setattr(SiteDomainService, 'write_app_vhost',
                        classmethod(lambda cls, a, force_type=None:
                                    {'nginx': None, 'warning': 'docker app has no published port to route to.'}))

    resp = client.put(f'/api/v1/apps/{site.id}/micro-cache', headers=auth_headers,
                      json={'enabled': True})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['applied'] is False
    assert 'warning' in data
    # The flag is still saved even when the vhost write degraded.
    assert Application.query.get(site.id).micro_cache_enabled is True


def test_api_put_requires_enabled_key(client, auth_headers, app):
    site = _api_site(name='api-mc-bad', host=None)
    resp = client.put(f'/api/v1/apps/{site.id}/micro-cache', headers=auth_headers, json={})
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_api_404_for_missing_app(client, auth_headers, app):
    assert client.put('/api/v1/apps/999999/micro-cache', headers=auth_headers,
                      json={'enabled': True}).status_code == 404
    assert client.post('/api/v1/apps/999999/micro-cache/purge',
                       headers=auth_headers).status_code == 404


def test_api_requires_auth(client, app):
    assert client.put('/api/v1/apps/1/micro-cache', json={'enabled': True}).status_code == 401
    assert client.post('/api/v1/apps/1/micro-cache/purge').status_code == 401


def test_api_purge_clears_only_this_sites_hosts(client, auth_headers, app, monkeypatch):
    site = _api_site(name='api-mc-purge', host='purge-mc.lvh.me')
    seen = []
    monkeypatch.setattr(NginxService, 'purge_micro_cache',
                        classmethod(lambda cls, hosts=None: seen.append(hosts) or
                                    {'success': True, 'purged': 3, 'message': 'Cleared 3'}))
    resp = client.post(f'/api/v1/apps/{site.id}/micro-cache/purge', headers=auth_headers)
    assert resp.status_code == 200
    assert resp.get_json() == {'message': 'Cleared 3', 'purged': 3}
    assert seen == [['purge-mc.lvh.me']], 'purge must never fall back to every site'


def test_api_purge_without_a_domain_touches_nothing(client, auth_headers, app, monkeypatch):
    site = _api_site(name='api-mc-purge-nodom', host=None)
    monkeypatch.setattr(NginxService, 'purge_micro_cache',
                        classmethod(lambda cls, hosts=None: pytest.fail('purged')))
    resp = client.post(f'/api/v1/apps/{site.id}/micro-cache/purge', headers=auth_headers)
    assert resp.status_code == 200
    assert resp.get_json()['purged'] == 0


def test_api_put_sets_and_validates_the_ttl(client, auth_headers, app, monkeypatch):
    site = _api_site(host=None, name='api-mc-ttl')
    url = f'/api/v1/apps/{site.id}/micro-cache'
    ok = client.put(url, headers=auth_headers, json={'enabled': True, 'ttl': 60})
    assert ok.status_code == 200 and ok.get_json()['micro_cache_ttl'] == 60
    for bad in (0, NginxService.MICROCACHE_TTL_MAX + 1, '60', True, 1.5):
        resp = client.put(url, headers=auth_headers, json={'enabled': True, 'ttl': bad})
        assert resp.status_code == 400, bad
    assert Application.query.get(site.id).micro_cache_ttl == 60
    reset = client.put(url, headers=auth_headers, json={'enabled': True, 'ttl': None})
    assert reset.get_json()['micro_cache_ttl'] is None


def test_api_purge_failure_is_500_with_error(client, auth_headers, app, monkeypatch):
    site = _api_site(name='api-mc-purgefail', host='purgefail-mc.lvh.me')
    monkeypatch.setattr(NginxService, 'purge_micro_cache',
                        classmethod(lambda cls, hosts=None: {'success': False, 'error': 'boom'}))
    resp = client.post(f'/api/v1/apps/{site.id}/micro-cache/purge', headers=auth_headers)
    assert resp.status_code == 500
    assert resp.get_json() == {'error': 'boom'}


# ── purge service ────────────────────────────────────────────────────────────

def test_purge_wipes_and_recreates_cache_dirs(fake_subprocess, monkeypatch):
    import os as _os
    monkeypatch.setattr(_os, 'name', 'posix')   # force the Linux branch
    fake_subprocess.script(['rm'])
    fake_subprocess.script(['mkdir'])

    res = NginxService.purge_micro_cache()      # no hosts: every site
    assert res['success'] is True
    rm, mkdir = fake_subprocess.commands()
    assert rm[:2] == ['rm', '-rf']
    assert f'{NginxService.MICROCACHE_DIR}/proxy'.replace('/', _os.sep) in [p.replace('/', _os.sep) for p in rm]
    assert mkdir[:2] == ['mkdir', '-p']


def test_purge_is_linux_guarded(fake_subprocess, monkeypatch):
    import os as _os
    monkeypatch.setattr(_os, 'name', 'nt')
    # Nothing is scripted: any exec at all would raise UnscriptedCommand.
    res = NginxService.purge_micro_cache()
    assert res['success'] is False
    assert 'Linux' in res['error']
    assert fake_subprocess.commands() == []


# ── per-site purge + TTL (plan 86 §B3) ───────────────────────────────────────

@pytest.mark.parametrize('ttl,expected', [
    (None, '10s'), (60, '60s'), (0, '1s'), (99999, f'{NginxService.MICROCACHE_TTL_MAX}s'),
])
def test_ttl_is_rendered_and_clamped(ttl, expected):
    cfg = NginxService.render_site_config(
        'shop', 'docker', ['shop.lvh.me'], port=8300, micro_cache=True,
        micro_cache_ttl=ttl)['config']
    assert f'proxy_cache_valid 200 301 {expected};' in cfg


def test_ttl_cap_stays_below_the_zone_inactive_window():
    """A stale copy only survives `inactive`; a TTL above it would leave
    use_stale nothing to serve."""
    assert 'inactive=10m' in NginxService.MICROCACHE_ZONE_SNIPPET
    assert NginxService.MICROCACHE_TTL_MAX < 600


def test_app_vhost_kwargs_carry_the_ttl(app):
    site = _mk_site(_mk_owner().id, name='mc-ttl-kwargs', host='mc-ttl.lvh.me')
    site.micro_cache_ttl = 45
    kwargs, _warn = SiteDomainService.app_vhost_kwargs(site)
    assert kwargs['micro_cache_ttl'] == 45


@pytest.mark.skipif(shutil.which('grep') is None, reason='needs grep')
def test_per_site_purge_removes_only_that_hosts_files(tmp_path, monkeypatch):
    """Real grep over real cache-shaped files: nginx writes the key as a
    `KEY: ...` header line near the start of each cache file."""
    import subprocess as _sp
    monkeypatch.setattr(os, 'name', 'posix')
    monkeypatch.setattr(NginxService, 'MICROCACHE_DIR', str(tmp_path))
    monkeypatch.setattr('app.services.nginx_service.run_privileged',
                        lambda argv, **kw: _sp.run(argv, capture_output=True, text=True,
                                                   timeout=kw.get('timeout', 30)))
    entries = {
        'proxy/a/1/shop1': 'KEY: https://shop.lvh.me/\n',
        'proxy/a/2/shop2': 'KEY: http://shop.lvh.me/cart?x=1\n',
        'fastcgi/b/3/shop3': 'KEY: https://shop.lvh.me/blog\n',
        'proxy/c/4/other': 'KEY: https://blog.lvh.me/\n',
        'proxy/c/5/lookalike': 'KEY: https://shop.lvh.me.evil.test/\n',
    }
    for rel, key in entries.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\x05\x00binary-header\n' + key.encode() + b'\r\nbody')

    res = NginxService.purge_micro_cache(['shop.lvh.me'])

    assert res['success'] is True, res
    assert res['purged'] == 3
    left = sorted(p.name for p in tmp_path.rglob('*') if p.is_file())
    assert left == ['lookalike', 'other']


def test_per_site_purge_rejects_a_bad_host(monkeypatch, fake_subprocess):
    monkeypatch.setattr(os, 'name', 'posix')
    res = NginxService.purge_micro_cache(['shop.lvh.me; rm -rf /'])
    assert res['success'] is False
    assert fake_subprocess.commands() == []
