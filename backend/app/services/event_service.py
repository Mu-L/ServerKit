"""Service for event emission and webhook delivery."""
import json
import logging
from datetime import datetime, timedelta

from app import db
from app.models.event_subscription import EventSubscription, EventDelivery
from app.queue_bus.consumers.webhook_consumer import enqueue_webhook_delivery
from app.services.telemetry_service import generate_correlation_id
from app.services.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

# Mapping from audit log actions to event types
AUDIT_TO_EVENT = {
    'app.create': 'app.created',
    'app.update': 'app.updated',
    'app.delete': 'app.deleted',
    'app.start': 'app.started',
    'app.stop': 'app.stopped',
    'app.restart': 'app.restarted',
    'app.deploy': 'app.deployed',
    'backup.create': 'backup.created',
    'backup.restore': 'backup.restored',
    'user.create': 'user.created',
    'user.login': 'user.login',
    'api_key.create': 'api_key.created',
    'api_key.revoke': 'api_key.revoked',
}

# Available events catalog
EVENT_CATALOG = [
    {'type': 'app.created', 'category': 'Applications', 'description': 'An application was created'},
    {'type': 'app.updated', 'category': 'Applications', 'description': 'An application was updated'},
    {'type': 'app.deleted', 'category': 'Applications', 'description': 'An application was deleted'},
    {'type': 'app.started', 'category': 'Applications', 'description': 'An application was started'},
    {'type': 'app.stopped', 'category': 'Applications', 'description': 'An application was stopped'},
    {'type': 'app.restarted', 'category': 'Applications', 'description': 'An application was restarted'},
    {'type': 'app.deployed', 'category': 'Applications', 'description': 'An application was deployed'},
    {'type': 'container.started', 'category': 'Docker', 'description': 'A container was started'},
    {'type': 'container.stopped', 'category': 'Docker', 'description': 'A container was stopped'},
    {'type': 'backup.created', 'category': 'Backups', 'description': 'A backup was created'},
    {'type': 'backup.restored', 'category': 'Backups', 'description': 'A backup was restored'},
    {'type': 'user.created', 'category': 'Users', 'description': 'A user was created'},
    {'type': 'user.login', 'category': 'Users', 'description': 'A user logged in'},
    {'type': 'security.alert', 'category': 'Security', 'description': 'A security alert was triggered'},
    {'type': 'ssl.expiring', 'category': 'SSL', 'description': 'An SSL certificate is expiring soon'},
    {'type': 'domain.created', 'category': 'Domains', 'description': 'A domain was created'},
    {'type': 'domain.deleted', 'category': 'Domains', 'description': 'A domain was deleted'},
    {'type': 'api_key.created', 'category': 'API', 'description': 'An API key was created'},
    {'type': 'api_key.revoked', 'category': 'API', 'description': 'An API key was revoked'},
    # Ported from the retired Workflow Builder event bus (plan 45 Phase 4) so
    # Automations (tramo) workflows can trigger on them via the events bridge.
    {'type': 'health.check_failed', 'category': 'Monitoring', 'description': 'An environment health check failed'},
    {'type': 'git.push', 'category': 'Deployments', 'description': 'A git push was received for an app'},
    {'type': 'monitor.high_cpu', 'category': 'Monitoring', 'description': 'A server crossed its high-CPU alert threshold'},
    {'type': 'monitor.high_memory', 'category': 'Monitoring', 'description': 'A server crossed its high-memory alert threshold'},
]

# Extension-registered event types (plan 52 D4 hook inversion). Extensions add
# their own catalog entries at load via register_event_types() — e.g. the
# WordPress extension contributes the wordpress.* lifecycle events. The core
# catalog above keeps only core events; an absent extension's types simply
# don't exist (subscriptions to them just never match, which is the graceful
# degradation — emission never validates against the catalog).
_EXTENSION_EVENT_TYPES = []
# event type -> registrant slug (audit F5): lets disable/uninstall drop
# exactly one extension's types (audit F1).
_EXTENSION_EVENT_SOURCES = {}


