"""Database insights against REAL engines (plan 86 §A3).

PostgreSQL 16: insights say pg_stat_statements is off, the tuner enables it
(preload + restart + CREATE EXTENSION), and a query that ran afterwards shows
up in the top list. MySQL 8: performance_schema collects digests by default,
so the top list and the InnoDB hit ratio come back straight away.

    SERVERKIT_DOCKER_BUILDS=1 pytest tests -m docker_builds
"""
import os
import shutil
import subprocess
import time
import uuid

import pytest

from app.services.db_config_tuner_service import DbConfigTunerService
from app.services.db_insights_service import DbInsightsService


def _docker_ready() -> bool:
    if shutil.which('docker') is None:
        return False
    try:
        return subprocess.run(['docker', 'info'], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.docker_builds,
    pytest.mark.skipif(os.environ.get('SERVERKIT_DOCKER_BUILDS') != '1',
                       reason='docker-builds leg; opt in with SERVERKIT_DOCKER_BUILDS=1'),
    pytest.mark.skipif(not _docker_ready(), reason='docker daemon not reachable'),
]

PASSWORD = 'insights-pw'


def _start(name, image, env):
    argv = ['docker', 'run', '-d', '--name', name]
    for key, value in env.items():
        argv += ['-e', f'{key}={value}']
    subprocess.run(argv + [image], check=True, capture_output=True, timeout=300)


def _wait(fn, what, timeout=120):
    deadline = time.time() + timeout
    while True:
        result = fn()
        if result:
            return result
        assert time.time() < deadline, f'{what} never happened'
        time.sleep(2)


def test_postgres_top_queries_after_enabling_pg_stat_statements(app, monkeypatch, tmp_path):
    # The tuner is Linux-gated for the panel; the docker daemon is what it
    # actually needs, and this leg has one.
    monkeypatch.setattr(DbConfigTunerService, '_linux_supported', classmethod(lambda cls: True))
    monkeypatch.setattr(DbConfigTunerService, 'STATE_DIR', str(tmp_path))
    name = f'skc-pg-{uuid.uuid4().hex[:8]}'
    target = {'engine': 'postgresql', 'container': name, 'user': 'postgres',
              'password': PASSWORD, 'database': 'postgres'}
    try:
        _start(name, 'postgres:16-alpine', {'POSTGRES_PASSWORD': PASSWORD})
        before = _wait(lambda: (lambda r: r if 'error' not in r else None)(
            DbInsightsService.insights(target)), 'postgres ready')
        assert before['top_queries_available'] is False
        assert before['can_enable'] is True
        assert before['connections']['max'] == 100
        assert 0 <= (before['cache_hit_ratio'] or 0) <= 1

        DbConfigTunerService.enable_pg_stat_statements(target)
        DbInsightsService._exec_sql(target, 'SELECT count(*) FROM pg_class;')

        after = DbInsightsService.insights(target)
        assert after['top_queries_available'] is True, after
        assert any('pg_class' in q['query'] for q in after['top_queries']), after['top_queries']
        # Enabling twice is a no-op, not a second restart.
        again = DbConfigTunerService.enable_pg_stat_statements(target)
        assert again['restarted'] is False

        # The tuner's rollback undoes it (the plan's "rollback comes for free").
        DbConfigTunerService.rollback(target)
        preload = DbInsightsService._exec_sql(target, 'SHOW shared_preload_libraries;')
        assert 'pg_stat_statements' not in preload['output']
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=60)


def test_mysql_top_queries_and_buffer_pool_ratio(app):
    name = f'skc-my-{uuid.uuid4().hex[:8]}'
    target = {'engine': 'mysql', 'container': name, 'user': 'root', 'password': PASSWORD}
    try:
        _start(name, 'mysql:8.4', {'MYSQL_ROOT_PASSWORD': PASSWORD})
        _wait(lambda: DbInsightsService._exec_sql(target, 'SELECT 1;')['success'],
              'mysql ready', timeout=180)
        DbInsightsService._exec_sql(target, 'SELECT count(*) FROM information_schema.tables;')

        result = DbInsightsService.insights(target)
        assert 'error' not in result, result
        assert result['top_queries_available'] is True, result
        assert any('information_schema' in q['query'].lower() for q in result['top_queries'])
        assert result['connections']['in_use'] >= 1
        assert result['connections']['max'] >= 100
        assert result['cache_hit_ratio'] is not None
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=60)
