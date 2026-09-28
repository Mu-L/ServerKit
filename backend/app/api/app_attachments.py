# Bucket: PER-APP (plan 29 #9). Reads gate on can_access_app; attach/detach
# gate on can_edit_app for the app AND can_access_app for the service used.
"""Attach services an app uses (plan 86 §C2). Mounted under ``/api/v1/apps``.

  GET    /<app_id>/attachments                 — this app's attachments
  GET    /attachments/storage-services         — installed storage it can use
  POST   /<app_id>/attachments/storage         — {service_app_id}
  DELETE /<app_id>/attachments/<attachment_id> — detach (keeps the bucket)

Attach and detach change the app's env; it applies on the next deploy or
restart, so responses say ``redeploy_required``.
"""
from flask import Blueprint, jsonify, request

from app.exceptions import NotFoundError, ValidationError
from app.middleware.rbac import developer_required, get_current_user, viewer_required
from app.services.app_attachment_service import AppAttachmentService, AttachmentError
from app.services.resource_grant_service import ResourceGrantService

app_attachments_bp = Blueprint('app_attachments', __name__)


@app_attachments_bp.route('/<int:app_id>/attachments', methods=['GET'])
@viewer_required
def list_attachments(app_id):
    app = AppAttachmentService.live_app(app_id)
    if app is None or not ResourceGrantService.can_access_app(get_current_user(), app):
        raise NotFoundError('Application not found')
    return jsonify({'attachments': [a.to_dict() for a in AppAttachmentService.list_for_app(app)]})


@app_attachments_bp.route('/attachments/storage-services', methods=['GET'])
@viewer_required
def list_storage_services():
    user = get_current_user()
    services = [s for s in AppAttachmentService.storage_services()
                if ResourceGrantService.can_access_app(user, s)]
    return jsonify({'services': [{'id': s.id, 'name': s.name, 'status': s.status}
                                 for s in services]})


@app_attachments_bp.route('/<int:app_id>/attachments/storage', methods=['POST'])
@developer_required
def attach_storage(app_id):
    user = get_current_user()
    app = AppAttachmentService.live_app(app_id)
    if app is None or not ResourceGrantService.can_edit_app(user, app):
        raise NotFoundError('Application not found')
    data = request.get_json(silent=True) or {}
    service_app_id = data.get('service_app_id')
    if not isinstance(service_app_id, int) or isinstance(service_app_id, bool):
        raise ValidationError("'service_app_id' is required")
    service = AppAttachmentService.live_app(service_app_id)
    if service is None or not ResourceGrantService.can_access_app(user, service):
        raise NotFoundError('Storage service not found')
    try:
        row, created = AppAttachmentService.attach_storage(app, service, user_id=user.id)
    except AttachmentError as exc:
        raise ValidationError(str(exc)) from exc
    return jsonify({'attachment': row.to_dict(), 'redeploy_required': created}), (201 if created else 200)


@app_attachments_bp.route('/<int:app_id>/attachments/<int:attachment_id>', methods=['DELETE'])
@developer_required
def detach(app_id, attachment_id):
    user = get_current_user()
    app = AppAttachmentService.live_app(app_id)
    if app is None or not ResourceGrantService.can_edit_app(user, app):
        raise NotFoundError('Application not found')
    attachment = AppAttachmentService.get(app, attachment_id)
    if attachment is None:
        raise NotFoundError('Attachment not found')
    warning = AppAttachmentService.detach(attachment, user_id=user.id)
    body = {'success': True, 'redeploy_required': True}
    if warning:
        body['warning'] = warning
    return jsonify(body)