def register_event_types(entries, source=None):
    """Register extension-owned event types into the catalog.

    ``entries``: list of ``{'type', 'category', 'description'}`` dicts.
    Idempotent — types already in the core catalog or already registered are
    skipped, so per-boot re-registration never duplicates. ``source`` is the
    registrant extension's slug, recorded so teardown
    (:func:`unregister_event_types`) removes exactly that extension's types.
    """
    known = {e['type'] for e in EVENT_CATALOG}
    known.update(e['type'] for e in _EXTENSION_EVENT_TYPES)
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        etype = entry.get('type')
        if not etype or etype in known:
            continue
        _EXTENSION_EVENT_TYPES.append({
            'type': etype,
            'category': entry.get('category') or 'Extensions',
            'description': entry.get('description') or '',
        })
        _EXTENSION_EVENT_SOURCES[etype] = source
        known.add(etype)
    if source:
        logger.info(f'Registered event types from {source}')


def unregister_event_types(source):
    """Drop every event type registered by ``source`` (disable/uninstall
    teardown, audit F1). Returns the number removed."""
    if not source:
        return 0
    doomed = {t for t, s in _EXTENSION_EVENT_SOURCES.items() if s == source}
    if not doomed:
        return 0
    _EXTENSION_EVENT_TYPES[:] = [
        e for e in _EXTENSION_EVENT_TYPES if e['type'] not in doomed]
    for t in doomed:
        del _EXTENSION_EVENT_SOURCES[t]
    return len(doomed)


# In-process listeners (plan 86 §B4): the extension-to-core event path. An
# extension's core_hooks registers a callable for an event type; every emit of
# that type calls it with the payload, best-effort, whether or not any webhook
# subscription exists. event type -> [(fn, source slug)].
_LISTENERS = {}


def register_listener(event_type, fn, source=None):
    """Call ``fn(payload)`` on every ``event_type`` emit. Idempotent per
    (event type, fn) so per-boot re-registration never doubles a call."""
    entries = _LISTENERS.setdefault(event_type, [])
    if any(existing is fn for existing, _ in entries):
        return
    entries.append((fn, source))


def unregister_listeners(source):
    """Drop every listener ``source`` registered (disable/uninstall)."""
    removed = 0
    for event_type in list(_LISTENERS):
        kept = [(fn, s) for fn, s in _LISTENERS[event_type] if s != source]
        removed += len(_LISTENERS[event_type]) - len(kept)
        _LISTENERS[event_type] = kept
    return removed


def _notify_listeners(event_type, payload):
    for fn, source in list(_LISTENERS.get(event_type, [])):
        try:
            fn(payload)
        except Exception as e:  # noqa: BLE001 - a listener never breaks the emitter
            logger.warning(f'Event listener from {source} failed on {event_type}: {e}')


def clear_registered_event_types():
    """Drop every extension-registered event type. Tests only."""
    _EXTENSION_EVENT_TYPES.clear()
    _EXTENSION_EVENT_SOURCES.clear()


