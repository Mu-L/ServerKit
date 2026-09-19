"""Baseline coverage for the plugin/extension install pipeline.

These behaviors already existed (builtin install, the contributions envelope,
the disable→503 guard, reinstall metadata refresh, zip-slip rejection) but were
untested. Phase 0 of docs/plans/12_EXTENSIONS_PLATFORM_PLAN.md locks them in
before later phases build on them.
"""
import json
import os

import pytest

from app import db
from app.models.plugin import InstalledPlugin
from app.services import plugin_service

# Registers blueprints / url rules on the app fixture. Flask cannot unregister
# those, so these tests need a private app rather than the session-wide one
# (plan 64 Phase 1).
pytestmark = pytest.mark.fresh_app


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def plugin_dirs(tmp_path, monkeypatch):
    """Redirect the plugin service's on-disk targets into a temp tree so tests
    never touch the real repo's backend/app/plugins or frontend/src/plugins."""
    backend = tmp_path / 'backend_plugins'
    frontend = tmp_path / 'frontend_plugins'
    builtin = tmp_path / 'builtin_extensions'
    backend.mkdir()
    frontend.mkdir()
    builtin.mkdir()
    monkeypatch.setattr(plugin_service, 'BACKEND_PLUGINS_DIR', str(backend))
    monkeypatch.setattr(plugin_service, 'FRONTEND_PLUGINS_DIR', str(frontend))
    monkeypatch.setattr(plugin_service, 'BUILTIN_EXTENSIONS_DIR', str(builtin))
    return {'backend': backend, 'frontend': frontend, 'builtin': builtin}


def _write_builtin(builtin_dir, slug='serverkit-demo', version='1.0.0',
                   display_name='Demo Extension'):
    """Create a minimal frontend-only builtin extension on disk."""
    folder = builtin_dir / slug
    (folder / 'frontend').mkdir(parents=True)
    manifest = {
        'name': slug,
        'display_name': display_name,
        'version': version,
        'description': 'A demo builtin extension.',
        'author': 'ServerKit',
        'category': 'utility',
        'permissions': ['filesystem'],
        'contributions': {
            'nav': [{'id': 'demo', 'label': 'Demo', 'route': '/demo',
                     'category': 'system', 'icon': '<circle cx="12" cy="12" r="8"/>'}],
            'routes': [{'path': 'demo', 'component': 'DemoPage'},
                       {'path': 'demo-tab', 'component': 'DemoTabPage',
                        'group': 'files'}],
            'page_titles': {'/demo': 'Demo'},
            'command_palette': [{'label': 'Demo', 'path': '/demo',
                                 'category': 'Pages', 'keywords': 'demo'}],
            # Tab-group contribution (#43): a tab added to a core-owned
            # TabGroupLayout group, paired with the group route above.
            'tabs': [{'group': 'files', 'to': '/demo-tab', 'label': 'Demo Tab',
                      'icon': '<circle cx="12" cy="12" r="8"/>'}],
            # Placeable dashboard widget type (plan 62): shows up in the
            # dashboard widget library, not at a fixed host slot.
            'dashboard_widgets': [{'id': 'demo-widget', 'name': 'Demo Widget',
                                   'component': 'DemoWidget', 'category': 'Utility',
                                   'w': 3, 'h': 2, 'min': [2, 2],
                                   'default_cfg': {'limit': 5}}],
            'walkthroughs': [{
                'id': 'demo-first-run',
                'title': 'Try Demo',
                'description': 'Open Demo and complete one safe action.',
                'steps': [{
                    'id': 'open-demo',
                    'title': 'Open Demo',
                    'description': 'Navigate to the Demo extension.',
                    'path': '/demo',
                    'target': 'demo-page',
                    'completion': {'type': 'route', 'path': '/demo'},
                }],
            }],
        },
    }
    (folder / 'plugin.json').write_text(json.dumps(manifest), encoding='utf-8')
    (folder / 'frontend' / 'index.jsx').write_text(
        'export function DemoPage() { return null; }\n', encoding='utf-8')
    return folder, manifest


