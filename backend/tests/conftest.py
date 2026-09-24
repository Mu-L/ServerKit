"""Pytest fixtures for backend tests (Flask app, DB, client)."""
import os
import sys

import pytest

# Ensure backend root is on path
_backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend not in sys.path:
    sys.path.insert(0, _backend)

# And this directory, so sibling helper modules (popen_guard) import cleanly
# regardless of pytest's import mode.
_tests = os.path.dirname(os.path.abspath(__file__))
if _tests not in sys.path:
    sys.path.insert(0, _tests)

os.environ.setdefault('FLASK_ENV', 'testing')

# Keep the suite offline: unset SERVERKIT_REGISTRY_URL now means "use the
# public serverkit-extensions registry", while set-but-EMPTY means explicitly
# disabled (bundled index only) — which is what hermetic tests want.
os.environ.setdefault('SERVERKIT_REGISTRY_URL', '')

# Tests use a FILE-backed SQLite, not :memory:. Flask-SQLAlchemy serves an
# in-memory SQLite from a single shared connection (StaticPool), so a test that
# drives the DB from a background thread (e.g. the agent send_command round-trip
# e2e) races on that one connection and intermittently dies with
# ObjectDeletedError / PendingRollbackError. A temp file gives each thread its
# own connection with SQLite's own file locking — safe and deterministic. Each
# test still create_all/drop_all's a clean schema (see the `app` fixture).
# The file is per-PROCESS, not a fixed shared name. A shared name breaks two
# ways, both of which surface as errors at fixture setup in a different test
# every run — never in the test that caused them:
#
#   * a previous run whose app threads outlived it still holds the file, so
#     Windows refuses the delete and this run inherits that database's rows;
#     create_all then collides with the already-seeded settings row.
#   * two suites running at once share one database and truncate each other's
#     tables mid-test.
#
# A private file per process costs nothing and removes both.
_OWN_DB = None

if 'TEST_DATABASE_URL' not in os.environ:
    import glob
    import tempfile

    _tmp = tempfile.gettempdir()
    # Sweep the legacy shared file and any database left behind by a crashed
    # run. Best effort: one still held open just stays until the next sweep.
    for _stale in [os.path.join(_tmp, 'serverkit_test.db')] + \
            glob.glob(os.path.join(_tmp, 'serverkit_test_*.db')):
        try:
            os.remove(_stale)
        except OSError:
            pass

    _OWN_DB = os.path.join(_tmp, f'serverkit_test_{os.getpid()}.db').replace('\\', '/')
    os.environ['TEST_DATABASE_URL'] = 'sqlite:///' + _OWN_DB


def pytest_sessionfinish(session, exitstatus):
    """Drop this process's database so temp files can't accumulate."""
    if not _OWN_DB:
        return
    try:
        from app import db
        db.engine.dispose()
    except Exception:
        pass
    try:
        os.remove(_OWN_DB)
    except OSError:
        pass  # Still held by a lingering thread; the next run sweeps it.


# A file-backed SQLite fsyncs on every commit, which makes the suite ~3x
# slower than :memory:. For throwaway test data that durability is pure
# overhead, so disable it and keep the journal in memory — this recovers
# roughly in-memory speed while keeping the per-connection thread-safety the
# file gives us. SQLite-only and scoped to the test process.
#
# Guarded import: some CI jobs run only the stdlib-y system-utils tests with a
# minimal dependency set (no SQLAlchemy). conftest still has to import there,
# and those jobs run no DB-backed test, so the PRAGMA tuning is simply skipped.
try:
    import sqlite3  # noqa: E402
    from sqlalchemy import event  # noqa: E402
    from sqlalchemy.engine import Engine  # noqa: E402

    @event.listens_for(Engine, 'connect')
    def _fast_sqlite_for_tests(dbapi_connection, _record):
        if isinstance(dbapi_connection, sqlite3.Connection):
            cur = dbapi_connection.cursor()
            cur.execute('PRAGMA synchronous=OFF')
            cur.execute('PRAGMA journal_mode=MEMORY')
            cur.execute('PRAGMA temp_store=MEMORY')
            cur.close()

    from sqlalchemy.pool import Pool  # noqa: E402

    @event.listens_for(Pool, 'checkout')
    def _foreign_keys_off_per_checkout(dbapi_connection, _record, _proxy):
        """Every checkout starts at SQLite's default: no FK enforcement.

        A test that turns enforcement on does it on the connection it checked
        out, but its cleanup could land on a different pooled connection, so
        an enforcing one leaked into later tests (CI failed
        test_workspace_scope depending only on which shard it landed in).
        """
        if isinstance(dbapi_connection, sqlite3.Connection):
            dbapi_connection.execute('PRAGMA foreign_keys=OFF')
