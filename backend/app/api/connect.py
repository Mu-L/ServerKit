"""Connect API — expose this panel's ServerKit Cloud connection state.

Pairing itself happens via `serverkit connect` on the host (see
app/services/connect_client.py); this read-only endpoint lets the Settings
UI render the connection state.
"""
from flask import Blueprint, jsonify

from app.middleware.rbac import admin_required
from app.services import connect_client

connect_bp = Blueprint('connect', __name__)


@connect_bp.route('/status', methods=['GET'])
@admin_required
def get_status():
    """Current ServerKit Cloud connection state (unpaired/paired_offline/...)."""
    return jsonify(connect_client.status())


@connect_bp.route('/managed-profile', methods=['GET'])
@admin_required
def get_managed_profile():
    """The managed profile in force (plan 25). Read-only: the signed
    ui.managed_profile command handler is the only writer, and lifting the
    profile from ServerKit Cloud — never from here — is the only off switch
    other than expiry or revocation."""
    from app.services import connect_managed_profile
    return jsonify(connect_managed_profile.current())
