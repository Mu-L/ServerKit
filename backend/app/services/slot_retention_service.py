"""Remove the previous version's install once the update has proven itself
(plan 85 §B1).

Updates install into the idle blue/green slot (``/opt/serverkit-a`` or
``-b``) and flip the ``/opt/serverkit`` symlink, so the version that was
running stays on disk — ~1.2 GB — as an instant rollback. That is worth
keeping for a day; weeks later nobody rolls back to it, and the next update
overwrites that slot anyway.

``storage.previous_slot_hours`` (default 24, ``0`` keeps the slot until the
next update) sets how long after the switch the idle slot is removed. The
switch time is the symlink's own mtime: the updater recreates it on every
switch. While an update holds its lock nothing is touched.
"""
import logging
import os
import shutil
import time

logger = logging.getLogger(__name__)

INSTALL_DIR = os.environ.get('SERVERKIT_INSTALL_DIR', '/opt/serverkit')
UPDATE_LOCK = os.environ.get('SERVERKIT_LOCK_FILE', '/var/lock/serverkit-update.lock')
DEFAULT_HOURS = 24


def _update_running(lock_path):
    """True while ``serverkit update`` holds its flock."""
    try:
        import fcntl
    except ImportError:          # not a POSIX host
        return False
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return False             # no lock file: no update has ever run
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def previous_slot(install_dir=INSTALL_DIR):
    """The idle slot directory, or None when this is not a blue/green install
    or there is no idle slot on disk."""
    if not os.path.islink(install_dir):
        return None
    active = os.path.realpath(install_dir)
    for suffix in ('-a', '-b'):
        slot = install_dir + suffix
        if os.path.isdir(slot) and not os.path.islink(slot) \
                and os.path.realpath(slot) != active:
            return slot
    return None


def remove_previous_slot(hours=None, install_dir=INSTALL_DIR, lock_path=UPDATE_LOCK,
                         now=None):
    """Delete the idle slot once the active one has been live ``hours``.
    Returns ``{'removed': path, 'bytes': n}`` or None when nothing was due."""
    if hours is None:
        from app.services.settings_service import SettingsService
        try:
            hours = int(SettingsService.get('storage.previous_slot_hours', DEFAULT_HOURS))
        except (TypeError, ValueError):
            hours = DEFAULT_HOURS
    if hours <= 0:
        return None
    slot = previous_slot(install_dir)
    if slot is None:
        return None
    live_for = (now or time.time()) - os.lstat(install_dir).st_mtime
    if live_for < hours * 3600 or _update_running(lock_path):
        return None

    from app.services.disk_reclaim_service import _path_size
    size = _path_size(slot)
    shutil.rmtree(slot, ignore_errors=True)
    if os.path.exists(slot):
        logger.warning('Could not fully remove the previous install at %s', slot)
        return None
    logger.info('Removed the previous install %s (%d bytes), live %.0f h',
                slot, size, live_for / 3600)
    return {'removed': slot, 'bytes': size}
