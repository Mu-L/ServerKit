"""Restore-point adapter for one local nginx vhost lifecycle."""

import os

from app.services.nginx_service import NginxService


def _validate_scope(scope_id, server_id):
    name = str(scope_id or '')
    if (
        not name
        or name in ('.', '..')
        or os.path.isabs(name)
        or os.path.basename(name) != name
        or '/' in name
        or '\\' in name
        or '\x00' in name
    ):
        raise ValueError('nginx vhost scope must be one safe filename')
    if server_id is not None:
        raise ValueError('Remote nginx vhost restore points are not supported')
    return name


def capture(scope_id, server_id=None):
    """Capture file existence, enablement, and exact vhost bytes.

    A present-but-unreadable file is not represented as an empty or absent
    vhost.  Refusing that capture prevents a later restore from deleting state
    that merely could not be observed.
    """
    name = _validate_scope(scope_id, server_id)
    available_path = os.path.join(NginxService.SITES_AVAILABLE, name)
    enabled_path = os.path.join(NginxService.SITES_ENABLED, name)
    exists = os.path.exists(available_path)
    content = NginxService.read_vhost(name) if exists else None
    if exists and content is None:
        raise RuntimeError(f'Existing nginx vhost {name} could not be read')
    payload = {
        'exists': exists,
        'enabled': os.path.exists(enabled_path),
        'content': content,
    }
    slot = _slot_state(name, content)
    if slot:
        payload['slot'] = slot
    return payload


def _slot_app(name):
    from app.models.application import Application
    app = Application.query_active().filter_by(name=name, server_id=None).first()
    return app if app is not None and app.slot_deploys_enabled and app.active_slot else None


def _slot_state(name, content):
    """Which slot the captured vhost points at (plan 87).

    A slot app's vhost proxies to the live slot's port; restoring the file
    without restoring the slot would point nginx at a standby that may be
    stopped. Read from the vhost bytes, not ``app.port``: a switch captures
    the OLD file after it has already moved ``app.port`` to the new slot.
    """
    import re
    from flask import has_app_context
    from app.models.app_slot import AppSlot
    ports = {int(p) for p in re.findall(r'proxy_pass http://127\.0\.0\.1:(\d+)', content or '')}
    if not ports or not has_app_context():
        return None
    app = _slot_app(name)
    if app is None:
        return None
    for row in AppSlot.query.filter_by(application_id=app.id).all():
        if row.host_port in ports:
            return {'active_slot': row.slot, 'port': row.host_port}
    return None


def _restore_slot(name, slot):
    """Make the captured slot live again before the vhost points at it."""
    from app import db
    from app.models.app_slot import AppSlot
    from app.services.docker_service import DockerService
    app = _slot_app(name)
    if app is None or not isinstance(slot, dict) or slot.get('active_slot') == app.active_slot:
        return None
    row = AppSlot.query.filter_by(application_id=app.id, slot=slot.get('active_slot')).first()
    if row is None or not row.container_name or not DockerService.get_container(row.container_name):
        return {'success': False,
                'error': f"Slot {slot.get('active_slot')} of {name} no longer has a container; "
                         'deploy that release again instead of restoring this vhost.'}
    started = DockerService.start_container(row.container_name)
    if not started.get('success'):
        return started
    current = AppSlot.query.filter_by(application_id=app.id, slot=app.active_slot).first()
    if current is not None:
        current.state = 'standby'
    row.state = 'live'
    app.active_slot = row.slot
    app.port = row.host_port
    app.container_id = row.container_name
    db.session.commit()
    return None


def _validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError('nginx restore payload must be an object')
    if not isinstance(payload.get('exists'), bool):
        raise ValueError('nginx restore payload requires an exists flag')
    if not isinstance(payload.get('enabled'), bool):
        raise ValueError('nginx restore payload requires an enabled flag')
    content = payload.get('content')
    if payload['exists'] and not isinstance(content, str):
        raise ValueError('Existing nginx restore payload requires vhost content')
    return payload['exists'], payload['enabled'], content


def restore(scope_id, payload, actor=None, server_id=None):
    """Re-converge exclusively through nginx lifecycle service doors."""
    del actor  # Actor attribution belongs to the generic restore-point service.
    name = _validate_scope(scope_id, server_id)
    exists, enabled, content = _validate_payload(payload)

    if exists and payload.get('slot'):
        refused = _restore_slot(name, payload['slot'])
        if refused:
            return refused

    if not exists:
        return NginxService.delete_site(name)

    written = NginxService.write_vhost(name, content, enable=enabled)
    if not written.get('success'):
        return written

    # write_vhost(enable=False) deliberately preserves a currently enabled
    # site.  A restore target is stronger: reproduce the captured enabled bit.
    if not enabled:
        disabled = NginxService.disable_site(name)
        if not disabled.get('success'):
            return disabled

    return {'success': True, 'message': f'nginx vhost {name} restored'}
