"""Immutable per-deployment image tags and their retention (plan 87 §A1).

Every build used to be tagged ``serverkit-app-<id>:latest``, so "roll back to
v11" re-ran ``:latest`` — the image v12 had just overwritten — and rolled
forward instead. A deploy now builds ``serverkit-app-<id>:d<deployment id>``
(a compose slot deploy: ``serverkit-app-<id>-<service>:d<deployment id>``),
the deployment keeps that tag, and a rollback runs the image that actually
ran.

The price is images per deploy, and plan 85 is about exactly that kind of
leak, so they are pruned on every successful deploy: images of the newest
``keep`` deployments stay, plus anything a slot or the live deployment still
references. ``docker rmi`` without ``-f`` refuses an image a container is
using, which is a second guard, not the first.
"""
import logging
import re

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 3
_TAG = re.compile(r'^d(\d+)$')
_REF_DEPLOYMENT = re.compile(r'd(\d+)$')


def repository(app_id) -> str:
    return f'serverkit-app-{app_id}'


def deploy_image_tag(app_id, deployment_id) -> str:
    return f'{repository(app_id)}:d{deployment_id}'


def _owned(app_id, repo: str) -> bool:
    base = repository(app_id)
    return repo == base or repo.startswith(base + '-')


def _local_refs(app_id):
    """``[(repository, tag)]`` of this app's images (single and per-service)."""
    from app.services.docker_service import DockerService
    result = DockerService.run(['images', '--format', '{{.Repository}}:{{.Tag}}',
                                '--filter', f'reference={repository(app_id)}*'], timeout=30)
    if not result.get('success'):
        return None
    refs = []
    for line in (result.get('output') or '').splitlines():
        repo, _, tag = line.strip().rpartition(':')
        if repo and _owned(app_id, repo):
            refs.append((repo, tag))
    return refs


def _deployment_id(ref):
    match = _REF_DEPLOYMENT.search(ref or '')
    return int(match.group(1)) if match else None


def prune(app_id, keep: int = DEFAULT_KEEP, protect=()) -> list:
    """Remove this app's ``d<N>`` images beyond the newest ``keep`` deployments.

    ``protect`` is image refs that must survive (slot images — for a compose
    slot a ``compose:d<N>`` marker — and the like). Best-effort: returns the
    refs removed, never raises.
    """
    from app.models.deployment import Deployment
    from app.services.docker_service import DockerService
    try:
        refs = _local_refs(app_id)
        if not refs:
            return []
        keep_ids = {d.id for d in Deployment.query.filter_by(app_id=app_id)
                    .order_by(Deployment.id.desc()).limit(max(int(keep), 1)).all()}
        live = Deployment.get_current(app_id)
        if live:
            keep_ids.add(live.id)
        keep_ids.update(i for i in (_deployment_id(r) for r in protect) if i is not None)
        removed = []
        for repo, tag in refs:
            match = _TAG.match(tag)
            if not match or int(match.group(1)) in keep_ids:
                continue            # :latest and anything a human tagged stay
            ref = f'{repo}:{tag}'
            if DockerService.remove_image(ref).get('success'):
                removed.append(ref)
        if removed:
            logger.info('Pruned %d old image(s) of app %s: %s', len(removed), app_id, removed)
        return removed
    except Exception as exc:  # noqa: BLE001 - retention never fails a deploy
        logger.warning('Image retention for app %s failed: %s', app_id, exc)
        return []
