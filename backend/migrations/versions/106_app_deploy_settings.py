"""Per-app deploy settings: health-gate timeout and 4xx rule (plan 87 §A4).

One JSON column rather than a column per knob: the slot engine (plan 87 §B)
adds watch-window and standby settings to the same object. NULL = defaults.
"""
from alembic import op
import sqlalchemy as sa

revision = '106_app_deploy_settings'
down_revision = '105_drop_container_scale_policies'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # Add-if-missing: the pre-alembic schema sync may already have added it.
    if 'deploy_settings' not in {c['name'] for c in inspector.get_columns('applications')}:
        op.add_column('applications', sa.Column('deploy_settings', sa.Text, nullable=True))


def downgrade():
    op.drop_column('applications', 'deploy_settings')
