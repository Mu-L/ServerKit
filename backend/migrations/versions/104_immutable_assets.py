"""Opt-in long caching of fingerprinted assets per app (plan 86 §B2). NULL = off."""
from alembic import op
import sqlalchemy as sa

revision = '104_immutable_assets'
down_revision = '103_pooler_enabled'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # Add-if-missing: the pre-alembic schema sync may already have added it.
    if 'immutable_assets' not in {c['name'] for c in inspector.get_columns('applications')}:
        op.add_column('applications', sa.Column('immutable_assets', sa.Boolean, nullable=True))


def downgrade():
    op.drop_column('applications', 'immutable_assets')
