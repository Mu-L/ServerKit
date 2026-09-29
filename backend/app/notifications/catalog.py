"""Event catalog for the Notification Bus.

Maps an ``event_key`` (e.g. ``backup.completed``) to its presentation defaults:
which template renders it, its default severity, its preference category, and a
title template. Callers pass data; the catalog turns ``event_key`` + data into a
title + routing metadata.

Plugins extend the catalog with ``register(...)`` so a plugin event renders
through the same pipeline as a core one.
"""
import logging

logger = logging.getLogger(__name__)

# category is one of: system | security | backups | apps  (matches
# NotificationPreferences.get_categories()). Title is a str.format template
# applied to the event data (missing keys degrade gracefully — see resolve()).
_CATALOG = {
    # --- backups ---
    'backup.completed': {
        'title': 'Backup completed: {app}',
        'template': 'backup_completed',
        'severity': 'success',
        'category': 'backups',
    },
    'backup.failed': {
        'title': 'Backup failed: {app}',
        'template': 'generic',
        'severity': 'critical',
        'category': 'backups',
    },
    'restore.completed': {
        'title': 'Restore completed: {app}',
        'template': 'generic',
        'severity': 'success',
        'category': 'backups',
    },
    'restore.failed': {
        'title': 'Restore failed: {app}',
        'template': 'generic',
        'severity': 'critical',
        'category': 'backups',
    },
    # Restore-proof edge alerts (plan 23 #17): fire once on a state transition
    # when a restore drill or backup verification starts failing / recovers.
    'backup.drill_failed': {
        'title': 'Restore drill failed: {target}',
        'template': 'generic',
        'severity': 'critical',
        'category': 'backups',
    },
    'backup.drill_recovered': {
        'title': 'Restore drills passing again: {target}',
        'template': 'generic',
        'severity': 'success',
        'category': 'backups',
    },
    'backup.verify_failed': {
        'title': 'Backup verification failed: {target}',
        'template': 'generic',
        'severity': 'critical',
        'category': 'backups',
    },
    'backup.verify_recovered': {
        'title': 'Backups verifying cleanly again: {target}',
        'template': 'generic',
        'severity': 'success',
        'category': 'backups',
    },
    # --- security ---
    'security.alert': {
        'title': 'Security alert: {alert_type}',
        'template': 'security_alert',
        'severity': 'critical',
        'category': 'security',
    },
    # --- apps / system ---
    'app.deployed': {
        'title': 'Deployed: {app}',
        'template': 'generic',
        'severity': 'success',
        'category': 'apps',
    },
    # Slot deploys (plan 87): a release that never went live, one that was
    # switched back automatically, and a switch back that did not work.
    'app.deploy_aborted': {
        'title': 'Deploy stopped, site unchanged: {app}',
        'template': 'generic',
        'severity': 'warning',
        'category': 'apps',
    },
    'app.deploy_reverted': {
        'title': 'Deploy switched back automatically: {app}',
        'template': 'generic',
        'severity': 'warning',
        'category': 'apps',
    },
    'app.deploy_revert_failed': {
        'title': 'Deploy failed and switching back failed: {app}',
        'template': 'generic',
        'severity': 'critical',
        'category': 'apps',
    },
    'system.alert': {
        'title': 'System alert on {hostname}',
        'template': 'generic',
        'severity': 'warning',
        'category': 'system',
    },
    'dns.sync_failed': {
        'title': 'DNS sync failed: {record}',
        'template': 'generic',
        'severity': 'warning',
        'category': 'system',
    },
    # The panel host's own hardware moved between boots — a VPS resize, a
    # volume attached or detached (plan 74). Informational: the operator
    # usually caused it, but until now nothing confirmed it landed.
    'host.specs_changed': {
        'title': "This server's hardware changed",
        'template': 'generic',
        'severity': 'info',
        'category': 'system',
    },
    # A storage correlation worth acting on: a mount missing from /etc/fstab,
    # or a filesystem running out while an attached volume sits idle.
    'host.storage_advisory': {
        'title': 'Storage needs attention on this server',
        'template': 'generic',
        'severity': 'warning',
        'category': 'system',
    },
    # The panel host's disk crossed the warning (85%) or critical (95%) line
    # (see app/services/disk_alert_service.py). Sent once per crossing.
    'storage.disk_low': {
        'title': 'Disk {percent}% full on this server',
        'template': 'generic',
        'severity': 'warning',
        'category': 'system',
    },
    # Daily doctor sweep found managed site domains that no longer resolve
    # (see app/services/doctor_service.py:_dns_checks).
    'dns.unresolved': {
        'title': 'Site domains no longer resolve ({count})',
        'template': 'generic',
        'severity': 'warning',
        'category': 'system',
    },
    # Daily registry check found newer versions of installed extensions (#50).
    'extensions.updates_available': {
        'title': 'Extension updates available ({count})',
        'template': 'generic',
        'severity': 'info',
        'category': 'system',
    },
    # Managed-sites publishing readiness — nudged when a site is created but the
    # base-domain / DNS / HTTPS config is only partly set up.
    'sites.publish.no_base_domain': {
        'title': 'Publish your sites at a real domain',
        'template': 'generic',
        'severity': 'warning',
        'category': 'system',
    },
    'sites.publish.http_only': {
        'title': 'Managed sites are served over HTTP',
        'template': 'generic',
        'severity': 'info',
        'category': 'system',
    },
    'sites.publish.no_server_ip': {
        'title': 'Set a server IP so site DNS can auto-create',
        'template': 'generic',
        'severity': 'warning',
        'category': 'system',
    },
    'sites.publish.base_overlaps_panel': {
        'title': 'Site base domain overlaps the panel domain',
        'template': 'generic',
        'severity': 'warning',
        'category': 'system',
    },
    # Daily drift sweep found managed config files that no longer match what
    # the panel would write (see app/services/drift_service.py).
    'drift.detected': {
        'title': 'Configuration drift detected ({count})',
        'template': 'generic',
        'severity': 'warning',
        'category': 'system',
    },
    # Multi-alert monitoring digest (used by the legacy send_all path).
    'monitoring.alert': {
        'title': 'ServerKit alert',
        'template': 'monitoring_alert',
        'severity': 'warning',
        'category': 'system',
    },
    # --- account ---
    'user.welcome': {
        'title': 'Welcome to ServerKit',
        'template': 'welcome',
        'severity': 'info',
        'category': 'system',
    },
    'user.invitation': {
        'title': "You've been invited to ServerKit",
        'template': 'invitation',
        'severity': 'info',
        'category': 'system',
    },
}

