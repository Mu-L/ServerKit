"""Resource observation must survive sandbox teardown without inventing success."""

import json
from pathlib import Path

from app.services import test_sandbox_service as sandbox


def test_collects_runtime_artifact_before_container_cleanup(tmp_path, monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ['docker', 'cp']:
            Path(cmd[-1]).write_text(json.dumps({'status': 'measured', 'samples': []}))
        return {'success': True}

    monkeypatch.setattr(sandbox, 'run_checked', run)
    with (tmp_path / 'ubuntu24.log').open('w') as log:
        result = sandbox.TestSandboxService._measure_runtime('test-container', log)
    assert result.startswith('measured')
    assert calls[0][:3] == ['docker', 'exec', 'test-container']
    assert calls[1][2] == 'test-container:/tmp/serverkit-runtime.json'
    assert json.loads((tmp_path / 'ubuntu24.runtime.json').read_text())['status'] == 'measured'


def test_missing_artifact_is_explicit_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, 'run_checked', lambda *args, **kwargs: {'success': False})
    with (tmp_path / 'rocky9.log').open('w') as log:
        result = sandbox.TestSandboxService._measure_runtime('test-container', log)
    assert result.startswith('unavailable/failed')
    assert json.loads((tmp_path / 'rocky9.runtime.json').read_text())['status'] == 'failed'


def test_successful_copy_of_unavailable_report_is_not_a_measurement(tmp_path, monkeypatch):
    def run(cmd, **kwargs):
        if cmd[:2] == ['docker', 'cp']:
            Path(cmd[-1]).write_text(json.dumps({'status': 'unavailable'}))
        return {'success': True}

    monkeypatch.setattr(sandbox, 'run_checked', run)
    with (tmp_path / 'legacy.log').open('w') as log:
        result = sandbox.TestSandboxService._measure_runtime('test-container', log)
    assert result.startswith('unavailable/failed')
