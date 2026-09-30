"""Proving tests for Phase 1 — stop dropping detected config (plan 17)."""

import pytest

# register the new table on metadata before create_all()
import app.models.application_manifest  # noqa: F401
from app.services.manifest_persistence_service import ManifestPersistenceService
from app.services.git_deploy_service import GitDeployService


def _owner():
    from app.models import User
    return User.query.filter_by(username='testadmin').first()


def _make_app(project_id=None, healthcheck=None):
    from app import db
    from app.models import Application
    row = Application(name='hcapp', app_type='docker', port=9099,
                      user_id=_owner().id, status='running',
                      project_id=project_id, healthcheck_path=healthcheck)
    db.session.add(row)
    db.session.commit()
    return row


def _project():
    from app import db
    from app.models import Project
    from app.services.workspace_service import WorkspaceService
    ws = WorkspaceService.ensure_default_workspace()
    proj = Project(workspace_id=ws.id, name='Proj', slug='proj')
    db.session.add(proj)
    db.session.commit()
    return proj


def test_healthcheck_column_and_todict(client, auth_headers, app):
    app_row = _make_app(healthcheck='/health')
    assert app_row.healthcheck_path == '/health'
    assert app_row.to_dict()['healthcheck_path'] == '/health'


def test_apply_import_seeds_env_and_healthcheck(client, auth_headers, app):
    app_row = _make_app()
    analysis = {
        'recommended': {'healthcheck_path': '/up'},
        'env': [
            {'key': 'LOG_LEVEL', 'value': 'info', 'secret': False},
            {'key': 'API_TOKEN', 'value': 'shh', 'secret': True},   # secret -> not seeded
            {'key': 'EMPTY', 'value': None, 'secret': False},        # empty -> skipped
        ],
    }
    summary = ManifestPersistenceService.apply_import(app_row, analysis, user_id=_owner().id)
    assert summary['healthcheck_path'] == '/up'
    assert app_row.healthcheck_path == '/up'
    assert summary['env_seeded'] == 1

    from app.services.env_service import EnvService
    env = EnvService.get_env_dict(app_row.id)
    assert env.get('LOG_LEVEL') == 'info'
    assert 'API_TOKEN' not in env    # secrets stay placeholders
    assert 'EMPTY' not in env


def test_store_manifest_upserts_one_row_per_project(client, auth_headers, app):
    from app.models.application_manifest import ApplicationManifest
    proj = _project()
    normalized = {'version': 1, 'services': [{'name': 'api', 'type': 'web'}]}
    row1 = ManifestPersistenceService.store_manifest(proj.id, normalized,
                                                     raw_text='version: 1', source_repo='r')
    from app import db
    db.session.commit()
    h1 = row1.manifest_hash

    normalized2 = {'version': 1, 'services': [{'name': 'api', 'type': 'worker'}]}
    row2 = ManifestPersistenceService.store_manifest(proj.id, normalized2)
    db.session.commit()

    assert row1.id == row2.id  # upsert, not insert
    assert row2.manifest_hash != h1
    assert ApplicationManifest.query.filter_by(project_id=proj.id).count() == 1


def test_apply_import_stores_manifest_when_project_and_v1(client, auth_headers, app):
    from app.models.application_manifest import ApplicationManifest
    proj = _project()
    app_row = _make_app(project_id=proj.id)
    analysis = {
        'recommended': {},
        'env': [],
        'manifest_v1_normalized': {'version': 1, 'services': [{'name': 'api', 'type': 'web'}]},
        'manifest_v1_raw': 'version: 1\n',
        'manifest_v1_file': 'serverkit.yaml',
    }
    summary = ManifestPersistenceService.apply_import(app_row, analysis, user_id=_owner().id)
    assert summary['manifest_stored'] is True
    stored = ApplicationManifest.query.filter_by(project_id=proj.id).first()
    assert stored is not None
    assert stored.get_normalized()['services'][0]['name'] == 'api'
