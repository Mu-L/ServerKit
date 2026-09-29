"""Immutable per-deployment image tags and their retention (plan 87 §A1).

Every build used to be tagged ``serverkit-app-<id>:latest``, so "roll back to
v11" re-ran ``:latest`` — the image v12 had just overwritten — and rolled
forward instead. A deploy now builds ``serverkit-app-<id>:d<deployment id>``,
the deployment row keeps that tag, and a rollback runs the image that actually
ran.

The price is one image per deploy, and plan 85 is about exactly that kind of
leak, so the tags are pruned on every successful deploy: the newest
``keep`` deployments' images stay, plus any image a slot or the live
deployment still references. ``docker rmi`` without ``-f`` refuses an image a
container is using, which is a second guard, not the first.
"""
import logging
import re

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 3
_TAG = re.compile(r'^d(\d+)$')


def repository(app_id) -> str:
    return f'serverkit-app-{app_id}'


def deploy_image_tag(app_id, deployment_id) -> str:
    return f'{repository(app_id)}:d{deployment_id}'


def _local_tags(app_id):
    from app.services.docker_service import DockerService
    result = DockerService.run(['images', repository(app_id), '--format', '{{.Tag}}'], timeout=30)
    if not result.get('success'):
        return None
    return [t.strip() for t in (result.get('output') or '').splitlines() if t.strip()]


def prune(app_id, keep: int = DEFAULT_KEEP, protect=()) -> list:
    """Remove this app's ``d<N>`` images beyond the newest ``keep`` deployments.

    ``protect`` is extra image refs that must survive (slot images, the live
    deployment's). Best-effort: returns the refs removed, never raises.
    """
    from app.models.deployment import Deployment
    from app.services.docker_service import DockerService
    try:
        tags = _local_tags(app_id)
        if not tags:
            return []
        recent = [d.image_tag for d in Deployment.query.filter_by(app_id=app_id)
                  .filter(Deployment.image_tag.isnot(None))
                  .order_by(Deployment.id.desc()).limit(max(int(keep), 1)).all()]
        live = Deployment.get_current(app_id)
        keep_refs = set(recent) | {r for r in protect if r}
        if live and live.image_tag:
            keep_refs.add(live.image_tag)
        removed = []
        for tag in tags:
            if not _TAG.match(tag):
                continue            # :latest and anything a human tagged stay
            ref = f'{repository(app_id)}:{tag}'
            if ref in keep_refs:
                continue
            if DockerService.remove_image(ref).get('success'):
                removed.append(ref)
        if removed:
            logger.info('Pruned %d old image(s) of app %s: %s', len(removed), app_id, removed)
        return removed
    except Exception as exc:  # noqa: BLE001 - retention never fails a deploy
        logger.warning('Image retention for app %s failed: %s', app_id, exc)
        return []
