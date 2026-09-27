"""Plan 85 A5 / §B1 / §C: log files are bounded, the previous install goes
once the update has proven itself, and small disks get small defaults."""
import json
import os
import time

import pytest

from app.services import log_limits_service as logs
from app.services import slot_retention_service as slots
from app.services import storage_profile_service as profile
from app.services.settings_service import SettingsService

GB = 1024 ** 3


# ── A5: log files ──────────────────────────────────────────────────────────

def test_logrotate_file_covers_the_panel_logs_and_is_idempotent(tmp_path):
    target = tmp_path / 'logrotate.d' / 'serverkit'
    target.parent.mkdir()
    assert logs.ensure_logrotate('/var/log/serverkit', path=str(target)) is True
    body = target.read_text()
    for name in logs.ROTATED_LOGS:
        assert f'/var/log/serverkit/{name}' in body
    assert 'copytruncate' in body and 'maxsize 50M' in body
    assert logs.ensure_logrotate('/var/log/serverkit', path=str(target)) is False


def test_no_logrotate_dir_means_nothing_is_written(tmp_path):
    assert logs.ensure_logrotate('/x', path=str(tmp_path / 'missing' / 'serverkit')) is False


def test_docker_log_cap_is_merged_into_an_existing_daemon_json(tmp_path):
    daemon = tmp_path / 'daemon.json'
    daemon.write_text(json.dumps({'data-root': '/srv/docker', 'log-opts': {'labels': 'a'}}))
    assert logs.ensure_docker_log_opts('10m', path=str(daemon)) is True
    config = json.loads(daemon.read_text())
    assert config['data-root'] == '/srv/docker'
    assert config['log-opts'] == {'labels': 'a', 'max-size': '10m', 'max-file': '3'}


@pytest.mark.parametrize('existing', [
    '{not json',                                        # never rewrite what we cannot read
    json.dumps({'log-driver': 'journald'}),             # another driver has its own limits
    json.dumps({'log-opts': {'max-size': '200m'}}),     # the operator already chose
])
def test_docker_daemon_json_left_alone(tmp_path, existing):
    daemon = tmp_path / 'daemon.json'
    daemon.write_text(existing)
    assert logs.ensure_docker_log_opts('10m', path=str(daemon)) is False
    assert daemon.read_text() == existing


def test_no_docker_dir_means_no_daemon_json(tmp_path):
    assert logs.ensure_docker_log_opts('10m', path=str(tmp_path / 'docker' / 'daemon.json')) is False


def _files(directory, names):
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for age, name in enumerate(names):
        path = directory / name
        path.write_text('x')
        os.utime(path, (now - age * 60, now - age * 60))   # first name = newest


def test_only_the_newest_update_logs_are_kept(tmp_path):
    names = [f'update-2026090{i}-000000.log' for i in range(9)] + \
            [f'update-2026080{i}-000000.log' for i in range(5)]
    _files(tmp_path, names + ['access.log'])
    assert logs.prune_update_logs(str(tmp_path), keep=10) == 4
    left = sorted(p.name for p in tmp_path.iterdir())
    assert 'access.log' in left and len(left) == 11
    assert set(names[:10]) <= set(left)


def test_build_logs_keep_the_newest_per_app(tmp_path):
    _files(tmp_path / '1', [f'build-{i}.json' for i in range(12)])
    _files(tmp_path / '2', ['build-0.json'])
    assert logs.prune_build_logs(str(tmp_path), keep=10) == 2
    assert len(list((tmp_path / '1').iterdir())) == 10
    assert len(list((tmp_path / '2').iterdir())) == 1


# ── §C: size-aware defaults ────────────────────────────────────────────────

def test_profile_follows_disk_size():
    assert profile.profile_name(25 * GB) == 'small'
    assert profile.profile_name(200 * GB) == 'standard'
    assert profile.profile_name(None) in ('small', 'standard')


def test_small_disk_moves_untouched_defaults_once(app):
    SettingsService.set('telemetry.retention_days', 30)     # the old default
    SettingsService.set('jobs.retention_days', 21)          # the operator's choice
    changed = profile.apply_to_existing(total_bytes=25 * GB)

    assert 'telemetry.retention_days' in changed
    assert int(SettingsService.get('telemetry.retention_days')) == 7
    assert int(SettingsService.get('jobs.retention_days')) == 21
    # Once only: a later change back to 30 is the operator's and stays.
    SettingsService.set('telemetry.retention_days', 30)
    assert profile.apply_to_existing(total_bytes=25 * GB) == []
    assert int(SettingsService.get('telemetry.retention_days')) == 30


def test_a_large_disk_keeps_the_standard_values(app):
    SettingsService.set('telemetry.retention_days', 30)
    assert profile.apply_to_existing(total_bytes=500 * GB) == []
    assert int(SettingsService.get('telemetry.retention_days')) == 30


# ── §B1: previous slot ─────────────────────────────────────────────────────

posix_only = pytest.mark.skipif(os.name == 'nt', reason='symlinks + flock (runs on Linux CI)')


def _bluegreen(tmp_path, switched_hours_ago):
    install = tmp_path / 'serverkit'
    for slot in ('serverkit-a', 'serverkit-b'):
        (tmp_path / slot / 'backend').mkdir(parents=True)
        (tmp_path / slot / 'backend' / 'app.py').write_text('x' * 100)
    os.symlink(tmp_path / 'serverkit-b', install)
    when = time.time() - switched_hours_ago * 3600
    os.utime(install, (when, when), follow_symlinks=False)
    return str(install)


@posix_only
def test_the_previous_slot_goes_a_day_after_the_switch(tmp_path):
    install = _bluegreen(tmp_path, switched_hours_ago=30)
    result = slots.remove_previous_slot(hours=24, install_dir=install,
                                        lock_path=str(tmp_path / 'no.lock'))
    assert result and result['removed'] == str(tmp_path / 'serverkit-a')
    assert not (tmp_path / 'serverkit-a').exists()
    assert (tmp_path / 'serverkit-b' / 'backend' / 'app.py').exists()


@posix_only
def test_a_fresh_update_keeps_its_rollback(tmp_path):
    install = _bluegreen(tmp_path, switched_hours_ago=2)
    assert slots.remove_previous_slot(hours=24, install_dir=install,
                                      lock_path=str(tmp_path / 'no.lock')) is None
    assert (tmp_path / 'serverkit-a').exists()


@posix_only
def test_nothing_is_removed_while_an_update_holds_the_lock(tmp_path):
    import fcntl
    install = _bluegreen(tmp_path, switched_hours_ago=30)
    lock = tmp_path / 'update.lock'
    lock.write_text('')
    with open(lock) as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert slots.remove_previous_slot(hours=24, install_dir=install,
                                          lock_path=str(lock)) is None
    assert (tmp_path / 'serverkit-a').exists()


@posix_only
def test_zero_hours_keeps_the_slot(tmp_path):
    install = _bluegreen(tmp_path, switched_hours_ago=300)
    assert slots.remove_previous_slot(hours=0, install_dir=install) is None
    assert (tmp_path / 'serverkit-a').exists()


def test_a_plain_directory_install_has_no_previous_slot(tmp_path):
    (tmp_path / 'serverkit').mkdir()
    (tmp_path / 'serverkit-a').mkdir()
    assert slots.previous_slot(str(tmp_path / 'serverkit')) is None
