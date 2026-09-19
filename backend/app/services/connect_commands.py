"""Running the signed commands ServerKit Cloud sends.

A command arrives as one `open` frame with kind `command`, carrying a JWT
ServerKit Cloud signed with its Ed25519 command key. Before anything runs, this module
checks four things, in this order and with no shortcuts:

1. **Signature.** Against the JWKS the panel already trusts for the release
   feed. An unsigned or wrongly signed command is not a command.
2. **Audience.** The `device_id` claim is this panel and nobody else, so a
   command minted for another server cannot be replayed here.
3. **Freshness and replay.** `exp` must be in the future and the `nonce` must
   be one we have not seen. A command runs at most once.
4. **Scope.** The action's consent scope must be in the JWT's signed `scopes`
   claim. ServerKit Cloud checks the customer's grant before it signs; this is the
   second check, on the machine that would do the work.

A command's arguments arrive one of two ways. Where this panel has published
an end-to-end key, they are `sealed`: encrypted to that key
by ServerKit Cloud, so the relay carries them without being able to read them — which is
what makes it safe to send a storage credential through it. Otherwise they are
`args`, protected by TLS on both legs. `unseal_args` is where the two meet, and
a sealed payload this panel cannot open is a refused command rather than a
guess.

Only then is the action looked up in HANDLERS. An action this client version
does not implement fails with that sentence rather than silently succeeding —
the operator sees "this server's ServerKit is too old for that" in Cloud,
which is true and actionable.

Results go back as a `command_result` ingest frame: one `running` ack as work
starts, one terminal frame with the outcome.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

# How long a nonce is remembered. ServerKit Cloud's commands live an hour by default, so
# an hour and a half covers every command that could still be valid.
NONCE_TTL_S = 5400
# A handler that has not finished by then is abandoned and reported failed;
# ServerKit Cloud gives up at the command's own timeout anyway.
DEFAULT_TIMEOUT_S = 600

# action -> the consent scope that must be granted for it (ServerKit Cloud's
# backend/commands.py ACTION_SCOPES; the storage actions share one consent).
ACTION_SCOPES = {
    'storage.assign': 'storage.manage',
    'storage.unassign': 'storage.manage',
    'storage.test': 'storage.manage',
    'storage.report': 'storage.manage',
    # Policy repairs, bundled as the consent screen groups them:
    # agreeing to "turn the firewall back on" is not agreeing to "install
    # operating system packages".
    'security.firewall.enable': 'security.remediate',
    'security.fail2ban.enable': 'security.remediate',
    'security.2fa.require': 'security.remediate',
    'packages.security_upgrade': 'packages.upgrade',
    'backup.verify': 'backup.run',
}


def scope_for(action: str) -> str:
    return ACTION_SCOPES.get(action, action)


class CommandRejected(Exception):
    """The command never ran and never will. Carries the sentence Cloud shows."""


class _Nonces:
    """Seen nonces with an expiry. Small, in-process, and rebuilt on restart —
    a restart cannot replay a command because `exp` still has to hold."""

    def __init__(self):
        self._seen = {}
        self._lock = threading.Lock()

    def check_and_add(self, nonce: str, now: float = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            for key, at in list(self._seen.items()):
                if now - at > NONCE_TTL_S:
                    self._seen.pop(key, None)
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


NONCES = _Nonces()


# ==================== verification ====================


def verify(token: str, jwks: dict, device_id: str, granted_scopes=None,
           now: float = None) -> dict:
    """Return the command claims, or raise CommandRejected with the reason."""
    import base64

    import jwt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not token:
        raise CommandRejected('The command carried no signature.')
    if not jwks or not (jwks.get('keys') or []):
        raise CommandRejected('This panel could not read ServerKit Cloud\'s signing keys, '
                              'so it did not run the command.')
    try:
        header = jwt.get_unverified_header(token)
        key = next((k for k in jwks['keys'] if k.get('kid') == header.get('kid')), None)
        if key is None or key.get('crv') != 'Ed25519':
            raise CommandRejected('The command was signed with a key this panel does not trust.')
        raw = base64.urlsafe_b64decode(key['x'] + '=' * (-len(key['x']) % 4))
        claims = jwt.decode(token, Ed25519PublicKey.from_public_bytes(raw),
                            algorithms=['EdDSA'], options={'verify_exp': False})
    except CommandRejected:
        raise
    except Exception as exc:
        raise CommandRejected(f'The command signature did not verify ({exc}).')

    if claims.get('device_id') != device_id:
        raise CommandRejected('The command was addressed to a different server.')

    now = now if now is not None else time.time()
    exp = claims.get('exp')
    if not exp or float(exp) < now:
        raise CommandRejected('The command had already expired when it arrived.')

    nonce = claims.get('nonce')
    if not nonce or not NONCES.check_and_add(nonce, now):
        raise CommandRejected('This command has already been run once.')

    action = claims.get('action') or ''
    needed = scope_for(action)
    signed = claims.get('scopes') or []
    if needed not in signed:
        raise CommandRejected(f'The command did not carry the {needed!r} consent scope.')
    if granted_scopes is not None and needed not in granted_scopes:
        raise CommandRejected(f'{needed!r} is not granted on this panel. Grant it in ServerKit '
                              f'Cloud and nothing needs to be re-paired.')
    return claims


# ==================== the handler registry ====================

HANDLERS = {}


def handler(action: str):
    def register(fn):
        HANDLERS[action] = fn
        return fn
    return register


def unseal_args(claims: dict) -> dict:
    """The command's arguments, whether they arrived sealed or in the clear.

    A sealed payload we cannot open raises: running a command with arguments
    we guessed at is worse than not running it.
    """
    sealed = claims.get('sealed')
    if not sealed:
        return claims.get('args') or {}
    from app.services import connect_keys
    return connect_keys.open_sealed(claims.get('device_id') or '', sealed)


def run(claims: dict, app=None) -> dict:
    """Execute one verified command. Returns {ok, summary, code, output}.

    A handler that raises is a failure with its own words, never a crash that
    leaves Cloud waiting for a result that never comes.
    """
    action = claims.get('action') or ''
    fn = HANDLERS.get(action)
    if fn is None:
        return {'ok': False, 'code': 501,
                'summary': f'This server\'s ServerKit does not know how to run {action}. '
                           f'Update it and try again.'}
    try:
        args = unseal_args(claims)
    except Exception as exc:
        logger.warning('Connect command %s: could not unseal its arguments: %s', action, exc)
        return {'ok': False, 'code': 400,
                'summary': 'This command arrived encrypted to a key this panel could not use, '
                           'so it was not run. Nothing was changed. Reconnect the panel to '
                           'ServerKit Cloud to publish a fresh key.'}
    try:
        out = fn(args, app)
    except Exception as exc:
        logger.warning('Connect command %s failed: %s', action, exc, exc_info=True)
        return {'ok': False, 'code': 500, 'summary': f'{type(exc).__name__}: {exc}'}
    if not isinstance(out, dict):
        return {'ok': True, 'code': 0, 'summary': str(out)[:2000] if out else None}
    return {
        'ok': bool(out.get('ok', True)),
        'code': out.get('code', 0 if out.get('ok', True) else 1),
        'summary': (str(out['summary'])[:2000] if out.get('summary') else None),
        'output': out.get('output'),
    }


def result_frame(cmd_id: str, state: str, result: dict = None, stream: str = None) -> dict:
    """The command_result ingest frame the relay forwards to ServerKit Cloud."""
    payload = {'cmd_id': cmd_id, 'state': state}
    if result:
        payload['result_code'] = result.get('code')
        payload['result_summary'] = result.get('summary')
        if result.get('output'):
            payload['output'] = result['output']
    return {'s': stream or f'res_{cmd_id}', 't': 'open', 'k': 'command_result', 'p': payload}


# Importing the handler modules is what registers them. Kept at the bottom so
# a handler module may import this one for @handler.
from app.services import connect_managed_profile, connect_policy, connect_storage  # noqa: E402,F401
