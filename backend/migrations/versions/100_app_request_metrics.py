"""Per-app request rollups from the timed nginx access log (plan 86 §A2)."""
from alembic import op
import sqlalchemy as sa

revision = '100_app_request_metrics'
down_revision = '099_ai_management'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if 'app_request_metrics' in inspector.get_table_names():
        return
    op.create_table(
        'app_request_metrics',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('app_id', sa.Integer,
                  sa.ForeignKey('applications.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('level', sa.String(8), nullable=False),
        sa.Column('bucket', sa.DateTime, nullable=False),
        sa.Column('requests', sa.Integer, nullable=False, server_default='0'),
        sa.Column('status_2xx', sa.Integer, nullable=False, server_default='0'),
        sa.Column('status_3xx', sa.Integer, nullable=False, server_default='0'),
        sa.Column('status_4xx', sa.Integer, nullable=False, server_default='0'),
        sa.Column('status_5xx', sa.Integer, nullable=False, server_default='0'),
        sa.Column('bytes_sent', sa.BigInteger, nullable=False, server_default='0'),
        sa.Column('timed_requests', sa.Integer, nullable=False, server_default='0'),
        sa.Column('latency_sum_ms', sa.Float, nullable=False, server_default='0'),
        sa.Column('latency_hist_json', sa.Text),
        sa.Column('cache_hit', sa.Integer, nullable=False, server_default='0'),
        sa.Column('cache_miss', sa.Integer, nullable=False, server_default='0'),
        sa.Column('cache_bypass', sa.Integer, nullable=False, server_default='0'),
        sa.Column('cache_stale', sa.Integer, nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime),
        sa.UniqueConstraint('app_id', 'level', 'bucket',
                            name='uq_app_request_metrics_app_level_bucket'),
    )
    op.create_index('ix_app_request_metrics_app_id', 'app_request_metrics', ['app_id'])
    op.create_index('ix_app_request_metrics_level_bucket', 'app_request_metrics',
                    ['level', 'bucket'])


def downgrade():
    op.drop_index('ix_app_request_metrics_level_bucket', table_name='app_request_metrics')
    op.drop_index('ix_app_request_metrics_app_id', table_name='app_request_metrics')
    op.drop_table('app_request_metrics')
