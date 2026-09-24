"""First-run setup code: reaching a fresh panel first is not enough to own it.

Until an account exists, the first registration becomes the administrator.
Without a secret, whoever loads a freshly installed panel's setup page first
(a scanner, not its owner) could claim it. The panel therefore mints a one-time
code while it has no users, shows it only to someone with access to the
server (the installer's summary, the service log, `serverkit setup-code`), and
requires it for that first registration. Creating the first admin by any route
consumes it.

`SERVERKIT_SETUP_CODE` pins the code for automation (release tests, local
tooling); it must be at least MIN_LENGTH characters so it cannot weaken the
generated default.

Stored in plain text on purpose, not in SettingsService.SECRET_KEYS: it only
exists while the panel has no account, whoever can read the database already
controls the server, and encryption at rest let a process with a different
key (the CLI vs the service) read ciphertext back as the "code" — locking the
owner out of their own panel.
"""
import hmac
import logging
import os
import re
import secrets

logger = logging.getLogger(__name__)

SETTING_KEY = 'setup_code'
ENV_OVERRIDE = 'SERVERKIT_SETUP_CODE'
# No 0/O or 1/I/L: the code is read off a terminal and typed by hand.
ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
GROUPS, GROUP_SIZE = 3, 4  # 12 characters, ~59 bits
MIN_LENGTH = 12
SETUP_CODE_PATTERN = re.compile(r'[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}')

_announced = False


def _has_users():
    from app.models.user import User
    return User.query.count() > 0


def _normalize(value):
    return ''.join(ch for ch in str(value or '').upper() if ch.isalnum())


def _generate():
    raw = ''.join(secrets.choice(ALPHABET) for _ in range(GROUPS * GROUP_SIZE))
    return '-'.join(raw[i:i + GROUP_SIZE] for i in range(0, len(raw), GROUP_SIZE))


def _pinned():
    value = os.environ.get(ENV_OVERRIDE, '').strip()
    if value and len(_normalize(value)) < MIN_LENGTH:
        logger.warning('%s ignored: it must have at least %d letters/digits', ENV_OVERRIDE, MIN_LENGTH)
        return None
    return value or None


def required():
    """True while the panel has no account and registration needs the code."""
    return not _has_users()


def ensure():
    """The active code, minting and announcing it if the panel has no users.

    Returns None once an account exists: there is nothing left to protect.
    """
    global _announced
    if _has_users():
        return None
    from app.services.settings_service import SettingsService
    code = _pinned() or SettingsService.get(SETTING_KEY)
    if not code:
        code = _generate()
        SettingsService.set(SETTING_KEY, code)
    if not _announced:
        _announced = True
        logger.warning('First-run setup code: %s (show it again with: serverkit setup-code)', code)
    return code


def verify(submitted):
    """Constant-time comparison, ignoring case, spaces and dashes."""
    expected = ensure()
    if not expected:
        return False
    return hmac.compare_digest(_normalize(submitted).encode(), _normalize(expected).encode())


def consume():
    """Forget the code once the first administrator exists."""
    from app import db
    from app.models.system_settings import SystemSettings
    SystemSettings.query.filter_by(key=SETTING_KEY).delete()
    db.session.commit()
