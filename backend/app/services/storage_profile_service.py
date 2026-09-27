"""Size-aware storage defaults (plan 85 §C).

A 25 GB VPS and a 500 GB server should not keep the same 30 days of
telemetry: on the small box ServerKit's own history competes with the apps
for the disk. This picks retention and log-size defaults from the size of the
filesystem the panel lives on.

These are defaults, never limits: every value is an ordinary setting the
operator can change, and nothing here refuses to run on small hardware.
"""
import logging
import os
import shutil

logger = logging.getLogger(__name__)

SMALL_DISK_BYTES = 50 * 1024 ** 3

# Values per profile. ``standard`` is what every install shipped with before
# profiles existed, so an existing row still holding it was never chosen.
PROFILES = {
    'small': {
        'telemetry.retention_days': 7,
        'jobs.retention_days': 7,
        'history.retention_days': 30,
        'storage.docker_log_max_size': '10m',
    },
    'standard': {
        'telemetry.retention_days': 30,
        'jobs.retention_days': 14,
        'history.retention_days': 90,
        'storage.docker_log_max_size': '50m',
    },
}

# Marks that the one-time adjustment below already ran on this install.
APPLIED_SETTING = 'storage.size_profile_applied'


def _probe_path():
    from app.paths import SERVERKIT_DIR
    return SERVERKIT_DIR if os.path.isdir(SERVERKIT_DIR) else os.path.abspath(os.sep)


def disk_total_bytes(path=None):
    try:
        return shutil.disk_usage(path or _probe_path()).total
    except OSError:
        return None


def profile_name(total_bytes=None):
    """``small`` below 50 GB, else ``standard``. An unreadable disk size falls
    back to ``standard`` — the defaults that were always there."""
    if total_bytes is None:
        total_bytes = disk_total_bytes()
    if total_bytes is not None and total_bytes < SMALL_DISK_BYTES:
        return 'small'
    return 'standard'


def default_for(key, total_bytes=None):
    return PROFILES[profile_name(total_bytes)].get(key)


def apply_to_existing(total_bytes=None):
    """Once per install: on a small disk, move settings that still hold the
    old standard value to the small-disk value. A value the operator changed
    to anything else is left alone. Returns the keys that changed."""
    from app.services.settings_service import SettingsService

    if SettingsService.get(APPLIED_SETTING):
        return []
    changed = []
    if profile_name(total_bytes) == 'small':
        for key, value in PROFILES['small'].items():
            current = SettingsService.get(key)
            if current is None or str(current) == str(PROFILES['standard'][key]):
                SettingsService.set(key, value)
                changed.append(key)
    SettingsService.set(APPLIED_SETTING, True)
    if changed:
        logger.info('Small disk: set %s to the small-disk defaults', ', '.join(changed))
    return changed
