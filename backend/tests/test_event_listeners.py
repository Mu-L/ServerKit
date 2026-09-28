"""In-process event listeners: the extension-to-core event path (plan 86 §B4)."""
from app.services import event_service
from app.services.event_service import EventService


def test_listeners_hear_emits_without_any_webhook_subscription(app):
    heard = []
    event_service.register_listener('app.deployed', heard.append, source='ext-a')
    try:
        EventService.emit('app.deployed', {'app_id': 7})
        EventService.emit('app.other', {'app_id': 8})
    finally:
        event_service.unregister_listeners('ext-a')
    assert heard == [{'app_id': 7}]


def test_registering_twice_calls_once(app):
    heard = []
    fn = heard.append
    event_service.register_listener('app.deployed', fn, source='ext-a')
    event_service.register_listener('app.deployed', fn, source='ext-a')
    try:
        EventService.emit('app.deployed', {'n': 1})
    finally:
        event_service.unregister_listeners('ext-a')
    assert heard == [{'n': 1}]


def test_a_failing_listener_never_breaks_the_emit(app):
    heard = []

    def boom(payload):
        raise RuntimeError('listener bug')
    event_service.register_listener('app.deployed', boom, source='ext-a')
    event_service.register_listener('app.deployed', heard.append, source='ext-b')
    try:
        EventService.emit('app.deployed', {'n': 1})
    finally:
        event_service.unregister_listeners('ext-a')
        event_service.unregister_listeners('ext-b')
    assert heard == [{'n': 1}]


def test_teardown_drops_only_that_extensions_listeners(app):
    from types import SimpleNamespace
    from app.services.extension_lifecycle import unregister_capabilities
    heard_a, heard_b = [], []
    event_service.register_listener('app.deployed', heard_a.append, source='ext-a')
    event_service.register_listener('app.deployed', heard_b.append, source='ext-b')
    try:
        unregister_capabilities(SimpleNamespace(slug='ext-a'))
        EventService.emit('app.deployed', {'n': 1})
    finally:
        event_service.unregister_listeners('ext-b')
    assert heard_a == [] and heard_b == [{'n': 1}]