# Used when an event_key has no catalog entry — still renders, via generic.html.
DEFAULT_ENTRY = {
    'title': 'Notification',
    'template': 'generic',
    'severity': 'info',
    'category': 'system',
}


# --------------------------------------------------------------------------- #
# Deep links (plan 24 Phase 5)
#
# Maps an ``event_key`` to the relative in-app path its notification should link
# to. Resolved at send time and PERSISTED on the notification row (see
# NotificationBusService.send) so a later route move never breaks old rows.
# Producers may override per-send via ``action_path`` / ``action_label``.
# --------------------------------------------------------------------------- #
_LINKS = {
    # backups / restores
    'backup.completed': '/backups',
    'backup.failed': '/backups',
    'restore.completed': '/backups',
    'restore.failed': '/backups',
    # security
    'security.alert': '/security',
    # apps / deploys
    'app.deployed': '/services',
    'app.deploy_aborted': '/services',
    'app.deploy_reverted': '/services',
    'app.deploy_revert_failed': '/services',
    # system / monitoring
    'system.alert': '/monitoring',
    'monitoring.alert': '/monitoring',
    'host.specs_changed': '/monitoring',
    'host.storage_advisory': '/monitoring',
    'storage.disk_low': '/settings/storage',
    'drift.detected': '/monitoring/doctor',
    'dns.sync_failed': '/domains',
    'dns.unresolved': '/domains',
    'cron.job_failed': '/cron',
    'extensions.updates_available': '/marketplace',
    'setup.incomplete': '/setup',
    # account
    'user.welcome': '/dashboard',
    'user.invitation': '/dashboard',
}

# Button label for the deep link, keyed by destination path.
_LINK_LABELS = {
    '/backups': 'View backups',
    '/security': 'View security',
    '/services': 'View services',
    '/monitoring': 'Open monitoring',
    '/monitoring/doctor': 'Open doctor',
    '/domains': 'View domains',
    '/cron': 'View scheduled tasks',
    '/marketplace': 'Open marketplace',
    '/setup': 'Finish setup',
    '/dashboard': 'Open dashboard',
}

_DEFAULT_LINK_LABEL = 'View details'


def link_for(event_key, data=None):
    """Return ``(action_path, action_label)`` for an event, or ``(None, None)``
    when the event has no known deep link. ``data`` is accepted for future
    per-event parameterization; the map is static today."""
    path = _LINKS.get(event_key)
    if not path:
        return None, None
    return path, _LINK_LABELS.get(path, _DEFAULT_LINK_LABEL)


def register(event_key, title, template='generic', severity='info', category='system'):
    """Register (or override) a catalog event. Safe to call at import time."""
    _CATALOG[event_key] = {
        'title': title,
        'template': template,
        'severity': severity,
        'category': category,
    }


def _safe_format(template_str, data):
    """str.format that won't explode on a missing/extra key."""
    class _Default(dict):
        def __missing__(self, key):
            return '{' + key + '}'
    try:
        return template_str.format_map(_Default(data or {}))
    except Exception:
        return template_str


def resolve(event_key, data=None, severity=None, title=None):
    """Resolve an event into concrete presentation metadata.

    Explicit ``severity``/``title`` args (from notify.send) win over catalog
    defaults. Returns a dict: title, template, severity, category.
    """
    entry = _CATALOG.get(event_key, DEFAULT_ENTRY)
    resolved_title = title or _safe_format(entry['title'], data)
    return {
        'title': resolved_title,
        'template': entry['template'],
        'severity': severity or entry['severity'],
        'category': entry['category'],
    }


def get(event_key):
    """Return the raw catalog entry for an event, or None."""
    return _CATALOG.get(event_key)
