"""Garage admin operations through its own CLI (plan 86 §C2).

``garage json-api <Endpoint> <payload>`` invokes the admin API over the node's
RPC socket and prints the JSON result, so the panel drives Garage with
``docker exec`` — no admin port to publish, no token to handle, and structured
output instead of scraped CLI text. Garage logs go to stderr; stdout is the
JSON answer.
"""
import json
from typing import Any, Dict, Optional


class GarageError(Exception):
    pass


# One node, one zone. Capacity is the layout's accounting number, not a
# quota; a large value keeps the single node from ever reading as "full".
LAYOUT_ZONE = 'dc1'
LAYOUT_CAPACITY = 1 << 40


class GarageAdmin:
    def __init__(self, container: str):
        self.container = container

    def call(self, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        from app.services.docker_service import DockerService

        argv = ['exec', self.container, '/garage', 'json-api', endpoint]
        if payload is not None:
            argv.append(json.dumps(payload))
        result = DockerService.run(argv, timeout=60)
        if not result.get('success'):
            detail = (result.get('stderr') or result.get('error') or '').strip()
            raise GarageError(f'{endpoint} failed: {detail.splitlines()[-1] if detail else "no output"}')
        try:
            return json.loads(result.get('output') or 'null')
        except ValueError as exc:
            raise GarageError(f'{endpoint} returned non-JSON output') from exc

    # -- cluster -------------------------------------------------------------

    def ensure_layout(self) -> bool:
        """Give the single node a role once. Returns True when it applied one.

        A fresh Garage node has no layout and refuses every bucket operation
        until one is applied; later calls are no-ops.
        """
        status = self.call('GetClusterStatus') or {}
        if int(status.get('layoutVersion') or 0) > 0:
            return False
        nodes = status.get('nodes') or []
        if not nodes:
            raise GarageError('Garage reports no nodes')
        self.call('UpdateClusterLayout', {'roles': [{
            'id': nodes[0]['id'], 'zone': LAYOUT_ZONE,
            'capacity': LAYOUT_CAPACITY, 'tags': [],
        }]})
        self.call('ApplyClusterLayout', {'version': 1})
        return True

    # -- buckets and keys ----------------------------------------------------

    def ensure_bucket(self, alias: str) -> str:
        """The id of the bucket named ``alias``, creating it when missing."""
        try:
            info = self.call('GetBucketInfo', {'globalAlias': alias})
            if info and info.get('id'):
                return info['id']
        except GarageError:
            pass
        created = self.call('CreateBucket', {'globalAlias': alias}) or {}
        if not created.get('id'):
            raise GarageError(f'could not create bucket {alias}')
        return created['id']

    def create_key(self, name: str) -> Dict[str, str]:
        created = self.call('CreateKey', {'name': name}) or {}
        if not created.get('accessKeyId') or not created.get('secretAccessKey'):
            raise GarageError('CreateKey returned no key')
        return {'access_key_id': created['accessKeyId'],
                'secret_access_key': created['secretAccessKey']}

    def allow(self, bucket_id: str, access_key_id: str) -> None:
        """Read and write on one bucket; never owner (no bucket admin)."""
        self.call('AllowBucketKey', {
            'bucketId': bucket_id, 'accessKeyId': access_key_id,
            'permissions': {'read': True, 'write': True, 'owner': False},
        })

    def delete_key(self, access_key_id: str) -> None:
        self.call('DeleteKey', {'id': access_key_id})
