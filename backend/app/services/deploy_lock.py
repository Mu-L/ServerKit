"""Per-app deploy lock (plan 87 §A5).

Two webhook pushes a second apart used to run two deploys of the same app at
once: both stopped the container, both started one, and whichever lost the
race left a half-built release behind. Every path that changes what an app
runs — deploy, rollback, webhook deploy, template update, slot switch — now
takes this lock first, so a second deploy WAITS for the first and then runs
against what the first left live. It never races and is never dropped.

In-process by design: the panel runs as one process (the agent gateway
already requires a single worker, see CLAUDE.md), and the job consumer shares
it. Re-entrant, so a deploy that rolls itself back inside the lock does not
deadlock on its own thread.
"""
import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_guard = threading.Lock()
_locks = {}
_holders = {}


class DeployLockTimeout(RuntimeError):
    pass


def _lock_for(app_id):
    with _guard:
        lock = _locks.get(app_id)
        if lock is None:
            lock = _locks[app_id] = threading.RLock()
        return lock


def is_locked(app_id) -> bool:
    return app_id in _holders


def holder(app_id) -> Optional[str]:
    return _holders.get(app_id)


@contextmanager
def deploy_lock(app_id, label: str = 'deploy', timeout: Optional[float] = 3600,
                log: Optional[Callable[[str], None]] = None):
    """Hold the app's deploy lock; wait (up to ``timeout``) if another has it."""
    lock = _lock_for(app_id)
    if not lock.acquire(blocking=False):
        waiting_on = _holders.get(app_id) or 'another deploy'
        if log:
            log(f'Waiting for {waiting_on} of this app to finish...')
        logger.info('app %s %s waits for %s', app_id, label, waiting_on)
        started = time.monotonic()
        acquired = lock.acquire(timeout=-1 if timeout is None else timeout)
        if not acquired:
            raise DeployLockTimeout(
                f'Timed out after {int(time.monotonic() - started)}s waiting for '
                f'{waiting_on} of this app to finish')
    outer = app_id not in _holders
    if outer:
        _holders[app_id] = label
    try:
        yield
    finally:
        if outer:
            _holders.pop(app_id, None)
        lock.release()


def with_deploy_lock(label: str):
    """Decorate a service method whose first argument (after cls) is app_id.

    A lock timeout comes back as the usual ``{'success': False, 'error'}``.
    """
    import functools

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(cls, app_id, *args, **kwargs):
            try:
                with deploy_lock(app_id, label, log=kwargs.get('log_callback')):
                    return fn(cls, app_id, *args, **kwargs)
            except DeployLockTimeout as exc:
                return {'success': False, 'error': str(exc)}
        return wrapper
    return decorate