except ImportError:
    pass


def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'fresh_app: build a PRIVATE Flask app for this test instead of sharing '
        'the session-wide one. Required for tests that mutate application '
        'STRUCTURE — Flask cannot unregister a blueprint or a url rule, so such '
        'a mutation would leak into every later test on a shared app.',
    )
    config.addinivalue_line(
        'markers',
        'real_binaries: run service code against the REAL system binaries '
        '(crontab, nginx -t, ...) instead of the fake_subprocess stub — the '
        'leg that catches what write-only assertions cannot (plan 82 §E). '
        'These tests mutate real system state (e.g. the invoking user\'s '
        'crontab, with backup/restore), so they additionally skip unless '
        'SERVERKIT_REAL_BINARIES=1: select with '
        '`SERVERKIT_REAL_BINARIES=1 pytest tests -m real_binaries` on Linux '
        '(WSL works) or in CI\'s real-binaries job.',
    )
    config.addinivalue_line(
        'markers',
        'docker_builds: run generated buildpack Dockerfiles through the REAL '
        'docker daemon (build + boot + HTTP probe) — the leg that catches '
        'generation bugs which only fail at build or boot time (the '
        '`--omit=dev` dead fallback that shipped `vite: not found`). Pulls '
        'images and builds containers, so it additionally skips unless '
        'SERVERKIT_DOCKER_BUILDS=1: select with '
        '`SERVERKIT_DOCKER_BUILDS=1 pytest tests -m docker_builds` on any OS '
        'with a running docker daemon.',
    )


