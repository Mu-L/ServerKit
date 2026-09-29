"""Per-app deploy settings (plan 87).

Stored as one JSON object on ``Application.deploy_settings``; NULL or a
missing key means the default below. ``effective(app)`` is what every reader
uses, so a default changes in one place.
"""
import json

DEFAULTS = {
    # Health gate (§A3): how long a new release may take to answer, whether a
    # 4xx on the health path still counts as up, and how many answers in a row
    # it takes.
    'healthcheck_timeout': 120,
    'healthcheck_allow_4xx': False,
    'healthcheck_consecutive': 3,
    # How many deployments' images stay on disk for rollback (§A1).
    'keep_images': 3,
}

# key -> (type, min, max). Anything outside is rejected, not clamped: a typo'd
# timeout of 0 would make every deploy fail.
_RULES = {
    'healthcheck_timeout': (int, 5, 1800),
    'healthcheck_allow_4xx': (bool, None, None),
    'healthcheck_consecutive': (int, 1, 20),
    'keep_images': (int, 1, 20),
}


def stored(app) -> dict:
    raw = getattr(app, 'deploy_settings', None)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def effective(app) -> dict:
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in stored(app).items() if k in DEFAULTS})
    return merged


def get(app, key):
    return effective(app)[key]


def validate(changes) -> tuple:
    """``(clean, error)``: the recognised keys coerced to their types."""
    if not isinstance(changes, dict):
        return None, 'deploy_settings must be an object'
    clean = {}
    for key, value in changes.items():
        if key not in _RULES:
            return None, f'Unknown deploy setting: {key}'
        kind, low, high = _RULES[key]
        if value is None:
            clean[key] = None          # back to the default
            continue
        if kind is bool:
            if not isinstance(value, bool):
                return None, f'{key} must be true or false'
            clean[key] = value
            continue
        if isinstance(value, bool):
            return None, f'{key} must be a number'
        try:
            number = kind(value)
        except (TypeError, ValueError):
            return None, f'{key} must be a number'
        if (low is not None and number < low) or (high is not None and number > high):
            return None, f'{key} must be between {low} and {high}'
        clean[key] = number
    return clean, None


def update(app, changes) -> tuple:
    """Merge validated ``changes`` into the app's stored settings (no commit)."""
    clean, error = validate(changes)
    if error:
        return None, error
    current = stored(app)
    for key, value in clean.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    app.deploy_settings = json.dumps(current) if current else None
    return effective(app), None
