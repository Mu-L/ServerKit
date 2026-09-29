# Bucket: PER-APP (plan 29 #9). Reads gate on can_access_app; changes gate on
# can_edit_app.
"""A/B slot deploys (plan 87). Mounted under ``/api/v1/apps``.

  GET  /<app_id>/slots               — slots, active slot, eligibility + reasons
  PUT  /<app_id>/slots               — {enabled}: opt in (adopts the current
                                       container as slot a, no restart) or out
  POST /<app_id>/slots/switch-back   — make the standby live again (a rollback
                                       to the release it holds: seconds while
                                       it is warm)
"""
from flask import Blueprint, jsonify, request

from app.exceptions import NotFoundError, ValidationError
from app.middleware.rbac import developer_required, get_current_user, viewer_required
from app.services.resource_grant_service import ResourceGrantService
from app.services.slot_deploy_service import SlotDeployService

app_slots_bp = Blueprint('app_slots', __name__)


def _app(app_id, write=False):
    app = SlotDeployService.live_app(app_id)
    user = get_current_user()
    allowed = (ResourceGrantService.can_edit_app if write
               else ResourceGrantService.can_access_app)
    if app is None or not allowed(user, app):
        raise NotFoundError('Application not found')
    return app, user


@app_slots_bp.route('/<int:app_id>/slots', methods=['GET'])
@viewer_required
def get_slots(app_id):
    app, _ = _app(app_id)
    return jsonify(SlotDeployService.status(app))


@app_slots_bp.route('/<int:app_id>/slots', methods=['PUT'])
@developer_required
def set_slots(app_id):
    app, _ = _app(app_id, write=True)
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled')
    if not isinstance(enabled, bool):
        raise ValidationError("'enabled' must be true or false")
    result = SlotDeployService.set_enabled(app, enabled)
    if not result.get('success'):
        return jsonify(result), 409
    return jsonify(result)


@app_slots_bp.route('/<int:app_id>/slots/switch-back', methods=['POST'])
@developer_required
def switch_back(app_id):
    app, user = _app(app_id, write=True)
    result = SlotDeployService.switch_back(app, user_id=user.id)
    if not result.get('success'):
        return jsonify(result), 409
    return jsonify(result)