@pytest.fixture(autouse=True)
def _popen_sbin_guard(monkeypatch):
    """Route every subprocess exec through the runtime sbin guard (plan 75 §B2).

    The static guard in test_sbin_command_guard.py reads literal argv lists —
    at most 57% of raw subprocess sites. This fixture sees the actual argv of
    every exec the suite performs (variables, f-strings, concatenation — the
    43% AST cannot), and fails any bare-name call that only resolves through
    an sbin dir: the plan 74 outage class, where a call works in dev and dies
    under the panel unit's sbin-less PATH.

    Tests that patch subprocess.run/Popen themselves replace this wrapper for
    their duration — the guard only judges execs that really happen. POSIX
    only; sbin semantics do not exist on a Windows dev box.
    """
    if os.name != 'posix':
        yield
        return
    import subprocess
    from popen_guard import GuardedPopen
    monkeypatch.setattr(subprocess, 'Popen', GuardedPopen)
    yield


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Scriptable subprocess seam — the one door for stubbing execs (§G7).

    Opt-in, not autouse: a test that does not ask for it keeps the real
    subprocess (and the §B2 runtime guard above). Asking for it replaces every
    exec entry point on the ``subprocess`` module, which covers BOTH the raw
    calls services still make and the ``run_privileged`` / ``run_unprivileged``
    helpers — they funnel through ``subprocess.run`` too. That is the point:
    when G1 moves services onto ``run_checked()``, this fixture is the single
    place that has to follow, instead of the 12 test files that currently
    hand-roll the same stub.

    Unscripted commands raise; see ``tests/subprocess_stub.py`` for why.
    """
    from subprocess_stub import ScriptedSubprocess
    return ScriptedSubprocess().install(monkeypatch)


@pytest.fixture(scope='session')
def _flask_app():
    """The Flask application and the schema, built ONCE for the test process.

    `create_app('testing')` registers 90+ blueprints and is expensive. Building
    it per test made fixture setup ~90% of this suite's total runtime (plan 64
    Phase 0), so it is built once and shared.

    Booted TWICE on purpose. `create_app()` is not a pure constructor: it seeds
    rows as a side effect of booting (the flagship extension rows that
    test_cloudflare_extraction asserts are "seeded on boot"). So the schema has
    to exist before the app that seeds into it:

        boot #1  ->  create_all()      tables now exist
        boot #2  ->  seeds into them   rows land in real tables

    A single boot would seed against a schema that does not exist yet, and the
    flagship rows would silently never appear. The cost is one extra create_app
    for the whole session.

    The import stays inside the function body on purpose: some CI jobs run only
    the stdlib-y system-utils tests with pytest and nothing else installed (see
    test-system-utils.yml), and conftest still has to import cleanly there.
    Never make a fixture that touches this one autouse.
    """
    from app import create_app
    from app import db as _db

    bootstrap = create_app('testing')
    with bootstrap.app_context():
        _db.create_all()

    return create_app('testing')


@pytest.fixture(scope='function')
def app(request, _flask_app):
    """Per-test database, on the session-wide app unless opted out.

    Still create_all/drop_all per test — Phase 1 deliberately changes only the
    app's lifetime, so that "shared app object" breakage can be diagnosed
    separately from "shared database" breakage.

    Two consequences of sharing one app, both handled here rather than patched
    test by test:

    * Config mutation now leaks where it previously died with the app (e.g.
      test_demo_deploys sets DEMO_DEPLOYS_ENABLED=False and never restores it),
      so config is snapshotted and restored around every test.
    * STRUCTURE mutation cannot be undone at all — Flask has no
      unregister_blueprint. Tests doing that mark themselves `fresh_app` and get
      a private app, paying the old per-test create_app cost knowingly.
    """
    from app import create_app
    from app import db as _db

    # `wp_extension` mounts the WordPress extension's blueprints onto whatever
    # app it is handed, so every test using it is structure-mutating too. Detect
    # it from the fixture graph instead of requiring each such module to
    # remember the marker — miss one and the failure lands in an unrelated test
    # much later in the run.
    needs_private_app = (
        request.node.get_closest_marker('fresh_app') is not None
        or 'wp_extension' in request.fixturenames
        or 'cf_extension' in request.fixturenames
    )
    target = create_app('testing') if needs_private_app else _flask_app

    saved_config = dict(target.config)
    with target.app_context():
        _db.create_all()
        # create_app() seeds the bundled flagship extension rows as a boot side
        # effect, and this drop_all/create_all wipes them. With a per-test app
        # that reseeding came free on every boot; with a shared app it has to be
        # re-run explicitly, or every test after the first sees no flagship and
        # the extension routes answer 503.
        from app.services.plugin_service import seed_flagship_extensions
        seed_flagship_extensions()
        try:
            yield target
        finally:
            _db.session.remove()
            _db.drop_all()
            target.config.clear()
            target.config.update(saved_config)


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def db_session(app):
    """Database session for the current test (same as app's db)."""
    from app import db
    return db


@pytest.fixture
def auth_headers(app):
    """Create an admin user and return headers with valid JWT for API tests."""
    from app import db
    from app.models import User
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    with app.app_context():
        user = User(
            email='testadmin@test.local',
            username='testadmin',
            password_hash=generate_password_hash('testpass'),
            role=User.ROLE_ADMIN,
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
        token = create_access_token(identity=user.id)

    return {'Authorization': f'Bearer {token}'}


# One-door test seeds (plan 77 G1): make_user/headers_for live in
# tests/factories.py; import them from there in test modules. The persona
# fixtures below cover the common "just give me a JWT of role X" case.
from factories import make_user, headers_for  # noqa: E402


@pytest.fixture(scope='session')
def route_rules(_flask_app):
    """Frozen URL rules of the session app (plan 77 G3).

    Tests that only need to check "does route X exist" read THIS instead of
    booting a second app with create_app() — every extra boot re-exposes the
    state-leak classes documented on _flask_app and costs ~seconds.
    """
    return sorted(r.rule for r in _flask_app.url_map.iter_rules())


@pytest.fixture
def developer_headers(app):
    """JWT headers for a plain developer-role user."""
    from app import db
    return headers_for(make_user(db, role='developer'))


@pytest.fixture
def viewer_headers(app):
    """JWT headers for a viewer-role user — the negative case for admin gates."""
    from app import db
    return headers_for(make_user(db, role='viewer'))


@pytest.fixture
def scoping_rbac(app):
    """Five personas over ONE workspace containing ONE application (plan 19
    Decision 2 — the workspace-scoping membership model).

    Every path to the app folds into a single capability tier (see
    app_access_tier): the app owner and a panel admin resolve to 'owner',
    workspace members resolve to their workspace role, and a foreign user has no
    access at all.

        owner  -> owns the app                         (tier 'owner')
        admin  -> panel admin, bypasses to             (tier 'owner')
        member -> workspace 'member' role              (tier 'member')
        viewer -> workspace 'viewer' role              (tier 'viewer')
        foreign-> no membership / no grant             (no access)

    Returns a namespace of per-persona auth headers plus the shared app/workspace
    ids so a suite can drive the /for-app read gate and the admin write gate.
    """
    from types import SimpleNamespace
    from app import db
    from app.models import Application, Workspace
    from app.services.workspace_service import WorkspaceService

    owner = make_user(db, 'scope_owner')
    member = make_user(db, 'scope_member')
    viewer = make_user(db, 'scope_viewer')
    foreign = make_user(db, 'scope_foreign')
    admin = make_user(db, 'scope_admin', role='admin')

    ws = Workspace(name='scope-ws', slug='scope-ws', created_by=owner.id)
    db.session.add(ws)
    db.session.commit()

    WorkspaceService.add_member(ws.id, member.id, role='member')
    WorkspaceService.add_member(ws.id, viewer.id, role='viewer')

    a = Application(name='scope-app', app_type='php', user_id=owner.id,
                    workspace_id=ws.id, root_path='/srv/scope')
    db.session.add(a)
    db.session.commit()

    return SimpleNamespace(
        app_id=a.id,
        ws_id=ws.id,
        owner=headers_for(owner),
        admin=headers_for(admin),
        member=headers_for(member),
        viewer=headers_for(viewer),
        foreign=headers_for(foreign),
    )


# --- WordPress extension (plan 52 Phase 5) -----------------------------------
# WordPress left the tree into the standalone serverkit-wordpress repo. Core
# suites that exercise core features THROUGH the WP surface (domains, site
# routing, url swap, authz, workspace scoping, …) load it from a sibling
# checkout when one is available and skip cleanly when it isn't — a fresh
# ServerKit clone's suite must pass without the extension source.


def sibling_extension_dir(slug, env_var=None):
    """A standalone extension repo's checkout dir, or None (plan 52 cutover).

    Resolution mirrors _wp_ext_tests_dir: an explicit env override first
    (SERVERKIT_<SLUG>_DIR, dashes/prefix dropped), then the default sibling
    checkout next to the ServerKit workspace. Suites that exercise an
    extracted extension mount it from here and skip cleanly when absent.
    """
    env_name = env_var or 'SERVERKIT_{}_DIR'.format(
        slug.replace('serverkit-', '').replace('-', '_').upper())
    env = os.environ.get(env_name)
    candidates = [env] if env else []
    candidates.append(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))), slug))
    for cand in candidates:
        if cand and os.path.isfile(os.path.join(cand, 'plugin.json')):
            return cand
    return None