# --------------------------------------------------------------------------- #
# Zip-slip defense
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize('evil', [
    '../../etc/passwd',
    'a/../../b',
    '..\\..\\windows\\system32',
    'C:\\Windows\\system32',   # drive-qualified absolute path
])
def test_safe_extract_path_rejects_traversal(tmp_path, evil):
    with pytest.raises(ValueError):
        plugin_service._safe_extract_path(str(tmp_path), evil)


def test_safe_extract_path_allows_normal(tmp_path):
    out = plugin_service._safe_extract_path(str(tmp_path), 'sub/dir/file.py')
    assert out.startswith(str(tmp_path))


def test_safe_extract_path_neutralizes_leading_slash(tmp_path):
    # A rooted POSIX path is not an error — the leading slash is stripped so
    # the entry lands *inside* the destination rather than at the filesystem
    # root. The guarantee is containment, not rejection.
    out = plugin_service._safe_extract_path(str(tmp_path), '/etc/cron.d/x')
    assert out.startswith(str(tmp_path))
    assert out.endswith(os.path.join('etc', 'cron.d', 'x'))


# --------------------------------------------------------------------------- #
# Builtin install + reinstall metadata refresh
# --------------------------------------------------------------------------- #

def test_install_builtin_creates_active_plugin(app, plugin_dirs):
    _write_builtin(plugin_dirs['builtin'])

    listed = plugin_service.list_builtin_extensions()
    assert len(listed) == 1
    assert listed[0]['slug'] == 'serverkit-demo'
    assert listed[0]['status'] == 'not_installed'

    plugin = plugin_service.install_builtin_extension('serverkit-demo')
    assert plugin.status == InstalledPlugin.STATUS_ACTIVE
    assert plugin.source_type == 'builtin'
    assert plugin.has_frontend is True
    assert plugin.has_backend is False
    # Contributions survive the round-trip through the manifest.
    assert plugin.manifest['contributions']['nav'][0]['route'] == '/demo'

    # The pre-bundled frontend was written into the (temp) plugins dir.
    assert (plugin_dirs['frontend'] / 'serverkit-demo' / 'index.jsx').exists()


def test_install_from_path_stamps_local(app, plugin_dirs):
    folder, _ = _write_builtin(plugin_dirs['builtin'], slug='local-demo')
    plugin = plugin_service.install_from_path(str(folder))
    assert plugin.status == InstalledPlugin.STATUS_ACTIVE
    assert plugin.source_type == 'local'


