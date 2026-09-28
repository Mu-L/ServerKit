"""The panel's own Prometheus scrape token (plan 86 §A4)."""
import pytest

from app.services import metrics_token_service
from app.services.template_service import TemplateService

URL = '/api/v1/fleet-monitor/prometheus'


@pytest.fixture(autouse=True)
def _no_env_token(monkeypatch):
    monkeypatch.delenv('PROMETHEUS_TOKEN', raising=False)


def test_the_endpoint_accepts_the_panels_token(app, client, monkeypatch):
    monkeypatch.setattr('app.services.fleet_monitor_service.fleet_monitor_service'
                        '.get_prometheus_metrics', lambda: 'serverkit_up 1\n')
    assert client.get(URL).status_code == 401
    token = metrics_token_service.get_or_create()
    assert client.get(f'{URL}?token=wrong').status_code == 401
    ok = client.get(f'{URL}?token={token}')
    assert ok.status_code == 200 and b'serverkit_up 1' in ok.data
    assert client.get(URL, headers={'X-Prometheus-Token': token}).status_code == 200


def test_an_env_token_still_works(app, monkeypatch):
    monkeypatch.setenv('PROMETHEUS_TOKEN', 'from-env')
    assert metrics_token_service.is_valid('from-env')
    assert not metrics_token_service.is_valid('')
    assert not metrics_token_service.is_valid(None)


def test_the_token_is_created_once_and_kept_encrypted(app):
    from app.models.secret_vault import Secret
    token = metrics_token_service.get_or_create()
    assert metrics_token_service.get_or_create() == token
    row = Secret.query.filter_by(name=metrics_token_service.SECRET_NAME).one()
    assert token not in row.encrypted_value


def test_the_magic_variable_resolves_to_the_panels_token(app):
    template = {'files': [{'path': '/x', 'content': 'token: ${SERVERKIT_METRICS_TOKEN}'}]}
    generated = TemplateService.collect_magic_variables(template)
    assert generated == {'SERVERKIT_METRICS_TOKEN': metrics_token_service.get_or_create()}


def test_the_prometheus_template_scrapes_the_panel(app):
    template = TemplateService.get_template('prometheus')['template']
    (config,) = [f['content'] for f in template['files'] if f['path'].endswith('prometheus.yml')]
    assert 'metrics_path: /api/v1/fleet-monitor/prometheus' in config
    assert "token: ['${SERVERKIT_METRICS_TOKEN}']" in config
    assert 'post_install' not in template, 'the old script wrote to a path the container never saw'