def mount_sibling_extension(app, slug, url_prefix, entry_module, entry_attr,
                            display_name=None):
    """Mirror a real install of an extracted extension from its sibling repo:
    register its backend dir as the ``app.plugins.<slug>`` package, seed an
    active InstalledPlugin row (manifest included), and mount its blueprint.
    The plan 52 cutover analogue of _wp_support.ensure_plugin."""
    import importlib.util
    import json as _json

    src = sibling_extension_dir(slug)
    if not src:
        pytest.skip(f'{slug} checkout not available '
                    f'(set SERVERKIT_{slug.replace("serverkit-", "").replace("-", "_").upper()}_DIR)')
    backend_dir = os.path.join(src, 'backend')
    pkg = f'app.plugins.{slug}'
    if pkg not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            pkg, os.path.join(backend_dir, '__init__.py'),
            submodule_search_locations=[backend_dir])
        module = importlib.util.module_from_spec(spec)
        sys.modules[pkg] = module
        spec.loader.exec_module(module)

    from app import db
    from app.models.plugin import InstalledPlugin
    with open(os.path.join(src, 'plugin.json'), encoding='utf-8') as f:
        manifest = _json.load(f)
    plugin = InstalledPlugin.query.filter_by(slug=slug).first()
    if not plugin:
        plugin = InstalledPlugin(
            name=slug, display_name=display_name or manifest.get('display_name', slug),
            slug=slug, version=manifest.get('version', '0.0.0'),
            status=InstalledPlugin.STATUS_ACTIVE,
            has_backend=True, url_prefix=url_prefix,
        )
        db.session.add(plugin)
    plugin.status = InstalledPlugin.STATUS_ACTIVE
    plugin.manifest = manifest
    db.session.commit()

    mod = __import__(f'{pkg}.{entry_module}', fromlist=[entry_attr])
    bp = getattr(mod, entry_attr)
    if bp.name not in app.blueprints:
        app.register_blueprint(bp, url_prefix=url_prefix)
    return plugin


