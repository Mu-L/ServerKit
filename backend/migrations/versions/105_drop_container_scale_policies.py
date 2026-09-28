"""Drop container scaling policies (plan 86 §E2).

Auto-scaling adjusted a compose service's replica count, but nginx only ever
proxied to one port and the sweep was never scheduled, so it could not work;
the feature was removed in favour of one live copy per app. Running
containers are not touched: a service scaled up earlier keeps its replicas
until its next deploy.
"""
import sqlalchemy as sa
from alembic import op

revision = '105_drop_container_scale_policies'
down_revision = '104_immutable_assets'
branch_labels = None
depends_on = None


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    if _has_table('container_scale_policies'):
        op.drop_table('container_scale_policies')


def downgrade():
    if _has_table('container_scale_policies'):
        return
    op.create_table(
        'container_scale_policies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('application_id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('service_name', sa.String(length=100), nullable=True),
        sa.Column('min_replicas', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('max_replicas', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('cpu_high_percent', sa.Integer(), nullable=False, server_default='75'),
        sa.Column('cpu_low_percent', sa.Integer(), nullable=False, server_default='25'),
        sa.Column('cooldown_seconds', sa.Integer(), nullable=False, server_default='300'),
        sa.Column('current_replicas', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('last_scaled_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['application_id'], ['applications.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_container_scale_policies_application_id',
                    'container_scale_policies', ['application_id'], unique=True)
