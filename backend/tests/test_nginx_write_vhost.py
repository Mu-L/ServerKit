"""The one nginx vhost lifecycle (plan 75 §G4).

`environment_domain_service` re-implemented write → symlink → `nginx -t` →
reload with raw `sudo` calls, and `nginx_advanced_service` wrote the file with
a plain unprivileged `open()`. Both are gone; what they each half-did is
asserted here once.

The rollback has no precedent in any of the copies, so it gets the most
attention: a vhost that fails `nginx -t` and stays on disk turns the *next*
unrelated reload — a cert renewal, another site going live — into the failure,
and the blame lands on whatever triggered it.
"""

import os

import pytest

from app.services.nginx_service import NginxService

pytestmark = pytest.mark.usefixtures('fake_subprocess')


@pytest.fixture
def nginx(tmp_path, monkeypatch, fake_subprocess):
    """NginxService pointed at a temp tree, with every exec scripted."""
    available = tmp_path / 'sites-available'
    enabled = tmp_path / 'sites-enabled'
    available.mkdir()
    enabled.mkdir()
    (tmp_path / 'conf.d').mkdir()   # http-level snippets (timed log format)
    monkeypatch.setattr(NginxService, 'NGINX_CONF_DIR', str(tmp_path))
    monkeypatch.setattr(NginxService, 'SITES_AVAILABLE', str(available))
    monkeypatch.setattr(NginxService, 'SITES_ENABLED', str(enabled))
    monkeypatch.setattr('app.services.nginx_service.is_command_available',
                        lambda *a, **k: True)

    from subprocess_stub import FakeProc

    def tee(argv, kwargs):
        path = argv[2] if argv[1] == '-a' else argv[1]
        with open(path, 'a' if argv[1] == '-a' else 'w') as fh:
            fh.write(kwargs.get('input', ''))
        return FakeProc()

    def cat(argv, kwargs):
        try:
            with open(argv[1]) as fh:
                return FakeProc(stdout=fh.read())
        except OSError:
            return FakeProc(returncode=1, stderr='No such file')

    def ln(argv, kwargs):
        target, link = argv[-2], argv[-1]
        if not os.path.exists(link):
            with open(link, 'w') as fh:      # a plain file stands in for the symlink
                fh.write(target)
        return FakeProc()

    def rm(argv, kwargs):
        for path in argv[1:]:
            if not path.startswith('-') and os.path.exists(path):
                os.remove(path)
        return FakeProc()

    fake_subprocess.when(['tee'], tee)
    fake_subprocess.when(['cat'], cat)
    fake_subprocess.when(['ln'], ln)
    fake_subprocess.when(['rm'], rm)
    fake_subprocess.script(['nginx', '-t'])          # valid by default
    fake_subprocess.script(['systemctl'])            # reload
    return fake_subprocess


def _paths(name='shop'):
    return (os.path.join(NginxService.SITES_AVAILABLE, name),
            os.path.join(NginxService.SITES_ENABLED, name))


class TestHappyPath:
    def test_writes_enables_tests_and_reloads(self, nginx):
        res = NginxService.write_vhost('shop', 'server { listen 80; }')
        assert res['success'] is True

        available, enabled = _paths()
        assert open(available).read() == 'server { listen 80; }'
        assert os.path.exists(enabled)

        cmds = nginx.commands()
        assert ['nginx', '-t'] in cmds
        assert any(c[:2] == ['systemctl', 'reload'] for c in cmds)

    def test_config_test_runs_before_the_reload(self, nginx):
        NginxService.write_vhost('shop', 'server {}')
        cmds = nginx.commands()
        test_at = cmds.index(['nginx', '-t'])
        reload_at = next(i for i, c in enumerate(cmds) if c[:2] == ['systemctl', 'reload'])
        assert test_at < reload_at

    def test_enable_false_writes_without_symlinking(self, nginx):
        NginxService.write_vhost('shop', 'server {}', enable=False)
        available, enabled = _paths()
        assert os.path.exists(available)
        assert not os.path.exists(enabled)


class TestRollback:
    """What none of the hand-rolled copies did."""

    def test_a_new_vhost_that_fails_the_test_is_removed(self, nginx):
        nginx.script(['nginx', '-t'], returncode=1, stderr='invalid directive')

        res = NginxService.write_vhost('shop', 'server { bogus; }')

        assert res['success'] is False
        assert 'invalid directive' in res['error']
        available, enabled = _paths()
        assert not os.path.exists(available), 'broken vhost left on disk'
        assert not os.path.exists(enabled), 'broken vhost left enabled'

    def test_an_existing_vhost_is_restored_byte_for_byte(self, nginx):
        good = 'server { listen 80; server_name a.test; }'
        assert NginxService.write_vhost('shop', good)['success'] is True

        nginx.script(['nginx', '-t'], returncode=1, stderr='invalid directive')
        res = NginxService.write_vhost('shop', 'server { bogus; }')

        assert res['success'] is False
        available, _ = _paths()
        assert open(available).read() == good

    def test_an_already_enabled_vhost_stays_enabled_after_a_rollback(self, nginx):
        NginxService.write_vhost('shop', 'server {}')
        nginx.script(['nginx', '-t'], returncode=1, stderr='nope')

        NginxService.write_vhost('shop', 'server { bogus; }')

        _, enabled = _paths()
        assert os.path.exists(enabled), 'rollback disabled a site it did not enable'

    def test_no_reload_happens_when_the_test_fails(self, nginx):
        """The point of testing first: a broken config never reaches nginx."""
        nginx.script(['nginx', '-t'], returncode=1, stderr='nope')
        NginxService.write_vhost('shop', 'server { bogus; }')
        assert not any(c[:2] == ['systemctl', 'reload'] for c in nginx.commands())


