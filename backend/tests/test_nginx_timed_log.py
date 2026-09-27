"""The timed access-log format every generated vhost uses (plan 86 §A1).

The format appends request/upstream timing and cache status AFTER the standard
combined fields, so the readers that already parse these logs (bandwidth
accounting, the fail2ban `^<HOST> -` filters) keep working on both the old and
the new line shape. Asserted here from both ends: what the templates write, and
what the existing parser still reads.
"""
import os
from datetime import date

import pytest

from app.services.bandwidth_service import BandwidthService
from app.services.drift_service import _nginx_read_actual
from app.services.nginx_service import NginxService

OLD_LINE = ('203.0.113.9 - - [03/Jul/2026:10:00:00 +0000] "GET / HTTP/1.1" '
            '200 512 "-" "curl/8"')
NEW_LINE = (OLD_LINE
            + ' rt=0.042 urt="0.040" cs=HIT h=shop.example.com')


@pytest.mark.parametrize('kwargs', [
    dict(app_type='docker', port=8003),
    dict(app_type='static', root_path='/var/www/shop'),
    dict(app_type='php', root_path='/var/www/shop'),
    dict(app_type='python', port=8004, root_path='/srv/shop'),
    dict(app_type='remote', upstream='10.8.0.2:8080'),
    dict(app_type='docker', port=8003, micro_cache=True,
         ssl_cert='/c.pem', ssl_key='/k.pem'),
])
def test_every_vhost_flavor_logs_in_the_timed_format(kwargs):
    rendered = NginxService.render_site_config(
        'shop', domains=['shop.example.com'], **kwargs)
    assert rendered['success'], rendered
    assert ('access_log /var/log/nginx/shop.access.log serverkit_timed;'
            in rendered['config'])


def test_the_format_keeps_combined_fields_first():
    snippet = NginxService.TIMED_LOG_FORMAT_SNIPPET
    assert 'log_format serverkit_timed' in snippet
    combined = ('$remote_addr - $remote_user [$time_local] "$request" '
                "'\n                           '"
                '$status $body_bytes_sent "$http_referer" "$http_user_agent"')
    assert combined in snippet
    for field in ('$request_time', '$upstream_response_time',
                  '$upstream_cache_status', '$host'):
        assert field in snippet
        assert snippet.index(field) > snippet.index('$http_user_agent')


@pytest.mark.parametrize('line', [OLD_LINE, NEW_LINE])
def test_bandwidth_parser_reads_old_and_new_lines(tmp_path, line):
    log = tmp_path / 'shop.access.log'
    log.write_text(line + '\n')
    per_host, plain, skipped = BandwidthService.parse_log_file(
        str(log), date(2026, 7, 3))
    assert plain == [512, 1]
    assert skipped == 0
    assert per_host == {}


class TestWriteVhostDeclaresTheFormat:
    @pytest.fixture
    def nginx(self, tmp_path, monkeypatch, fake_subprocess):
        from subprocess_stub import FakeProc

        conf = tmp_path / 'nginx'
        for d in ('sites-available', 'sites-enabled', 'conf.d'):
            (conf / d).mkdir(parents=True)
        monkeypatch.setattr(NginxService, 'NGINX_CONF_DIR', str(conf))
        monkeypatch.setattr(NginxService, 'SITES_AVAILABLE', str(conf / 'sites-available'))
        monkeypatch.setattr(NginxService, 'SITES_ENABLED', str(conf / 'sites-enabled'))
        monkeypatch.setattr('app.services.nginx_service.is_command_available',
                            lambda *a, **k: True)

        def tee(argv, kwargs):
            with open(argv[-1], 'w') as fh:
                fh.write(kwargs.get('input', ''))
            return FakeProc()

        def ln(argv, kwargs):
            open(argv[-1], 'w').close()
            return FakeProc()

        fake_subprocess.when(['tee'], tee)
        fake_subprocess.when(['ln'], ln)
        fake_subprocess.script(['cat'], returncode=1)
        fake_subprocess.script(['nginx', '-t'])
        fake_subprocess.script(['systemctl'])
        return conf

    def test_snippet_is_written_before_a_timed_vhost(self, nginx):
        res = NginxService.write_vhost(
            'shop', 'server { access_log /x.log serverkit_timed; }')
        assert res['success'], res
        snippet = nginx / 'conf.d' / NginxService.TIMED_LOG_CONF_NAME
        assert snippet.read_text() == NginxService.TIMED_LOG_FORMAT_SNIPPET

    def test_a_vhost_without_the_format_does_not_write_it(self, nginx):
        assert NginxService.write_vhost('shop', 'server {}')['success']
        assert not (nginx / 'conf.d' / NginxService.TIMED_LOG_CONF_NAME).exists()


def test_drift_reads_a_legacy_access_log_line_as_the_timed_form(tmp_path):
    """An upgrade must not flag every pre-§A1 vhost as drifted."""
    vhost = tmp_path / 'shop'
    vhost.write_text('server {\n'
                     '    access_log /var/log/nginx/shop.access.log;\n'
                     '    error_log /var/log/nginx/shop.error.log;\n'
                     '}\n')
    got = _nginx_read_actual([str(vhost)])[str(vhost)]
    assert '    access_log /var/log/nginx/shop.access.log serverkit_timed;\n' in got
    assert '    error_log /var/log/nginx/shop.error.log;\n' in got


def test_drift_leaves_a_hand_edited_access_log_alone(tmp_path):
    """Only the exact legacy line is equivalent; anything else is real drift."""
    vhost = tmp_path / 'shop'
    body = 'server {\n    access_log off;\n}\n'
    vhost.write_text(body)
    assert _nginx_read_actual([str(vhost)])[str(vhost)] == body


def test_drift_reports_a_missing_file_as_none(tmp_path):
    missing = str(tmp_path / 'nope')
    assert _nginx_read_actual([missing]) == {missing: None}