def test_install_from_zip_stamps_upload(app, plugin_dirs):
    import io
    import zipfile
    manifest = {
        'name': 'zip-demo', 'display_name': 'Zip Demo', 'version': '1.0.0',
        'category': 'utility',
        'contributions': {'nav': [{'id': 'zip-demo', 'label': 'Z', 'route': '/z'}]},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('plugin.json', json.dumps(manifest))
        zf.writestr('frontend/index.jsx', 'export function P(){return null;}\n')
    plugin = plugin_service.install_from_zip(buf.getvalue())
    assert plugin.status == InstalledPlugin.STATUS_ACTIVE
    assert plugin.source_type == 'upload'


def test_reinstall_refreshes_metadata(app, plugin_dirs):
    folder, manifest = _write_builtin(plugin_dirs['builtin'])
    plugin_service.install_builtin_extension('serverkit-demo')

    # An active plugin can't be reinstalled directly — disable it first.
    existing = InstalledPlugin.query.filter_by(slug='serverkit-demo').first()
    plugin_service.disable_plugin(existing.id)

    # Bump the manifest and reinstall.
    manifest['version'] = '2.0.0'
    manifest['display_name'] = 'Demo Extension v2'
    (folder / 'plugin.json').write_text(json.dumps(manifest), encoding='utf-8')

    plugin = plugin_service.install_builtin_extension('serverkit-demo')
    assert plugin.version == '2.0.0'
    assert plugin.display_name == 'Demo Extension v2'
    assert plugin.status == InstalledPlugin.STATUS_ACTIVE
    # Still a single row — reinstall updates in place.
    assert InstalledPlugin.query.filter_by(slug='serverkit-demo').count() == 1


def test_installed_manifests_are_written_with_lf(app, plugin_dirs):
    """Manifests are tracked files pinned to LF by .gitattributes.

    Python's text mode rewrites \\n as \\r\\n on Windows, so an install run on a
    dev machine used to rewrite every tracked manifest with CRLF and leave four
    modified files behind after the suite -- a diff with no content change.
    """
    _write_builtin(plugin_dirs['builtin'])
    plugin_service.install_builtin_extension('serverkit-demo')

    # The demo builtin ships a frontend half only, so no backend manifest is
    # written. _regenerate_frontend_manifest() is skipped under the testing
    # config, so plugins-manifest.json is out of reach here too.
    path = plugin_dirs['frontend'] / 'serverkit-demo' / 'plugin.json'
    assert path.exists(), f'{path} was not written'
    raw = path.read_bytes()
    assert b'\r\n' not in raw, f'{path} was written with CRLF'
    assert raw.endswith(b'\n'), f'{path} lost its trailing newline'


def test_reinstall_active_plugin_is_rejected(app, plugin_dirs):
    _write_builtin(plugin_dirs['builtin'])
    plugin_service.install_builtin_extension('serverkit-demo')
    with pytest.raises(ValueError, match='already installed'):
        plugin_service.install_builtin_extension('serverkit-demo')


# --------------------------------------------------------------------------- #
# Contributions endpoint envelope
# --------------------------------------------------------------------------- #

def test_contributions_endpoint_envelope(app, client, auth_headers, plugin_dirs):
    _write_builtin(plugin_dirs['builtin'])
    plugin_service.install_builtin_extension('serverkit-demo')

    resp = client.get('/api/v1/plugins/contributions', headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()

    # The envelope always carries every key.
    for key in ('nav', 'routes', 'page_titles', 'command_palette', 'widgets',
                'dashboard_widgets', 'layouts', 'tabs', 'walkthroughs', 'ai'):
        assert key in data

    # Contributions are tagged with the source plugin slug.
    assert any(n.get('plugin') == 'serverkit-demo' and n.get('route') == '/demo'
               for n in data['nav'])
    assert any(r.get('plugin') == 'serverkit-demo' and r.get('component') == 'DemoPage'
               for r in data['routes'])
    assert data['page_titles'].get('/demo') == 'Demo'

    # Tab-group contributions (#43): the tab entry and its group-nested route
    # both survive the round-trip, tagged with the plugin slug.
    assert any(t.get('plugin') == 'serverkit-demo' and t.get('group') == 'files'
               and t.get('to') == '/demo-tab' for t in data['tabs'])
    assert any(r.get('plugin') == 'serverkit-demo' and r.get('group') == 'files'
               and r.get('component') == 'DemoTabPage' for r in data['routes'])

    # Placeable dashboard widget types travel on their own key, tagged with the
    # slug, and never leak into the fixed-slot `widgets` surface.
    assert any(w.get('plugin') == 'serverkit-demo' and w.get('id') == 'demo-widget'
               and w.get('component') == 'DemoWidget'
               for w in data['dashboard_widgets'])
    assert not any(w.get('id') == 'demo-widget' for w in data['widgets'])

    assert any(w.get('plugin') == 'serverkit-demo'
               and w.get('id') == 'demo-first-run'
               for w in data['walkthroughs'])


def test_disabled_plugin_drops_from_contributions(app, client, auth_headers, plugin_dirs):
    _write_builtin(plugin_dirs['builtin'])
    plugin = plugin_service.install_builtin_extension('serverkit-demo')

    plugin_service.disable_plugin(plugin.id)
    resp = client.get('/api/v1/plugins/contributions', headers=auth_headers)
    assert resp.status_code == 200
    assert not any(n.get('plugin') == 'serverkit-demo' for n in resp.get_json()['nav'])


# --------------------------------------------------------------------------- #
# Disable guard: a disabled plugin's routes return 503
# --------------------------------------------------------------------------- #

def test_disable_guard_returns_503(app):
    from flask import Blueprint, jsonify

    p = InstalledPlugin(
        name='guard-test', display_name='Guard Test', slug='guard-test',
        version='1.0.0', status=InstalledPlugin.STATUS_ACTIVE, has_backend=True,
    )
    p.manifest = {}
    db.session.add(p)
    db.session.commit()

    bp = Blueprint('guard_test_bp', __name__)

    @bp.route('/ping')
    def ping():
        return jsonify({'ok': True})

    plugin_service._attach_status_guard(bp, 'guard-test')
    app.register_blueprint(bp, url_prefix='/api/v1/guard-test')

    c = app.test_client()
    assert c.get('/api/v1/guard-test/ping').status_code == 200

    p.status = InstalledPlugin.STATUS_DISABLED
    db.session.commit()
    r = c.get('/api/v1/guard-test/ping')
    assert r.status_code == 503
    assert r.get_json()['status'] == 'disabled'


# --------------------------------------------------------------------------- #
# Per-plugin config store (#49)
# --------------------------------------------------------------------------- #

def test_plugin_config_roundtrip_and_sdk(app, client, auth_headers, plugin_dirs):
    """Config saves via the admin API, reads back, and reaches the plugin
    through the plugins_sdk.config(slug) accessor."""
    _write_builtin(plugin_dirs['builtin'])
    plugin = plugin_service.install_builtin_extension('serverkit-demo')

    # Starts empty (schema comes along for the form).
    r = client.get(f'/api/v1/plugins/{plugin.id}/config', headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json()['config'] == {}

    r = client.put(
        f'/api/v1/plugins/{plugin.id}/config', headers=auth_headers,
        json={'config': {'api_key': 'sk-123', 'refresh_seconds': 30, 'enabled': True}},
    )
    assert r.status_code == 200

    r = client.get(f'/api/v1/plugins/{plugin.id}/config', headers=auth_headers)
    assert r.get_json()['config'] == {
        'api_key': 'sk-123', 'refresh_seconds': 30, 'enabled': True}

    from app import plugins_sdk
    assert plugins_sdk.config('serverkit-demo')['api_key'] == 'sk-123'
    assert plugins_sdk.config('no-such-plugin') == {}

    # Values may hold secrets — they must never leak through to_dict().
    assert 'config' not in plugin.to_dict()
    assert 'config_json' not in plugin.to_dict()


def test_plugin_config_rejects_non_object(app, client, auth_headers, plugin_dirs):
    _write_builtin(plugin_dirs['builtin'])
    plugin = plugin_service.install_builtin_extension('serverkit-demo')
    r = client.put(f'/api/v1/plugins/{plugin.id}/config', headers=auth_headers,
                   json={'config': 'not-a-dict'})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Manifest shape lint (#52)
# --------------------------------------------------------------------------- #

_LINT_BASE = {'name': 'demo', 'display_name': 'Demo', 'version': '1.0.0'}


def test_manifest_lint_rejects_bad_shapes():
    from app.services.plugin_service import _validate_manifest

    with pytest.raises(ValueError, match='entry_point'):
        _validate_manifest({**_LINT_BASE, 'entry_point': 'not-a-module-ref'})
    with pytest.raises(ValueError, match='socket_entry'):
        _validate_manifest({**_LINT_BASE, 'socket_entry': 'nope nope'})
    with pytest.raises(ValueError, match=r'jobs\[0\]'):
        _validate_manifest({**_LINT_BASE, 'jobs': [{'kind': 'x'}]})
    with pytest.raises(ValueError, match=r'schedules\[0\]'):
        _validate_manifest({**_LINT_BASE, 'schedules': [{'name': 'n'}]})
    with pytest.raises(ValueError, match=r'tabs\[0\] missing to, label'):
        _validate_manifest({**_LINT_BASE,
                            'contributions': {'tabs': [{'group': 'files'}]}})
    with pytest.raises(ValueError, match=r'routes\[0\] missing component'):
        _validate_manifest({**_LINT_BASE,
                            'contributions': {'routes': [{'path': 'x'}]}})
    with pytest.raises(ValueError, match='lifecycle.install'):
        _validate_manifest({**_LINT_BASE, 'lifecycle': {'install': 'no-colon'}})
    with pytest.raises(ValueError, match=r'walkthroughs\[0\].steps\[0\].target'):
        _validate_manifest({
            **_LINT_BASE,
            'contributions': {'walkthroughs': [{
                'id': 'bad-guide',
                'title': 'Bad Guide',
                'description': 'Uses an arbitrary selector.',
                'steps': [{
                    'id': 'bad-step',
                    'title': 'Bad Step',
                    'description': 'This must fail.',
                    'target': '#password',
                }],
            }]},
        })


def test_manifest_lint_unknown_contrib_kind_warns_not_fails():
    """Forward compat: a newer manifest with a contribution kind this panel
    doesn't know must still install (warn only)."""
    from app.services.plugin_service import _validate_manifest
    assert _validate_manifest(
        {**_LINT_BASE, 'contributions': {'future_kind': [{'x': 1}]}}) is True


def test_manifest_lint_accepts_all_shipped_builtins():
    """Every in-repo builtin manifest passes the lint — keeps the rules and the
    shipped extensions honest against each other."""
    from app.services.plugin_service import _validate_manifest
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(tests_dir))
    builtin_root = os.path.join(repo_root, 'builtin-extensions')
    manifests = []
    for folder in sorted(os.listdir(builtin_root)):
        mpath = os.path.join(builtin_root, folder, 'plugin.json')
        if os.path.isfile(mpath):
            with open(mpath, encoding='utf-8') as f:
                manifests.append((folder, json.load(f)))
    assert manifests, 'no builtin manifests found'
    for folder, manifest in manifests:
        assert _validate_manifest(manifest) is True, folder


# --------------------------------------------------------------------------- #
# Scheduled extension-update check (#50)
# --------------------------------------------------------------------------- #

def test_extension_update_check_notifies_admins_once_per_release(app, monkeypatch):
    """The daily job notifies admins when updates appear, then stays quiet for
    the same release set, and speaks up again when the set changes."""
    from app.jobs import builtin_handlers
    from app.notifications.sdk import NotifySdk

    sent = []
    monkeypatch.setattr(
        NotifySdk, 'send',
        lambda self, event, to=None, data=None, **kw: sent.append((event, to, data)))

    updates = [{'slug': 'serverkit-gui', 'installed_version': '1.0.0',
                'available_version': '1.1.0', 'update_available': True}]
    monkeypatch.setattr(
        'app.services.plugin_service.check_for_updates', lambda: updates)

    result = builtin_handlers.run_extension_update_check()
    assert result == {'updates': 1, 'notified': True}
    assert sent and sent[0][0] == 'extensions.updates_available'
    assert sent[0][1] == 'admins'
    assert 'serverkit-gui' in sent[0][2]['summary']

    # Same release set → no second notification.
    result = builtin_handlers.run_extension_update_check()
    assert result == {'updates': 1, 'notified': False}
    assert len(sent) == 1

    # A newer release changes the fingerprint → notify again.
    updates[0]['available_version'] = '1.2.0'
    result = builtin_handlers.run_extension_update_check()
    assert result == {'updates': 1, 'notified': True}
    assert len(sent) == 2


def test_extension_update_check_quiet_when_no_updates(app, monkeypatch):
    from app.jobs import builtin_handlers
    monkeypatch.setattr('app.services.plugin_service.check_for_updates', lambda: [])
    assert builtin_handlers.run_extension_update_check() is None


def test_extension_update_schedule_is_seeded():
    """The builtin schedule table includes the daily extension-update check."""
    from app.jobs.builtin_handlers import _BUILTINS
    kinds = {b[0] for b in _BUILTINS}
    assert 'builtin.extension_updates' in kinds


# --------------------------------------------------------------------------- #
# Boot repair pass (#48): plugins whose files a panel update wiped
# --------------------------------------------------------------------------- #

def test_repair_restores_builtin_files(app, plugin_dirs):
    """A builtin install whose extracted files vanished (fresh update tree) is
    restored from builtin-extensions/ and stays active."""
    import shutil
    _write_builtin(plugin_dirs['builtin'])
    plugin = plugin_service.install_builtin_extension('serverkit-demo')
    assert (plugin_dirs['frontend'] / 'serverkit-demo' / 'index.jsx').exists()

    # Simulate the update wiping the extracted plugin dirs.
    shutil.rmtree(plugin_dirs['frontend'] / 'serverkit-demo')

    plugin_service.repair_missing_plugins()

    assert (plugin_dirs['frontend'] / 'serverkit-demo' / 'index.jsx').exists()
    db.session.refresh(plugin)
    assert plugin.status == InstalledPlugin.STATUS_ACTIVE


def test_repair_reinstalls_url_plugin_from_source(app, plugin_dirs, monkeypatch):
    """A URL-installed plugin with missing files is re-downloaded from its
    source_url, without hot-loading (the boot loader registers right after)."""
    p = InstalledPlugin(
        name='remote-thing', display_name='Remote Thing', slug='remote-thing',
        version='1.0.0', status=InstalledPlugin.STATUS_ACTIVE,
        has_backend=True, source_type='url',
        source_url='https://example.com/remote-thing.zip',
    )
    p.manifest = {}
    db.session.add(p)
    db.session.commit()

    calls = []
    monkeypatch.setattr(
        plugin_service, 'install_from_url',
        lambda url, **kw: calls.append((url, kw)))

    plugin_service.repair_missing_plugins()

    assert calls, 'repair should reinstall from the recorded source_url'
    url, kw = calls[0]
    assert url == 'https://example.com/remote-thing.zip'
    assert kw.get('force') is True
    assert kw.get('hot_load') is False


def test_repair_marks_sourceless_install_as_error(app, plugin_dirs):
    """An upload-installed plugin (no source URL) with missing files can't be
    auto-restored — it flips to error with a re-upload hint instead."""
    p = InstalledPlugin(
        name='uploaded-thing', display_name='Uploaded Thing',
        slug='uploaded-thing', version='1.0.0',
        status=InstalledPlugin.STATUS_ACTIVE,
        has_frontend=True, source_type='upload', source_url='uploaded.zip',
    )
    p.manifest = {}
    db.session.add(p)
    db.session.commit()

    plugin_service.repair_missing_plugins()

    db.session.refresh(p)
    assert p.status == InstalledPlugin.STATUS_ERROR
    assert 'Re-upload' in p.error_message


def test_repair_leaves_intact_installs_alone(app, plugin_dirs, monkeypatch):
    """A healthy install (files present) is not touched by the repair pass."""
    _write_builtin(plugin_dirs['builtin'])
    plugin = plugin_service.install_builtin_extension('serverkit-demo')

    monkeypatch.setattr(
        plugin_service, 'install_from_path',
        lambda *a, **kw: pytest.fail('repair must not reinstall a healthy plugin'))

    plugin_service.repair_missing_plugins()
    db.session.refresh(plugin)
    assert plugin.status == InstalledPlugin.STATUS_ACTIVE


# --------------------------------------------------------------------------- #
# Hot-load on a live app (#144): install must work after the first request
# --------------------------------------------------------------------------- #

def test_blueprint_registers_after_first_request(app):
    """Flask refuses register_blueprint once the app has served a request, and
    on a live panel that is always the case by the time anyone installs an
    extension — login and dashboard requests come first. The failed hot-load
    used to leave the plugin's routes missing until a restart, and an
    unmatched POST (e.g. the malware scanner's custom-path scan) fell through
    to the static-file route and surfaced as a 405."""
    from flask import Blueprint, jsonify

    c = app.test_client()
    c.get('/')  # flip _got_first_request — the state every live panel is in

    bp = Blueprint('live_reg_bp', __name__)

    @bp.route('/ping', methods=['POST'])
    def ping():
        return jsonify({'ok': True})

    plugin_service._register_blueprint_live(
        app, bp, url_prefix='/api/v1/live-reg')

    resp = c.post('/api/v1/live-reg/ping')
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True}


def test_register_blueprint_live_preserves_first_request_flag(app):
    """The setup guard is waived only for the registration call itself."""
    from flask import Blueprint

    c = app.test_client()
    c.get('/')
    assert app._got_first_request is True

    bp = Blueprint('live_reg_flag_bp', __name__)
    plugin_service._register_blueprint_live(
        app, bp, url_prefix='/api/v1/live-flag')

    assert app._got_first_request is True


def test_register_blueprint_live_on_fresh_app(app):
    """The no-op branch: an app that has not served a request yet registers
    through the plain path."""
    from flask import Blueprint, jsonify

    bp = Blueprint('live_reg_fresh_bp', __name__)

    @bp.route('/ping')
    def ping():
        return jsonify({'ok': True})

    plugin_service._register_blueprint_live(
        app, bp, url_prefix='/api/v1/live-fresh')

    assert app.test_client().get('/api/v1/live-fresh/ping').status_code == 200
