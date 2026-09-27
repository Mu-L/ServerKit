"""Warn before the panel host's disk fills (plan 85 §D3).

Host disk alerts existed but were off by default and not started at boot,
and Doctor's ``disk.headroom`` failure notified nobody — so a box reached
100% with no warning. This check is on by default and runs every 15 minutes.

Admins are notified once when usage crosses the warning line
(``storage.disk_alert_percent``, default 85; ``0`` disables), and once more at
95%. The level resets only after usage drops 5 points below the warning line,
so a disk hovering at the line does not notify every tick.
"""
import logging
import shutil

logger = logging.getLogger(__name__)

PERCENT_SETTING = 'storage.disk_alert_percent'
LEVEL_SETTING = 'storage.disk_alert_level'
DEFAULT_PERCENT = 85
CRITICAL_PERCENT = 95
HYSTERESIS = 5


def _usage():
    from app.services import storage_profile_service
    du = shutil.disk_usage(storage_profile_service._probe_path())
    return du.used / du.total * 100 if du.total else None, du.free


def _human(n):
    from app.services.disk_reclaim_service import human_bytes
    return human_bytes(n)


def check(usage=None):
    """Compare usage with the thresholds; notify on an upward crossing.
    Returns the level now recorded (``''``, ``'warning'`` or ``'critical'``)."""
    from app.services.settings_service import SettingsService

    try:
        warn_at = int(SettingsService.get(PERCENT_SETTING, DEFAULT_PERCENT))
    except (TypeError, ValueError):
        warn_at = DEFAULT_PERCENT
    if warn_at <= 0:
        return ''
    percent, free = usage if usage is not None else _usage()
    if percent is None:
        return SettingsService.get(LEVEL_SETTING) or ''

    previous = SettingsService.get(LEVEL_SETTING) or ''
    if percent >= max(CRITICAL_PERCENT, warn_at):
        level = 'critical'
    elif percent >= warn_at:
        level = 'warning'
    elif percent < warn_at - HYSTERESIS:
        level = ''
    else:
        level = previous        # inside the hysteresis band: hold

    rank = {'': 0, 'warning': 1, 'critical': 2}
    if rank[level] > rank.get(previous, 0):
        try:
            from app.plugins_sdk import notify
            notify.send(
                'storage.disk_low', to='admins',
                severity='critical' if level == 'critical' else 'warning',
                data={'percent': round(percent), 'free': _human(free), 'level': level,
                      'message': f'The disk is {round(percent)}% full ({_human(free)} free). '
                                 'Open Storage to see what is using it and free up space.'})
        except Exception as e:  # noqa: BLE001 — notification is best-effort
            logger.warning('could not send storage.disk_low notification: %s', e)
    if level != previous:
        SettingsService.set(LEVEL_SETTING, level)
    return level
