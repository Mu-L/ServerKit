import json
from pathlib import Path

from app.services.test_sandbox_service import DISTROS, TestSandboxService


REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / 'backend' / 'app' / 'data' / 'distro-catalog.json'


def _catalog():
    return json.loads(CATALOG_PATH.read_text(encoding='utf-8'))


def test_catalog_has_all_25_published_targets_and_unique_probes():
    catalog = _catalog()
    assert catalog['schema_version'] == 1
    assert len(catalog['targets']) == 25
    assert len({target['id'] for target in catalog['targets']}) == 25
    assert sum(target['published_status'] == 'tested' for target in catalog['targets']) == 5
    assert sum(target['published_status'] == 'supported' for target in catalog['targets']) == 9
    assert sum(target['published_status'] == 'community' for target in catalog['targets']) == 11

    probes = [
        probe
        for target in catalog['targets']
        for probe in target.get('probes', [])
    ]
    assert len(probes) == 26
    assert len({probe['key'] for probe in probes}) == 26
    assert sum(probe['fidelity'] == 'exact' for probe in probes) == 23
    assert sum(probe['fidelity'] == 'proxy' for probe in probes) == 3


def test_sandbox_registry_is_generated_from_catalog_plus_legacy_probes():
    catalog = _catalog()
    catalog_keys = {
        probe['key']
        for target in catalog['targets']
        for probe in target.get('probes', [])
    }
    legacy_keys = {probe['key'] for probe in catalog['legacy_probes']}

    assert set(DISTROS) == catalog_keys | legacy_keys
    assert {'debian13', 'rocky10', 'alma10', 'centosstream10', 'arch', 'gentoo'} <= set(DISTROS)
    assert DISTROS['rhel10ubi']['fidelity'] == 'proxy'
    assert DISTROS['ubuntu24']['full_image']
    assert DISTROS['ubuntu26']['quick_image'] == 'ubuntu:26.04'
    assert DISTROS['ubuntu26']['full_image'] is None
    assert DISTROS['rocky10']['full_image'] is None


def test_sandbox_api_metadata_exposes_target_and_fidelity():
    listed = {item['key']: item for item in TestSandboxService.list_distros()}
    assert listed['amazon2023']['target'] == 'amazon-linux'
    assert listed['amazon2023']['fidelity'] == 'exact'
    assert listed['sles15bci']['fidelity'] == 'proxy'
    assert listed['debian11']['fidelity'] == 'legacy'
