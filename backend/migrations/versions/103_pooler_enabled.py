"""Opt-in PgBouncer per PostgreSQL app (plan 86 §D1). NULL = off."""
from alembic import op
import sqlalchemy as sa

revision = '103_pooler_enabled'
down_revision = '102_app_attachments'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # Add-if-missing: the pre-alembic schema sync may already have added it.
    if 'pooler_enabled' not in {c['name'] for c in inspector.get_columns('applications')}:
        op.add_column('applications', sa.Column('pooler_enabled', sa.Boolean, nullable=True))


def downgrade():
    op.drop_column('applications', 'pooler_enabled')
