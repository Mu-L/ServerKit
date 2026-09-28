"""Wire an installed Grafana to Prometheus / Loki (plan 86 §A4).

Attaching ``metrics`` (Prometheus) or ``logs`` (Loki) to a Grafana install
creates the data source through Grafana's HTTP API, at the loopback port its
template publishes, with the admin credentials recorded at install. A metrics
attach also imports one bundled dashboard of the panel's own fleet metrics.
Data sources get a fixed uid per attached service, so re-attaching updates in
place and detaching deletes exactly what was created.

Grafana reaches the data source by container name over the shared service
network (the attachment makes Grafana join it on its next deploy).
"""
import base64
import json
import os
import urllib.error
import urllib.request
from typing import Dict, Optional

DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              'data', 'grafana', 'serverkit-overview.json')
DATASOURCE_TYPES = {'metrics': 'prometheus', 'logs': 'loki'}


class GrafanaError(Exception):
    pass


def datasource_uid(kind: str, service_name: str) -> str:
    return f'serverkit-{kind}-{service_name}'[:40]


class GrafanaProvisioner:
    def __init__(self, base_url: str, user: str, password: str):
        self.base_url = base_url.rstrip('/')
        token = base64.b64encode(f'{user}:{password}'.encode()).decode()
        self._auth = f'Basic {token}'

    @classmethod
    def for_app(cls, grafana_app) -> 'GrafanaProvisioner':
        from app.services.service_connection_service import _install_variables
        variables = _install_variables(grafana_app)
        port = variables.get('HTTP_PORT') or (str(grafana_app.port) if grafana_app.port else '')
        if not port or not variables.get('ADMIN_PASSWORD'):
            raise GrafanaError('the Grafana install has no recorded port or admin password')
        return cls(f'http://127.0.0.1:{port}', variables.get('ADMIN_USER') or 'admin',
                   variables['ADMIN_PASSWORD'])

    def _call(self, method: str, path: str, body: Optional[Dict] = None, ok=(200,)):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f'{self.base_url}{path}', data=data, method=method,
                                     headers={'Authorization': self._auth,
                                              'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            if exc.code in ok:
                return exc.code, {}
            detail = exc.read().decode(errors='replace')[:200]
            raise GrafanaError(f'{method} {path}: HTTP {exc.code} {detail}') from exc
        except OSError as exc:
            raise GrafanaError(f'Grafana is not reachable at {self.base_url}: {exc}') from exc

    def upsert_datasource(self, kind: str, service_name: str, url: str) -> str:
        uid = datasource_uid(kind, service_name)
        body = {'uid': uid, 'name': f'{service_name} ({DATASOURCE_TYPES[kind]})',
                'type': DATASOURCE_TYPES[kind], 'access': 'proxy', 'url': url,
                'isDefault': False}
        status, _ = self._call('GET', f'/api/datasources/uid/{uid}', ok=(200, 404))
        if status == 404:
            self._call('POST', '/api/datasources', body)
        else:
            self._call('PUT', f'/api/datasources/uid/{uid}', body)
        return uid

    def delete_datasource(self, uid: str) -> None:
        self._call('DELETE', f'/api/datasources/uid/{uid}', ok=(200, 404))

    def import_dashboard(self, datasource: str) -> None:
        with open(DASHBOARD_PATH, encoding='utf-8') as fh:
            dashboard = json.loads(fh.read().replace('${DS_UID}', datasource))
        self._call('POST', '/api/dashboards/db',
                   {'dashboard': dashboard, 'overwrite': True,
                    'message': 'Imported by ServerKit'})