class TestReloadFailureRollback:
    """A config that passes `nginx -t` can still fail the reload itself
    (systemd unit error, raced restart). The write must not be left behind:
    nginx keeps serving the previous config after a failed reload, so the
    on-disk file is put back to match it."""

    def test_a_failed_reload_restores_the_previous_vhost(self, nginx):
        good = 'server { listen 80; server_name a.test; }'
        assert NginxService.write_vhost('shop', good)['success'] is True

        nginx.script(['systemctl'], returncode=1, stderr='reload exploded')
        res = NginxService.write_vhost('shop', 'server { listen 81; }')

        assert res['success'] is False
        available, enabled = _paths()
        assert open(available).read() == good
        assert os.path.exists(enabled), 'reload rollback disabled an existing site'

    def test_a_failed_reload_removes_a_new_vhost(self, nginx):
        nginx.script(['systemctl'], returncode=1, stderr='reload exploded')

        res = NginxService.write_vhost('shop', 'server {}')

        assert res['success'] is False
        available, enabled = _paths()
        assert not os.path.exists(available), 'unreloadable vhost left on disk'
        assert not os.path.exists(enabled), 'unreloadable vhost left enabled'


class TestSiteNameValidation:
    """`name` is joined straight onto SITES_AVAILABLE — a caller-controlled
    name must not become a path."""

    def test_a_traversing_name_is_refused(self, nginx):
        res = NginxService.write_vhost('../evil', 'server {}')
        assert res['success'] is False
        assert nginx.commands() == [], 'a refused name still ran commands'

    def test_an_absolute_name_is_refused(self, nginx):
        res = NginxService.write_vhost('/tmp/evil', 'server {}')
        assert res['success'] is False
        assert nginx.commands() == []

    def test_dotdot_itself_is_refused(self, nginx):
        assert NginxService.write_vhost('..', 'server {}')['success'] is False


class TestConsolidatedCallers:
    """The high-traffic renderers must inherit the lifecycle door's rollback."""

    def test_create_site_removes_a_new_vhost_when_config_test_fails(self, nginx):
        nginx.script(['nginx', '-t'], returncode=1, stderr='rendered config is invalid')

        res = NginxService.create_site(
            'shop', 'docker', ['shop.example.com'], port=8080)

        assert res['success'] is False
        assert 'rendered config is invalid' in res['error']
        available, enabled = _paths()
        assert not os.path.exists(available), 'create_site bypassed write_vhost rollback'
        assert not os.path.exists(enabled), 'create_site left a broken vhost enabled'

    def test_add_ssl_restores_the_previous_vhost_when_config_test_fails(self, nginx):
        original = '''server {
    listen 80;
    server_name shop.example.com;
}
'''
        assert NginxService.write_vhost('shop', original)['success'] is True
        nginx.script(['nginx', '-t'], returncode=1, stderr='bad certificate config')

        res = NginxService.add_ssl_to_site(
            'shop', '/certs/fullchain.pem', '/certs/privkey.pem')

        assert res['success'] is False
        assert 'bad certificate config' in res['error']
        available, enabled = _paths()
        assert open(available).read() == original
        assert os.path.exists(enabled), 'SSL rollback disabled an existing site'


class TestFailedWrite:
    def test_a_failed_write_is_reported_and_nothing_is_enabled(self, nginx):
        nginx.script(['tee'], returncode=1, stderr='Permission denied')

        res = NginxService.write_vhost('shop', 'server {}')

        assert res['success'] is False
        assert 'Permission denied' in res['error']
        _, enabled = _paths()
        assert not os.path.exists(enabled)

    def test_a_missing_tee_does_not_report_success(self, nginx):
        """§A: a write that could not run must never render as one that did."""
        nginx.script(['tee'], raises=FileNotFoundError)
        assert NginxService.write_vhost('shop', 'server {}')['success'] is False


class TestReadVhost:
    def test_returns_the_content(self, nginx):
        NginxService.write_vhost('shop', 'server { listen 80; }')
        assert NginxService.read_vhost('shop') == 'server { listen 80; }'

    def test_unreadable_is_none_never_empty_string(self, nginx):
        """None means "could not determine"; '' would read as an empty vhost."""
        assert NginxService.read_vhost('does-not-exist') is None

    def test_a_broken_cat_is_none_not_an_exception(self, nginx):
        nginx.script(['cat'], raises=FileNotFoundError)
        assert NginxService.read_vhost('shop') is None


class TestOneDeclarationOfThePaths:
    """Four files used to hardcode these; a test redirecting one left the other
    three writing to the real /etc/nginx."""

    def test_every_service_reads_nginxservice_paths(self, monkeypatch):
        from app.services import waf_service
        from app.services.environment_domain_service import EnvironmentDomainService
        from app.services.nginx_advanced_service import NginxAdvancedService

        assert EnvironmentDomainService.SITES_AVAILABLE == NginxService.SITES_AVAILABLE
        assert EnvironmentDomainService.SITES_ENABLED == NginxService.SITES_ENABLED
        assert NginxAdvancedService.SITES_AVAILABLE == NginxService.SITES_AVAILABLE
        assert NginxAdvancedService.SITES_ENABLED == NginxService.SITES_ENABLED
        assert waf_service.SITES_AVAILABLE == NginxService.SITES_AVAILABLE
