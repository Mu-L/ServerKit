"""Per-site micro-cache TTL (plan 86 §B3). NULL keeps the 10s default."""
from alembic import op
import sqlalchemy as sa

revision = '101_micro_cache_ttl'
down_revision = '100_app_request_metrics'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # Add-if-missing: the pre-alembic schema sync may already have added it.
    if 'micro_cache_ttl' not in {c['name'] for c in inspector.get_columns('applications')}:
        op.add_column('applications', sa.Column('micro_cache_ttl', sa.Integer, nullable=True))


def downgrade():
    op.drop_column('applications', 'micro_cache_ttl')