@pytest.fixture
def cf_extension(app):
    """serverkit-cloudflare-ops mounted from its sibling checkout (skip when
    absent). Structure-mutating — forces a private app like wp_extension."""
    return mount_sibling_extension(
        app, 'serverkit-cloudflare-ops', '/api/v1/cloudflare',
        'cloudflare', 'cloudflare_bp', display_name='Cloudflare Zone Ops')


def _wp_ext_tests_dir():
    """The standalone serverkit-wordpress repo's tests/ dir, or None."""
    env = os.environ.get('SERVERKIT_WORDPRESS_DIR')
    candidates = [env] if env else []
    # default sibling checkout: <workspace>/serverkit-wordpress next to
    # <workspace>/ServerKit
    candidates.append(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))), 'serverkit-wordpress'))
    for cand in candidates:
        if cand and os.path.isfile(os.path.join(cand, 'tests', '_wp_support.py')):
            return os.path.join(cand, 'tests')
    return None


@pytest.fixture
def wp_extension(app):
    """Mount the WordPress extension on the test app from its standalone repo
    (active row + blueprint mounts + core_hooks seams), or skip when the
    source isn't available. Set SERVERKIT_WORDPRESS_DIR to its checkout."""
    tests_dir = _wp_ext_tests_dir()
    if not tests_dir:
        pytest.skip('serverkit-wordpress source not available '
                    '(set SERVERKIT_WORDPRESS_DIR to its checkout)')
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import _wp_support
    return _wp_support.ensure_plugin(app)


@pytest.fixture
def wp_extension_package(app):
    """Load the WordPress extension's backend package (app.plugins
    .serverkit-wordpress.*) from its standalone repo AND mark it active, which
    is what the lazy wordpress_bridge needs to resolve service classes. Skips
    when the source isn't available. For route-level tests use wp_extension,
    which additionally mounts the blueprints and registers the core_hooks seams.

    The active row is not optional. `wordpress_bridge.ensure_loadable()` gates
    on an ACTIVE InstalledPlugin row, not on importability -- deliberately, so
    a DISABLED extension's services stay unreachable even though its modules
    still import (audit F2). Loading the package alone therefore stopped being
    enough, and every test whose code path reached the bridge failed with
    WordPressExtensionMissingError while looking exactly like "the extension is
    not installed".
    """
    tests_dir = _wp_ext_tests_dir()
    if not tests_dir:
        pytest.skip('serverkit-wordpress source not available '
                    '(set SERVERKIT_WORDPRESS_DIR to its checkout)')
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import _wp_support
    mods = _wp_support.load_ext()

    from app import db
    from app.models.plugin import InstalledPlugin
    with app.app_context():
        row = InstalledPlugin.query.filter_by(slug='serverkit-wordpress').first()
        if not row:
            row = InstalledPlugin(
                name='serverkit-wordpress', display_name='WordPress',
                slug='serverkit-wordpress', version='1.0.0',
                status=InstalledPlugin.STATUS_ACTIVE,
                has_backend=True, url_prefix='/api/v1/wordpress',
            )
            db.session.add(row)
        else:
            row.status = InstalledPlugin.STATUS_ACTIVE
        db.session.commit()
    return mods
