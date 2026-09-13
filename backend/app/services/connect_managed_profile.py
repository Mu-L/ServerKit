"""The managed profile: ServerKit Cloud's signed presentation policy.

When a customer's Managed scope says ServerKit Cloud runs the server for them,
Cloud sends one ``ui.managed_profile`` command over the same signed path as
every other command. What it carries is a *presentation* policy: which
capabilities Cloud has taken over, who holds them, and when the policy lapses.
This module is the only writer of ``managed-profile.json`` — kept next to
``connect.json`` because it belongs to the pairing and leaves with it — and
the only place that decides whether the profile currently applies.

The rules that keep this truthful (plan 25):

- **Hidden, never removed.** The policy changes what the panel *shows*. Every
  route stays registered and every API keeps answering with the caller's
  normal role; support, break-glass, exports and Cloud's own handoff to
  ``/fleet`` all keep landing.
- **Expiry lifts the profile.** A panel that cannot reach Cloud past
  ``expires_at`` shows everything again — a managed customer whose provider
  disappears must be able to run their own server. An expired document is
  kept (the UI says the profile lapsed) but holds nothing.
- **Revoked or unpaired clears it.** A panel that is no longer connected is
  not managed; ``clear()`` runs on local disconnect and on relay revocation.
- **An override lifts it without deleting it.** A support session sees the
  full panel until the override's end time, and the profile returns on its
  own afterwards.

Every apply and lift is written to the panel's audit log with the policy id.
"""
import json
import logging
import os
from datetime import datetime, timezone

from app import paths
from app.services.connect_commands import handler

logger = logging.getLogger(__name__)

PROFILE_FILENAME = 'managed-profile.json'

# The launch capability set (plan 25 §Vocabulary). Cloud validates against its
# own copy before signing; the panel stores what it received and the UI maps
# the ids it knows, so an id from a newer Cloud simply maps to nothing here.
CAPABILITIES = (
    'fleet',               # Servers, Agent Fleet, Fleet Proxy
    'provisioning',        # Cloud Servers
    'config_templates',    # Server Templates
    'updates',             # panel/agent update screens and self-update
    'backups_schedule',    # backup scheduling and destinations; restore stays
    'monitoring_alerts',   # alert rules and channels; live charts stay
    'firewall',
)

ACTION_APPLIED = 'connect.managed_profile_applied'
ACTION_CLEARED = 'connect.managed_profile_cleared'


def profile_path() -> str:
    return os.path.join(paths.SERVERKIT_CONFIG_DIR, PROFILE_FILENAME)


def _read() -> dict:
    path = profile_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning('Could not read %s: %s', path, exc)
        return {}


def _write(payload: dict):
    path = profile_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
        f.write('\n')
    if os.name != 'nt':
        os.chmod(path, 0o600)


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _audit(action: str, details: dict, app=None):
    """Best effort and never in the policy's way: a profile that applied but
    could not be audited still applies, and the warning says why."""
    try:
        if app is None:
            from flask import has_app_context
            if not has_app_context():
                return
        from app.services.audit_service import AuditService
        def go():
            AuditService.log(action, target_type='connect',
                             target_id=details.get('policy_id'), details=details)
        if app is not None:
            with app.app_context():
                go()
        else:
            go()
    except Exception:
        logger.warning('Managed profile: could not write the audit entry', exc_info=True)


def clear(reason: str, app=None, policy_id: str = None) -> bool:
    """Remove the profile document. Returns True when one was there to lift.

    Every lift is audited with the policy id that was in force and the reason
    ('cleared' from Cloud, 'revoked', 'disconnected')."""
    doc = _read()
    path = profile_path()
    if os.path.exists(path):
        try:
            os.unlink(path)
        except OSError as exc:
            logger.warning('Could not remove %s: %s', path, exc)
            return False
    if doc:
        _audit(ACTION_CLEARED, {
            'policy_id': doc.get('policy_id') or policy_id,
            'reason': reason,
            'capabilities': doc.get('capabilities') or [],
        }, app=app)
    return bool(doc)


