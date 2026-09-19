"""Managed profile (plan 25 M1): apply, expiry, override, clear, audit."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services import connect_managed_profile as mp


@pytest.fixture(autouse=True)
def profile_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mp.paths, 'SERVERKIT_CONFIG_DIR', str(tmp_path))
    yield tmp_path


def _args(**over):
    base = {
        'policy_id': 'mpol_test1',
        'profile': 'managed',
        'capabilities': ['fleet', 'updates'],
        'held_by': 'ServerKit Cloud',
        'since': '2026-10-01T00:00:00Z',
        'expires_at': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        'support_url': 'https://app.serverkit.ai/orgs/acme/servers/dev_1',
        'override': None,
    }
    base.update(over)
    return base


def _read_doc(profile_dir):
    with open(profile_dir / 'managed-profile.json', 'r', encoding='utf-8') as f:
        return json.load(f)


def test_apply_stores_document_and_acks_policy_id(profile_dir):
    out = mp.apply_profile(_args())
    assert out['ok'] is True
    doc = _read_doc(profile_dir)
    assert doc['policy_id'] == 'mpol_test1'
    assert doc['capabilities'] == ['fleet', 'updates']
    assert doc['override'] is None
    assert doc['applied_at']
    ack = json.loads(out['output'])
    assert ack == {'policy_id': 'mpol_test1', 'applied': True}


def test_current_active_profile():
    mp.apply_profile(_args())
    state = mp.current()
    assert state['active'] is True
    assert state['capabilities'] == ['fleet', 'updates']
    assert state['held_by'] == 'ServerKit Cloud'
    assert state['expired'] is False
    assert state['override'] is None


def test_no_profile_is_inactive():
    state = mp.current()
    assert state['active'] is False
    assert state['profile'] == 'none'
    assert state['capabilities'] == []


def test_expired_policy_holds_nothing_but_stays_visible():
    mp.apply_profile(_args(expires_at=(datetime.now(timezone.utc)
                                       - timedelta(hours=1)).isoformat()))
    state = mp.current()
    assert state['active'] is False
    assert state['capabilities'] == []
    assert state['expired'] is True
    # The document's detail survives so Settings can say what lapsed.
    assert state['held_capabilities'] == ['fleet', 'updates']
    assert state['policy_id'] == 'mpol_test1'


def test_override_lifts_profile_until_its_end():
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    mp.apply_profile(_args(override={'until': until, 'reason': 'support session'}))
    state = mp.current()
    assert state['active'] is False
    assert state['capabilities'] == []
    assert state['override'] == {'until': until, 'reason': 'support session'}


def test_elapsed_override_returns_the_profile():
    until = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    mp.apply_profile(_args(override={'until': until, 'reason': 'support session'}))
    state = mp.current()
    assert state['active'] is True
    assert state['capabilities'] == ['fleet', 'updates']
    assert state['override'] is None


def test_profile_none_clears_and_acks(profile_dir):
    mp.apply_profile(_args())
    out = mp.apply_profile(_args(policy_id='mpol_clear1', profile='none',
                                 capabilities=[]))
    assert out['ok'] is True
    assert 'lifted' in out['summary']
    assert not (profile_dir / 'managed-profile.json').exists()
    assert mp.current()['active'] is False
    ack = json.loads(out['output'])
    assert ack == {'policy_id': 'mpol_clear1', 'applied': False}


def test_empty_capability_list_clears():
    mp.apply_profile(_args())
    out = mp.apply_profile(_args(capabilities=[]))
    assert out['ok'] is True
    assert mp.current()['active'] is False


def test_policy_without_expiry_is_refused(profile_dir):
    out = mp.apply_profile(_args(expires_at=None))
    assert out['ok'] is False
    assert 'expiry' in out['summary']
    assert not (profile_dir / 'managed-profile.json').exists()


def test_policy_without_id_is_refused(profile_dir):
    out = mp.apply_profile(_args(policy_id=''))
    assert out['ok'] is False
    assert not (profile_dir / 'managed-profile.json').exists()


def test_override_without_end_is_refused(profile_dir):
    out = mp.apply_profile(_args(override={'reason': 'support session'}))
    assert out['ok'] is False
    assert not (profile_dir / 'managed-profile.json').exists()


def test_clear_reports_whether_one_was_in_force():
    assert mp.clear('revoked') is False
    mp.apply_profile(_args())
    assert mp.clear('revoked') is True
    assert mp.current()['active'] is False