class EventService:
    """Service for emitting events and delivering webhooks."""

    @staticmethod
    def get_available_events():
        """Return the event catalog: core events + extension-registered types."""
        return EVENT_CATALOG + list(_EXTENSION_EVENT_TYPES)

    @staticmethod
    def emit_wp(event_type, site, **extra):
        """Emit a wordpress.* lifecycle event with a standard payload derived from a
        WordPressSite. Best-effort — never raises into the caller (so emitting an
        event can never break the WP operation that triggered it)."""
        try:
            payload = {'event': event_type, 'timestamp': datetime.utcnow().isoformat()}
            if site is not None:
                payload['site_id'] = getattr(site, 'id', None)
                app = getattr(site, 'application', None)
                payload['site_name'] = app.name if app else f'site {getattr(site, "id", "?")}'
            payload.update(extra)
            EventService.emit(event_type, payload)
        except Exception as e:
            logger.error(f'Failed to emit {event_type}: {e}')

    @staticmethod
    def emit(event_type, payload, user_id=None):
        """Emit an event to in-process listeners and all matching subscriptions."""
        _notify_listeners(event_type, payload)
        subscriptions = EventSubscription.query.filter_by(is_active=True).all()
        matching = [s for s in subscriptions if s.matches_event(event_type)]

        if not matching:
            return

        for sub in matching:
            delivery = EventDelivery(
                subscription_id=sub.id,
                event_type=event_type,
                status=EventDelivery.STATUS_PENDING,
                correlation_id=generate_correlation_id(),
            )
            delivery.set_payload(payload)
            db.session.add(delivery)

        db.session.commit()

        # Dispatch deliveries via the queue bus
        for sub in matching:
            pending = EventDelivery.query.filter_by(
                subscription_id=sub.id,
                event_type=event_type,
                status=EventDelivery.STATUS_PENDING,
            ).order_by(EventDelivery.created_at.desc()).first()

            if pending:
                try:
                    enqueue_webhook_delivery(pending.id)
                except Exception as e:
                    logger.error(f'Failed to enqueue webhook delivery {pending.id}: {e}')

    @staticmethod
    def emit_for_audit(action, target_type, target_id, details, user_id):
        """Emit an event based on an audit log action."""
        event_type = AUDIT_TO_EVENT.get(action)
        if not event_type:
            return

        payload = {
            'event': event_type,
            'timestamp': datetime.utcnow().isoformat(),
            'target_type': target_type,
            'target_id': target_id,
            'user_id': user_id,
            'details': details or {},
        }

        try:
            EventService.emit(event_type, payload, user_id)
        except Exception as e:
            logger.error(f'Failed to emit event {event_type}: {e}')

    @staticmethod
    def retry_failed():
        """Retry failed deliveries that are due by re-enqueueing them."""
        now = datetime.utcnow()
        pending = EventDelivery.query.filter(
            EventDelivery.status == EventDelivery.STATUS_PENDING,
            EventDelivery.next_retry_at <= now,
            EventDelivery.attempts > 0,
        ).all()

        for delivery in pending:
            try:
                enqueue_webhook_delivery(delivery.id)
            except Exception as e:
                logger.error(f'Failed to enqueue retry for delivery {delivery.id}: {e}')

    @staticmethod
    def send_test(subscription_id):
        """Send a test event to a subscription via the queue bus."""
        sub = EventSubscription.query.get(subscription_id)
        if not sub:
            return None

        delivery = EventDelivery(
            subscription_id=sub.id,
            event_type='test.ping',
            status=EventDelivery.STATUS_PENDING,
        )
        delivery.set_payload({
            'event': 'test.ping',
            'timestamp': datetime.utcnow().isoformat(),
            'message': 'This is a test webhook from ServerKit',
        })
        db.session.add(delivery)
        db.session.commit()

        try:
            enqueue_webhook_delivery(delivery.id)
        except Exception as e:
            logger.error(f'Failed to enqueue test delivery {delivery.id}: {e}')
        return delivery

    @staticmethod
    def get_deliveries(subscription_id, page=1, per_page=50):
        """Get delivery history for a subscription."""
        return EventDelivery.query.filter_by(
            subscription_id=subscription_id
        ).order_by(
            EventDelivery.created_at.desc()
        ).paginate(page=page, per_page=per_page, error_out=False)

    @staticmethod
    def retry_delivery(delivery):
        """Reset a delivery atomically, then enqueue it after commit."""
        with unit_of_work():
            delivery.status = EventDelivery.STATUS_PENDING
            delivery.next_retry_at = None

        try:
            enqueue_webhook_delivery(delivery.id)
        except Exception as exc:
            # The durable pending row remains retryable by the queue sweep.
            logger.error('Failed to enqueue delivery retry %s: %s', delivery.id, exc)
        db.session.refresh(delivery)
        return delivery

    @staticmethod
    def cleanup_old_deliveries(days=30):
        """Purge old delivery records."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        deleted = EventDelivery.query.filter(EventDelivery.created_at < cutoff).delete()
        db.session.commit()
        return deleted

