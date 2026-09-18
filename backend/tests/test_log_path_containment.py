"""LogService.is_path_allowed must compare whole path segments.

The guard resolved the requested path with realpath and then asked whether the
result merely *started with* one of ALLOWED_LOG_DIRECTORIES. That is a text
test, not a containment test: '/opt-private' starts with the allowed root
'/opt' without sitting inside it, so a sibling directory that only shares a
name prefix was accepted as an allowed log location.

FileService guards the same roots and already gets this right at
file_service.py:96-100, comparing `real_path == root or
real_path.startswith(root + os.sep)`. These tests hold LogService to the same
contract.

The sandbox cases monkeypatch the root list onto a tmp_path so they run
anywhere. The shipped-constant case uses /opt rather than /var/log, because
realpath('/var/log') is '/private/var/log' on a macOS dev box, which would
assert the developer's filesystem instead of the guard.
"""
import os

import pytest

from app.services.file_service import FileService
from app.services.log_service import LogService


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """One allowed root, plus a sibling whose name shares its prefix.

    tmp_path is resolved first so the fixture compares like with like: the
    guard realpaths its argument before matching, and /var is a symlink to
    /private/var on macOS.
    """
    base = os.path.realpath(str(tmp_path))
    allowed = os.path.join(base, 'logroot')
    sibling = os.path.join(base, 'logroot-backup')
    os.makedirs(allowed)
    os.makedirs(sibling)
    monkeypatch.setattr(LogService, 'ALLOWED_LOG_DIRECTORIES', [allowed])
    return {'allowed': allowed, 'sibling': sibling}


# --------------------------------------------------------------------------- #
# A name prefix is not containment (the actual bug)
# --------------------------------------------------------------------------- #
def test_prefix_sibling_is_not_inside_the_root(roots):
    assert not LogService.is_path_allowed(roots['sibling'])
    assert not LogService.is_path_allowed(
        os.path.join(roots['sibling'], 'app.log'))


def test_shipped_roots_reject_a_prefix_sibling():
    """The literal constants. /opt is a shipped root and resolves to itself on
    the platforms the panel installs on, so it can be asserted directly."""
    assert '/opt' in LogService.ALLOWED_LOG_DIRECTORIES
    assert not LogService.is_path_allowed('/opt-private')
    assert not LogService.is_path_allowed('/opt-private/creds.txt')


def test_clear_log_refuses_a_prefix_sibling(roots):
    """The guard gates truncation as well as reading, through clear_log.

    The target deliberately does not exist, so the broken guard falls through
    to 'Log file not found' instead of running truncate against a real file.
    """
    result = LogService.clear_log(os.path.join(roots['sibling'], 'app.log'))

    assert result['success'] is False
    assert result['error'] == 'Access denied: path not in allowed directories'


# --------------------------------------------------------------------------- #
# Everything genuinely inside a root still resolves
# --------------------------------------------------------------------------- #
def test_paths_inside_the_root_still_pass(roots):
    """Including one reached through '..', which realpath collapses before the
    comparison happens."""
    assert LogService.is_path_allowed(roots['allowed'])
    assert LogService.is_path_allowed(os.path.join(roots['allowed'], 'app.log'))
    assert LogService.is_path_allowed(
        os.path.join(roots['allowed'], 'nested', '..', 'app.log'))


# --------------------------------------------------------------------------- #
# Panel-internal roots are never allowed, even under an allowed root
# --------------------------------------------------------------------------- #
def test_protected_roots_are_shared_with_file_service():
    """One source of truth: LogService must not grow its own copy of the
    panel-internal list and drift away from FileService's."""
    assert LogService.PROTECTED_ROOTS is FileService.PROTECTED_ROOTS


def test_install_dir_is_rejected_even_when_its_parent_is_allowed(monkeypatch):
    """On the install.sh layout the install dir sits under the allowed root
    /opt, so reachability must come from the exclusion, not from the allowed
    list happening not to contain the parent."""
    parent = os.path.dirname(FileService._INSTALL_DIR)
    monkeypatch.setattr(LogService, 'ALLOWED_LOG_DIRECTORIES', [parent])

    assert not LogService.is_path_allowed(FileService._INSTALL_DIR)
    assert not LogService.is_path_allowed(
        os.path.join(FileService._INSTALL_DIR, '.env'))
    assert not LogService.is_path_allowed(
        os.path.join(FileService._BACKEND_DIR, 'config.py'))


def test_clear_log_refuses_a_panel_secret(monkeypatch):
    """The exclusion also gates truncation, through clear_log.

    The target deliberately does not exist, so if the guard regresses the
    call falls through to 'Log file not found' instead of running truncate
    against a real file.
    """
    parent = os.path.dirname(FileService._INSTALL_DIR)
    monkeypatch.setattr(LogService, 'ALLOWED_LOG_DIRECTORIES', [parent])
    target = os.path.join(
        FileService._INSTALL_DIR, '.env-not-present-for-this-test')

    result = LogService.clear_log(target)

    assert result['success'] is False
    assert result['error'] == 'Access denied: path not in allowed directories'
