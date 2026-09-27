"""Global gzip/brotli snippet (plan 86 §B1).

Stock nginx configs already set some compression directives (Debian ships
`gzip on;` in nginx.conf), and repeating an http-level directive is fatal to
`nginx -t`. The snippet therefore carries only what the running config lacks,
read from `nginx -T`. Its real-parser leg is in test_real_nginx_vhost_syntax.
"""
import pytest

from app.services.nginx_service import NginxService

OURS = '/etc/nginx/conf.d/serverkit-compression.conf'

DEBIAN_DUMP = """nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
# configuration file /etc/nginx/nginx.conf:
user www-data;
include /etc/nginx/modules-enabled/*.conf;
events {
\tworker_connections 768;
}
http {
\tsendfile on;
\tgzip on;
\t# gzip_vary on;
\t# gzip_types text/plain text/css;
\tinclude /etc/nginx/conf.d/*.conf;
\tinclude /etc/nginx/sites-enabled/*;
}

# configuration file /etc/nginx/sites-enabled/shop:
server {
    listen 80;
    gzip_types text/plain;
    location / {
        gzip_vary off;
    }
}
"""


def _names(snippet):
    return [line.split()[0] for line in snippet.splitlines()
            if line and not line.startswith('#')]


def test_debian_gzip_on_is_not_repeated():
    snippet = NginxService.render_compression_snippet(DEBIAN_DUMP, OURS)
    names = _names(snippet)
    assert 'gzip' not in names
    assert names == ['gzip_vary', 'gzip_proxied', 'gzip_comp_level',
                     'gzip_min_length', 'gzip_types']
    assert '# Already set elsewhere, left alone: gzip' in snippet


def test_commented_and_server_level_directives_do_not_count():
    """`# gzip_vary on;` and a server block's gzip_types are not http level."""
    present = NginxService._http_level_directives(DEBIAN_DUMP, OURS)
    assert 'gzip' in present
    assert 'gzip_vary' not in present
    assert 'gzip_types' not in present


def test_a_conf_d_file_setting_gzip_types_is_respected():
    dump = DEBIAN_DUMP + (
        '\n# configuration file /etc/nginx/conf.d/tuning.conf:\n'
        'gzip_types text/plain;\n')
    assert 'gzip_types' not in _names(
        NginxService.render_compression_snippet(dump, OURS))


def test_our_own_snippet_is_ignored_on_a_rewrite():
    dump = DEBIAN_DUMP + (
        f'\n# configuration file {OURS}:\n'
        'gzip_vary on;\ngzip_types text/plain;\n')
    assert 'gzip_vary' in _names(
        NginxService.render_compression_snippet(dump, OURS))


def test_a_config_without_gzip_gets_the_full_set():
    dump = ('# configuration file /etc/nginx/nginx.conf:\n'
            'events {}\nhttp {\n    sendfile on;\n}\n')
    names = _names(NginxService.render_compression_snippet(dump, OURS))
    assert names[0] == 'gzip' and 'brotli' not in names


@pytest.mark.parametrize('dump_extra,nginx_v', [
    ('load_module modules/ngx_http_brotli_filter_module.so;\n', ''),
    ('', 'configure arguments: --add-module=../ngx_brotli'),
])
def test_brotli_only_when_the_module_is_there(dump_extra, nginx_v):
    dump = ('# configuration file /etc/nginx/nginx.conf:\n' + dump_extra
            + 'events {}\nhttp {\n}\n')
    names = _names(NginxService.render_compression_snippet(dump, OURS, nginx_v))
    assert {'brotli', 'brotli_types'} <= set(names)


def test_everything_already_set_is_a_comment_only_snippet():
    """Still written, so later vhost writes don't re-run `nginx -T`."""
    lines = ''.join(f'    {n} {v};\n' for n, v in NginxService.GZIP_DIRECTIVES)
    dump = ('# configuration file /etc/nginx/nginx.conf:\n'
            f'events {{}}\nhttp {{\n{lines}}}\n')
    snippet = NginxService.render_compression_snippet(dump, OURS)
    assert _names(snippet) == []
    assert 'left alone: gzip,' in snippet


def test_text_html_is_not_listed():
    """nginx always compresses text/html; listing it again only warns."""
    assert 'text/html' not in NginxService.COMPRESSION_TYPES


class TestEnsureCompression:
    @pytest.fixture
    def conf(self, tmp_path, monkeypatch, fake_subprocess):
        from subprocess_stub import FakeProc

        (tmp_path / 'conf.d').mkdir()
        monkeypatch.setattr(NginxService, 'NGINX_CONF_DIR', str(tmp_path))
        monkeypatch.setattr('app.services.nginx_service.is_command_available',
                            lambda *a, **k: True)

        def tee(argv, kwargs):
            with open(argv[-1], 'w') as fh:
                fh.write(kwargs.get('input', ''))
            return FakeProc()

        def rm(argv, kwargs):
            import os
            for path in argv[2:]:
                if os.path.exists(path):
                    os.remove(path)
            return FakeProc()

        fake_subprocess.when(['tee'], tee)
        fake_subprocess.when(['rm'], rm)
        fake_subprocess.script(['nginx', '-T'], stdout=DEBIAN_DUMP)
        fake_subprocess.script(['nginx', '-V'], stderr='nginx version: nginx/1.24.0')
        fake_subprocess.script(['nginx', '-t'])
        return tmp_path / 'conf.d' / NginxService.COMPRESSION_CONF_NAME

    def test_writes_the_snippet_once(self, conf, fake_subprocess):
        assert NginxService.ensure_compression()['changed'] is True
        assert 'gzip_types' in conf.read_text()
        assert NginxService.ensure_compression()['changed'] is False

    def test_a_snippet_nginx_rejects_is_removed(self, conf, fake_subprocess):
        fake_subprocess.script(['nginx', '-t'], returncode=1,
                               stderr='"gzip" directive is duplicate')
        res = NginxService.ensure_compression()
        assert res['success'] is False and 'duplicate' in res['error']
        assert not conf.exists(), 'a rejected snippet would break every reload'
