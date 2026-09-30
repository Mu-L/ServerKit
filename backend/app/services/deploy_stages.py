"""Deploy Console stage markers (plan 87 §F).

The Deploy Console's PipelineStrip renders ``job.plan.steps``; the job runner
advances it with ``set_step``. A deploy passes through several services
(DeploymentService → SlotDeployService → the health gate) and threading a
callback through every signature between them would touch a dozen stubs, so
the runner installs a callback for the current context and any layer can say
"now in stage X" with :func:`mark`. Outside a job, :func:`mark` does nothing.
"""
import contextvars
from contextlib import contextmanager
from typing import Callable, Optional

_current: contextvars.ContextVar = contextvars.ContextVar('deploy_stage_callback', default=None)


def mark(name: str) -> None:
    callback: Optional[Callable[[str], None]] = _current.get()
    if callback is not None:
        try:
            callback(name)
        except Exception:  # noqa: BLE001 - progress display never breaks a deploy
            pass


@contextmanager
def reporting(callback: Callable[[str], None]):
    token = _current.set(callback)
    try:
        yield
    finally:
        _current.reset(token)
