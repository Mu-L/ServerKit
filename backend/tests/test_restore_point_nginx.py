"""Plan 81 M2: nginx vhost restore-point payloads and lifecycle hooks."""

import os
from types import SimpleNamespace

import pytest

from app.services.nginx_service import NginxService
from app.services import restore_point_adapter_nginx as adapter


@pytest.fixture
def nginx_tree(tmp_path, monkeypatch, fake_subprocess):
    available = tmp_path / 'sites-available'
    enabled = tmp_path / 'sites-enabled'
    available.mkdir()
    enabled.mkdir()
    # Global snippets already in place, so vhost writes don't probe nginx -T.
    (tmp_path / 'conf.d').mkdir()
    (tmp_path / 'conf.d' / NginxService.COMPRESSION_CONF_NAME).write_text('# set')
    monkeypatch.setattr(NginxService, 'NGINX_CONF_DIR', str(tmp_path))
    monkeypatch.setattr(NginxService, 'SITES_AVAILABLE', str(available))
    monkeypatch.setattr(NginxService, 'SITES_ENABLED', str(enabled))
    monkeypatch.setattr(
        'app.services.nginx_service.is_command_available', lambda *a, **k: True,
    )

    from subprocess_stub import FakeProc

    def tee(argv, kwargs):
        path = argv[2] if argv[1] == '-a' else argv[1]
        with open(path, 'a' if argv[1] == '-a' else 'w') as stream:
            stream.write(kwargs.get('input', ''))
        return FakeProc()

    def cat(argv, _kwargs):
        try:
            with open(argv[1]) as stream:
                return FakeProc(stdout=stream.read())
        except OSError:
            return FakeProc(returncode=1, stderr='unreadable')

    def ln(argv, _kwargs):
        target, link = argv[-2], argv[-1]
        with open(link, 'w') as stream:
            stream.write(target)
        return FakeProc()

    def rm(argv, _kwargs):
        for path in argv[1:]:
            if not path.startswith('-') and os.path.exists(path):
                os.remove(path)
        return FakeProc()

    fake_subprocess.when(['tee'], tee)
    fake_subprocess.when(['cat'], cat)
    fake_subprocess.when(['ln'], ln)
    fake_subprocess.when(['rm'], rm)
    fake_subprocess.script(['nginx', '-t'])
    fake_subprocess.script(['systemctl'])
    return available, enabled


def test_capture_includes_exists_enabled_and_content(nginx_tree):
    available, enabled = nginx_tree
    (available / 'shop').write_text('server { listen 80; }')
    (enabled / 'shop').write_text(str(available / 'shop'))

    assert adapter.capture('shop') == {
        'exists': True,
        'enabled': True,
        'content': 'server { listen 80; }',
    }


def test_capture_refuses_unreadable_existing_vhost(nginx_tree, monkeypatch):
    available, _ = nginx_tree
    (available / 'shop').write_text('present')
    monkeypatch.setattr(
        NginxService, 'read_vhost', classmethod(lambda cls, name: None),
    )

    with pytest.raises(RuntimeError, match='could not be read'):
        adapter.capture('shop')


@pytest.mark.parametrize('name', [
    '../outside', '..\\outside', '/etc/nginx/nginx.conf', 'C:\\Windows\\win.ini',
])
def test_scope_rejects_paths_outside_the_vhost_directory(nginx_tree, name):
    with pytest.raises(ValueError, match='safe filename'):
        adapter.capture(name)


def test_restore_round_trip_reproduces_content_and_disabled_state(nginx_tree):
    available, enabled = nginx_tree
    saved = {
        'exists': True,
        'enabled': False,
        'content': 'server { listen 80; server_name saved.test; }',
    }
    assert adapter.restore('shop', saved)['success'] is True
    assert adapter.capture('shop') == saved

    changed = {
        'exists': True,
        'enabled': True,
        'content': 'server { listen 80; server_name changed.test; }',
    }
    assert adapter.restore('shop', changed)['success'] is True
    assert (enabled / 'shop').exists()

    assert adapter.restore('shop', saved)['success'] is True
    assert (available / 'shop').read_text() == saved['content']
    assert not (enabled / 'shop').exists()


def test_restore_absent_uses_delete_lifecycle_door(monkeypatch):
    calls = []
    monkeypatch.setattr(
        NginxService, 'delete_site',
        classmethod(lambda cls, name: calls.append(('delete', name)) or {
            'success': True,
        }),
    )

    result = adapter.restore('shop', {
        'exists': False, 'enabled': False, 'content': None,
    })

    assert result['success'] is True
    assert calls == [('delete', 'shop')]


def test_lifecycle_hooks_capture_once_per_public_action(
        app, nginx_tree, monkeypatch):
    from app.services import restore_point_service

    available, _ = nginx_tree
    existing = restore_point_service.get_adapter('nginx_vhost')
    restore_point_service.register_adapter('nginx_vhost', adapter)
    captured = []
    monkeypatch.setattr(
        restore_point_service, 'capture',
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    try:
        assert NginxService.write_vhost('shop', 'server {}')['success']
        assert NginxService.disable_site('shop')['success']
        assert NginxService.enable_site('shop')['success']
        assert NginxService.delete_site('shop')['success']
    finally:
        if existing is None:
            restore_point_service.ADAPTERS.pop('nginx_vhost', None)
        else:
            restore_point_service.register_adapter('nginx_vhost', existing)

    assert not (available / 'shop').exists()
    assert [item[1]['label'] for item in captured] == [
        'before nginx_vhost.write_vhost',
        'before nginx_vhost.disable_site',
        'before nginx_vhost.enable_site',
        'before nginx_vhost.delete_site',
    ]


def test_site_domain_writer_does_not_enable_twice(monkeypatch):
    from app.services.site_domain_service import SiteDomainService

    app = SimpleNamespace(name='shop')
    monkeypatch.setattr(
        SiteDomainService, 'app_vhost_kwargs',
        classmethod(lambda cls, target, force_type=None: ({
            'name': target.name,
            'app_type': 'docker',
            'domains': ['shop.test'],
            'port': 8080,
        }, None)),
    )
    monkeypatch.setattr(
        NginxService, 'create_site',
        classmethod(lambda cls, **kwargs: {'success': True}),
    )
    monkeypatch.setattr(
        NginxService, 'enable_site',
        classmethod(lambda cls, name: pytest.fail('redundant enable_site call')),
    )

    assert SiteDomainService.write_app_vhost(app) == {
        'nginx': {'success': True},
        'warning': None,
    }
