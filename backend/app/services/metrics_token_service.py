"""The panel's Prometheus scrape token (plan 86 §A4).

``/api/v1/fleet-monitor/prometheus`` only accepted a ``PROMETHEUS_TOKEN`` set in
the environment, which almost no install sets, so an installed Prometheus had
nothing it could scrape. The panel now owns a token: generated on first use,
kept encrypted in an internal vault, and handed to the Prometheus template
through the ``${SERVERKIT_METRICS_TOKEN}`` magic variable. An explicit
``PROMETHEUS_TOKEN`` still works and is accepted alongside it.
"""
import hmac
import os
import secrets
from typing import Optional

from app import db

VAULT_SLUG = 'serverkit-observability'
VAULT_NAME = 'Observability'
SECRET_NAME = 'prometheus-scrape-token'


def _vault():
    from app.models.secret_vault import SecretVault
    vault = SecretVault.query.filter_by(slug=VAULT_SLUG).first()
    if vault is None:
        vault = SecretVault(name=VAULT_NAME, slug=VAULT_SLUG,
                            description='Tokens ServerKit issues to monitoring services')
        db.session.add(vault)
        db.session.flush()
    return vault


def stored_token() -> Optional[str]:
    from app.models.secret_vault import Secret, SecretVault
    vault = SecretVault.query.filter_by(slug=VAULT_SLUG).first()
    if vault is None:
        return None
    row = Secret.query.filter_by(vault_id=vault.id, name=SECRET_NAME).first()
    return row.value if row else None


def get_or_create() -> str:
    token = stored_token()
    if token:
        return token
    from app.services.secret_vault_service import SecretService
    token = secrets.token_urlsafe(32)
    SecretService.upsert_internal_secret(
        _vault().id, SECRET_NAME, token,
        description='Prometheus scrape token for /api/v1/fleet-monitor/prometheus')
    return token


def is_valid(presented: Optional[str]) -> bool:
    """Constant-time check against the env token and the panel's own."""
    if not presented:
        return False
    candidates = [os.environ.get('PROMETHEUS_TOKEN'), stored_token()]
    return any(c and hmac.compare_digest(presented, c) for c in candidates)
