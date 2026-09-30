"""Drop git_webhooks.zero_downtime (plan 87 §C).

The flag switched webhook deploys to a `--scale web=2` rolling restart that
assumed a service called `web`, ignored its own health result and always
reported success. No UI ever set it. Slot deploys replace it.
"""
import sqlalchemy as sa
from alembic import op

revision = '108_drop_webhook_zero_downtime'
down_revision = '107_app_slots'
branch_labels = None
depends_on = None


def _columns():
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns('git_webhooks')}


def upgrade():
    if 'zero_downtime' in _columns():
        with op.batch_alter_table('git_webhooks') as batch:
            batch.drop_column('zero_downtime')


def downgrade():
    if 'zero_downtime' not in _columns():
        op.add_column('git_webhooks', sa.Column('zero_downtime', sa.Boolean, nullable=True))
