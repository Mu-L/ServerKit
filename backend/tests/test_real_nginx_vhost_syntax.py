"""Generated vhosts through the REAL nginx parser (plan 82 §E).

scripts/test/test_nginx_conf.sh runs `nginx -t` on the SHIPPED static
vhosts; nothing ever fed NginxService.render_site_config's GENERATED output
back through the real parser, so a template edit nginx rejects only failed
on a user's box at reload time. Each rendered flavor is wrapped in a minimal
nginx.conf and parsed with `nginx -t`.

Read-only for the system (everything under tmp_path); still gated with the
other real-binaries tests for one consistent selection story. Run with:

    SERVERKIT_REAL_BINARIES=1 pytest tests -m real_binaries
"""
import os
import platform
import shutil
import subprocess

import pytest

from app.services.nginx_service import NginxService


NGINX = (shutil.which('nginx')
         or next((p for p in ('/usr/sbin/nginx', '/usr/local/sbin/nginx')
                  if os.path.exists(p)), None))

pytestmark = [
    pytest.mark.real_binaries,
    pytest.mark.skipif(platform.system() != 'Linux',
                       reason='real nginx needs Linux'),
    pytest.mark.skipif(NGINX is None, reason='nginx binary not installed'),
    pytest.mark.skipif(os.environ.get('SERVERKIT_REAL_BINARIES') != '1',
                       reason='real-binaries leg; opt in with '
                              'SERVERKIT_REAL_BINARIES=1'),
]


def _localize(text, tmp_path):
    """Point the config's absolute system paths at *tmp_path*.

    `nginx -t` opens every access_log/error_log for writing and binds the
    listen sockets; the test user can neither write /var/log/nginx (nor
    /var/cache/nginx) nor bind privileged ports. Only path strings and
    port numbers are swapped — every directive still goes through the
    real parser.
    """
    return (text
            .replace('/var/log/nginx', f'{tmp_path}/log')
            .replace('/var/cache/nginx', f'{tmp_path}/cache')
            .replace('listen 80;', 'listen 18080;')
            .replace('listen [::]:80;', 'listen [::]:18080;')
            .replace('listen 443', 'listen 18443')
            .replace('listen [::]:443', 'listen [::]:18443'))


def _nginx_t(tmp_path, vhost_config):
    """`nginx -t` over a minimal wrapper conf that includes *vhost_config*.

    The wrapper stands in for the parts of a real deployment the vhost
    relies on: writable log/cache dirs, the fastcgi_params file that
    `include fastcgi_params;` resolves against the conf prefix (= tmp_path
    under `-c`), and the http-level micro-cache zones ServerKit installs
    and timed log format ServerKit installs as conf.d snippets.
    """
    (tmp_path / 'log').mkdir(exist_ok=True)
    # nginx -t mkdirs the *_cache_path leaf dirs itself, but not parents.
    (tmp_path / 'cache' / 'serverkit-microcache').mkdir(
        parents=True, exist_ok=True)
    system_params = '/etc/nginx/fastcgi_params'
    (tmp_path / 'fastcgi_params').write_text(
        open(system_params).read() if os.path.exists(system_params) else '')
    zones = tmp_path / 'serverkit-microcache.conf'
    zones.write_text(
        _localize(NginxService.MICROCACHE_ZONE_SNIPPET, tmp_path)
        + NginxService.WORDPRESS_RATE_LIMIT_ZONE_SNIPPET
        + NginxService.TIMED_LOG_FORMAT_SNIPPET
    )
    vhost = tmp_path / 'vhost.conf'
    vhost.write_text(_localize(vhost_config, tmp_path))
    wrapper = tmp_path / 'nginx.conf'
    wrapper.write_text(
        f'pid {tmp_path}/nginx.pid;\n'
        f'error_log {tmp_path}/error.log;\n'
        'events {}\n'
        'http {\n'
        f'    access_log {tmp_path}/access.log;\n'
        f'    client_body_temp_path {tmp_path}/body;\n'
        f'    proxy_temp_path {tmp_path}/proxy;\n'
        f'    fastcgi_temp_path {tmp_path}/fastcgi;\n'
        f'    uwsgi_temp_path {tmp_path}/uwsgi;\n'
        f'    scgi_temp_path {tmp_path}/scgi;\n'
        f'    include {zones};\n'
        f'    include {vhost};\n'
        '}\n')
    return subprocess.run([NGINX, '-t', '-c', str(wrapper)],
                          capture_output=True, text=True)


def _render(**kwargs):
    rendered = NginxService.render_site_config(**kwargs)
    assert rendered.get('success'), rendered
    return rendered['config']


@pytest.mark.parametrize('flavor,kwargs', [
    ('docker-proxy', dict(name='shop', app_type='docker',
                          domains=['shop.example.com'], port=8003)),
    ('static', dict(name='blog', app_type='static',
                    domains=['blog.example.com'], root_path='/var/www/blog')),
    ('php', dict(name='wp', app_type='php',
                 domains=['wp.example.com'], root_path='/var/www/wp')),
    ('docker-micro-cache', dict(name='fast', app_type='docker',
                                domains=['fast.example.com'], port=8004,
                                micro_cache=True)),
    ('wordpress-proxy', dict(name='wp', app_type='docker',
                             domains=['wp.example.com'], port=8005,
                             wordpress_protection=True)),
])
def test_generated_vhost_parses_with_real_nginx(tmp_path, flavor, kwargs):
    proc = _nginx_t(tmp_path, _render(**kwargs))
    assert proc.returncode == 0, (
        f'{flavor}: real nginx rejected the generated vhost:\n{proc.stderr}')


def test_the_harness_itself_rejects_garbage(tmp_path):
    """A wrapper that passed everything would make the suite vacuous."""
    proc = _nginx_t(tmp_path, 'server { this is not nginx syntax }')
    assert proc.returncode != 0
