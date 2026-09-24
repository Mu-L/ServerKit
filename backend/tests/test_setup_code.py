"""First-run setup code: the first registration (which becomes the admin)
needs the code only someone with access to the server can read."""
import pytest

from app.services import setup_code_service


def _register(client, **extra):
    body = {'email': 'owner@example.com', 'username': 'owner', 'password': 'correct-horse-9', **extra}
    return client.post('/api/v1/auth/register', json=body)


@pytest.fixture(autouse=True)
def _no_pinned_code(monkeypatch):
    monkeypatch.delenv(setup_code_service.ENV_OVERRIDE, raising=False)


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    # /register allows 3 attempts a minute; each test starts with its own budget.
    from app import limiter
    limiter.reset()
    yield
    limiter.reset()


def test_setup_status_says_a_code_is_needed_without_revealing_it(client, app):
    response = client.get('/api/v1/auth/setup-status')
    assert response.status_code == 200
    assert response.json['setup_code_required'] is True
    with app.app_context():
        code = setup_code_service.ensure()
    assert code and code not in response.get_data(as_text=True)


@pytest.mark.parametrize('submitted', [None, '', 'AAAA-BBBB-CCCC'])
def test_first_registration_without_the_right_code_is_refused(client, app, submitted):
    extra = {} if submitted is None else {'setup_code': submitted}
    response = _register(client, **extra)
    assert response.status_code == 403
    assert response.json['code'] == 'auth.setup_code_invalid'
    from app.models import User
    with app.app_context():
        assert User.query.count() == 0


def test_the_code_claims_the_admin_once_and_is_then_consumed(client, app):
    with app.app_context():
        code = setup_code_service.ensure()
    # Typed by hand: case, spaces and missing dashes do not matter.
    response = _register(client, setup_code=' ' + code.lower().replace('-', ' '))
    assert response.status_code == 201
    assert response.json['user']['role'] == 'admin'
    from app.models import SystemSettings
    with app.app_context():
        assert SystemSettings.query.filter_by(key=setup_code_service.SETTING_KEY).first() is None
        assert setup_code_service.ensure() is None
        assert setup_code_service.required() is False
    assert client.get('/api/v1/auth/setup-status').json['setup_code_required'] is False
    # The same code cannot open a second account.
    again = _register(client, email='late@example.com', username='late', setup_code=code)
    assert again.status_code == 403


def test_the_code_survives_a_process_with_another_encryption_key(app, monkeypatch):
    # The service mints it; `serverkit setup-code` (another process, maybe
    # another environment) must print the same code, not ciphertext.
    from cryptography.fernet import Fernet
    with app.app_context():
        monkeypatch.setenv('SERVERKIT_ENCRYPTION_KEY', Fernet.generate_key().decode())
        first = setup_code_service.ensure()
        monkeypatch.setenv('SERVERKIT_ENCRYPTION_KEY', Fernet.generate_key().decode())
        assert setup_code_service.ensure() == first
        assert setup_code_service.verify(first)
        assert setup_code_service.SETUP_CODE_PATTERN.fullmatch(first)


def test_an_existing_account_needs_no_code(app, auth_headers):
    with app.app_context():
        assert setup_code_service.required() is False
        assert setup_code_service.ensure() is None
        assert setup_code_service.verify('anything') is False


def test_a_pinned_code_is_used_only_when_long_enough(app, monkeypatch):
    with app.app_context():
        monkeypatch.setenv(setup_code_service.ENV_OVERRIDE, 'release-lab-pinned-code')
        assert setup_code_service.ensure() == 'release-lab-pinned-code'
        assert setup_code_service.verify('RELEASE LAB PINNED CODE')
        # Too short to be safe: ignored, so the generated code still applies.
        monkeypatch.setenv(setup_code_service.ENV_OVERRIDE, 'abc123')
        generated = setup_code_service.ensure()
        assert generated != 'abc123' and len(generated.replace('-', '')) == 12
