"""Connection properties an installed service offers to apps (plan 86 §C1).

Round trip, both halves: an installed engine (Redis, PostgreSQL) or a template
with a `connection:` block (MinIO) → an app references it through `fromService`
→ the app's effective env carries a URL that parses back to the same host,
port and secret, and both apps' compose overrides join the shared network
that makes the host reachable. Earlier round-trip bugs (plan 82) came from
testing one half only.
"""
import json
from urllib.parse import unquote, urlparse

import pytest
import yaml

from app import db
from app.models import Application
from app.services.compose_env_service import ComposeEnvService
from app.services.env_service import EnvService
from app.services.service_connection_service import (
    SHARED_NETWORK, ServiceConnectionService, container_port)
from app.services.template_service import TemplateService
from tests.factories import make_application

# URL-hostile on purpose: every one of these breaks an unencoded URL.
SECRET = 'p@ss:w/rd#1%'


@pytest.fixture
def installed(app, tmp_path, monkeypatch):
    """Factory for Applications that look exactly like finished template installs."""
    records = {}
    monkeypatch.setattr(TemplateService, 'get_config', classmethod(
        lambda cls: {'repos': [], 'installed': records}))

    def make(name, template_id=None, variables=None, compose=None):
        root = tmp_path / name
        root.mkdir()
        if template_id:
            (root / '.serverkit-template.json').write_text(json.dumps({
                'template_id': template_id, 'variables': variables or {}}))
        (root / 'docker-compose.yml').write_text(yaml.safe_dump(
            compose or {'services': {name: {'image': 'x'}}}))
        row = make_application(db, name=name, status='running', root_path=str(root))
        if template_id:
            records[str(row.id)] = {'template_id': template_id}
        return row
    return make


def _reference(app_row, key, service, prop):
    _var, _created, error = EnvService.set_env_reference(
        app_row.id, key, {'kind': 'service', 'service': service, 'property': prop})
    assert error is None, error


def _env(app_row):
    return EnvService.get_effective_env(app_row.id)


class TestEngineProperties:
    def test_redis_url_round_trips_host_port_and_secret(self, installed):
        installed('cache', 'redis', {'DB_PASSWORD': SECRET, 'PORT': '6390'})
        web = installed('web')
        _reference(web, 'REDIS_URL', 'cache', 'url')

        url = urlparse(_env(web)['REDIS_URL'])
        assert url.scheme == 'redis'
        assert url.hostname == 'cache'          # container name on the network
        assert url.port == 6379                 # container port, not host 6390
        assert unquote(url.password) == SECRET
        assert url.path == '/0'

    def test_postgres_offers_a_connection_string_and_parts(self, installed):
        installed('pg', 'postgresql', {'DB_PASSWORD': SECRET, 'DB_NAME': 'shop',
                                       'PORT': '5439'})
        props = ServiceConnectionService.spec(
            Application.query.filter_by(name='pg').first())['properties']
        url = urlparse(props['connectionString'])
        assert (url.scheme, url.hostname, url.port, url.path) == (
            'postgresql', 'pg', 5432, '/shop')
        assert unquote(url.password) == SECRET
        assert props['database'] == 'shop' and props['password'] == SECRET

    def test_an_unknown_property_names_what_is_offered(self, installed):
        installed('cache', 'redis', {'DB_PASSWORD': 'x'})
        web = installed('web')
        _reference(web, 'NOPE', 'cache', 'bucket')
        assert _env(web)['NOPE'] == ''          # unresolved → empty, never a guess
        _value, error = ServiceConnectionService.resolve(
            Application.query.filter_by(name='cache').first(), 'bucket')
        assert 'offers:' in error and 'url' in error


class TestDeclaredConnection:
    def test_minio_offers_its_endpoint_but_never_the_root_key(self, installed):
        installed('files', 'minio', {'ROOT_USER': 'admin', 'ROOT_PASSWORD': SECRET,
                                     'API_PORT': '9100'})
        web = installed('web')
        _reference(web, 'S3_ENDPOINT', 'files', 'endpoint')

        assert _env(web)['S3_ENDPOINT'] == 'http://files:9000'
        props = ServiceConnectionService.spec(
            Application.query.filter_by(name='files').first())['properties']
        assert SECRET not in json.dumps(props)
        assert 'secretKey' not in props

    def test_placeholders_encode_only_where_asked(self):
        ctx = {'host': 'h', 'port': '1', 'user': 'u', 'password': SECRET, 'database': ''}
        render = ServiceConnectionService._render
        assert render('{password}', ctx, {}) == SECRET
        assert unquote(render('{password_url}', ctx, {})) == SECRET
        assert '@' not in render('{password_url}', ctx, {})
        assert render('{var.KEY}/{var_url.KEY}', ctx, {'KEY': 'a b'}) == 'a b/a%20b'
        assert render('{unknown}', ctx, {}) == '{unknown}'


class TestReachability:
    def test_both_sides_join_the_shared_network(self, installed):
        cache = installed('cache', 'redis', {'DB_PASSWORD': 'x'},
                          compose={'services': {'app': {'image': 'redis'}}})
        web = installed('web', compose={'services': {
            'web': {'image': 'x'},
            'hostnet': {'image': 'y', 'network_mode': 'host'}}})
        _reference(web, 'REDIS_URL', 'cache', 'url')

        for row, joined, skipped in ((cache, ['app'], []), (web, ['web'], ['hostnet'])):
            spec = ComposeEnvService.render_override(row.root_path)
            override = yaml.safe_load(spec['content'])
            assert override['networks'] == {
                SHARED_NETWORK: {'external': True, 'name': SHARED_NETWORK}}
            for name in joined:
                assert override['services'][name]['networks'] == {
                    'default': {}, SHARED_NETWORK: {}}
            for name in skipped:
                assert 'networks' not in override['services'].get(name, {})

    def test_an_app_that_uses_no_service_stays_off_the_network(self, installed):
        plain = installed('plain')
        spec = ComposeEnvService.render_override(plain.root_path)
        assert SHARED_NETWORK not in (spec['content'] or '')

    def test_a_plain_app_sibling_keeps_the_generic_properties(self, installed):
        api = installed('api')
        api.port = 8080
        db.session.commit()
        web = installed('web')
        _reference(web, 'API_URL', 'api', 'url')
        assert _env(web)['API_URL'] == 'http://api:8080'
        assert not ServiceConnectionService.consumes_services(web)


@pytest.mark.parametrize('ports,port_var,expected', [
    (['${BIND_ADDRESS}:${PORT}:6379'], 'PORT', 6379),
    (['${CONSOLE_PORT}:9001', '${API_PORT}:9000'], 'API_PORT', 9000),
    (['8080:80/tcp'], None, 80),
    ([{'target': 5432, 'published': 5439}], None, 5432),
    ([], None, None),
])
def test_container_port(ports, port_var, expected):
    template = {'compose': {'services': {'s': {'ports': ports}}}}
    assert container_port(template, port_var) == expected


def test_rabbitmq_offers_an_amqp_url(installed):
    installed('queue', 'rabbitmq', {'RABBITMQ_USER': 'svc', 'RABBITMQ_PASSWORD': SECRET})
    props = ServiceConnectionService.spec(
        Application.query.filter_by(name='queue').first())['properties']
    url = urlparse(props['url'])
    assert (url.scheme, url.hostname, url.port, url.username) == ('amqp', 'queue', 5672, 'svc')
    assert unquote(url.password) == SECRET