def current(now: datetime = None) -> dict:
    """The effective profile for the API and the UI.

    ``active`` is the single question the sidebar, pages and in-page controls
    ask. An expired policy or a live override both answer False — the full
    panel shows — while the document's detail still comes back so Settings
    can say exactly what lapsed or who lifted it and until when.
    """
    doc = _read()
    if not doc:
        return {
            'profile': 'none',
            'active': False,
            'capabilities': [],
            'held_by': None,
            'since': None,
            'expires_at': None,
            'support_url': None,
            'policy_id': None,
            'applied_at': None,
            'expired': False,
            'override': None,
        }
    now = now or datetime.now(timezone.utc)
    expires_at = _parse_dt(doc.get('expires_at'))
    expired = expires_at is None or now >= expires_at
    override = doc.get('override') if isinstance(doc.get('override'), dict) else None
    override_until = _parse_dt((override or {}).get('until'))
    override_active = override is not None and override_until is not None and now < override_until
    capabilities = [c for c in (doc.get('capabilities') or []) if isinstance(c, str)]
    active = bool(capabilities) and not expired and not override_active
    return {
        'profile': doc.get('profile') or 'managed',
        'active': active,
        # The UI only ever reads held capabilities when active is True; the
        # full list stays in the document either way for Settings.
        'capabilities': capabilities if active else [],
        'held_capabilities': capabilities,
        'held_by': doc.get('held_by'),
        'since': doc.get('since'),
        'expires_at': doc.get('expires_at'),
        'support_url': doc.get('support_url'),
        'policy_id': doc.get('policy_id'),
        'applied_at': doc.get('applied_at'),
        'expired': expired,
        'override': {'until': override.get('until'), 'reason': override.get('reason')}
        if override_active else None,
    }


def _validate(args: dict) -> dict:
    """The document to store, or ValueError with the sentence Cloud shows."""
    policy_id = str(args.get('policy_id') or '').strip()
    if not policy_id:
        raise ValueError('The policy carried no id, so it was not applied.')
    profile = str(args.get('profile') or 'managed')
    if profile == 'none':
        return {'policy_id': policy_id, 'profile': 'none', 'capabilities': []}
    if profile != 'managed':
        raise ValueError(f'Unknown managed profile {profile!r}.')
    capabilities = args.get('capabilities')
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise ValueError('The policy capabilities were not a list of names.')
    expires_at = _parse_dt(args.get('expires_at'))
    if expires_at is None:
        # A policy without an end never lifts itself, which is exactly the
        # failure the expiry rule exists to prevent. Refuse it rather than
        # store it.
        raise ValueError('The policy carried no expiry, so it was not applied.')
    override = args.get('override')
    if override is not None:
        if not isinstance(override, dict) or _parse_dt(override.get('until')) is None:
            raise ValueError('The policy override carried no end time, so it was not applied.')
        override = {'until': override.get('until'),
                    'reason': str(override.get('reason') or '')[:500]}
    return {
        'policy_id': policy_id,
        'profile': 'managed',
        'capabilities': sorted({c.strip() for c in capabilities if c.strip()}),
        'held_by': str(args.get('held_by') or 'ServerKit Cloud')[:200],
        'since': args.get('since'),
        'expires_at': expires_at.isoformat(),
        'support_url': (str(args.get('support_url'))[:500] if args.get('support_url') else None),
        'override': override,
    }


@handler('ui.managed_profile')
def apply_profile(args: dict, app=None) -> dict:
    """Apply (or clear) the managed profile Cloud signed.

    The result output is a small JSON document carrying the policy id back as
    the acknowledgement; Cloud matches it against what it sent and re-sends
    once when they disagree."""
    try:
        doc = _validate(args or {})
    except ValueError as exc:
        return {'ok': False, 'code': 400, 'summary': str(exc)}

    if doc['profile'] == 'none' or not doc['capabilities']:
        had = clear('cleared', app=app, policy_id=doc['policy_id'])
        summary = ('The managed profile was lifted; the full panel is back.'
                   if had else 'No managed profile was in force; nothing changed.')
        return {'ok': True, 'summary': summary,
                'output': json.dumps({'policy_id': doc['policy_id'], 'applied': False})}

    doc['applied_at'] = datetime.now(timezone.utc).isoformat()
    try:
        _write(doc)
    except OSError as exc:
        return {'ok': False, 'code': 500,
                'summary': f'The profile could not be stored ({exc}), so nothing was applied.'}
    _audit(ACTION_APPLIED, {
        'policy_id': doc['policy_id'],
        'capabilities': doc['capabilities'],
        'held_by': doc['held_by'],
        'since': doc['since'],
        'expires_at': doc['expires_at'],
        'override': doc['override'],
    }, app=app)
    return {'ok': True,
            'summary': f"Managed profile applied: {len(doc['capabilities'])} "
                       f"capabilit{'y is' if len(doc['capabilities']) == 1 else 'ies are'} "
                       f"held by {doc['held_by']} until {doc['expires_at']}.",
            'output': json.dumps({'policy_id': doc['policy_id'], 'applied': True})}
