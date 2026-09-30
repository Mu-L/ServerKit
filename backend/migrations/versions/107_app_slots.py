"""A/B slot deploys for apps (plan 87 §B).

``app_slots`` holds each app's two slots; ``applications.active_slot`` names
the live one and ``slot_deploys_enabled`` opts the app in. Nothing changes for
an app until it opts in: its current container is then adopted as slot ``a``
in place, with no restart.
"""
from alembic import op
import sqlalchemy as sa

revision = '107_app_slots'
down_revision = '106_app_deploy_settings'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {c['name'] for c in inspector.get_columns('applications')}
    # Add-if-missing: the pre-alembic schema sync may already have added them.
    if 'active_slot' not in columns:
        op.add_column('applications', sa.Column('active_slot', sa.String(1), nullable=True))
    if 'slot_deploys_enabled' not in columns:
        op.add_column('applications', sa.Column('slot_deploys_enabled', sa.Boolean,
                                                nullable=False, server_default=sa.false()))
    # The schema sync may have added the column before this guard ran; make
    # sure no existing row is left NULL either way.
    op.execute(sa.text('UPDATE applications SET slot_deploys_enabled = :off '
                       'WHERE slot_deploys_enabled IS NULL').bindparams(off=False))
    if 'app_slots' in inspector.get_table_names():
        return
    op.create_table(
        'app_slots',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('application_id', sa.Integer,
                  sa.ForeignKey('applications.id', ondelete='CASCADE'), nullable=False),
        sa.Column('slot', sa.String(1), nullable=False),
        sa.Column('host_port', sa.Integer),
        sa.Column('container_port', sa.Integer),
        sa.Column('container_name', sa.String(128)),
        sa.Column('project_name', sa.String(128)),
        sa.Column('image_ref', sa.String(255)),
        sa.Column('commit_sha', sa.String(40)),
        sa.Column('deployment_id', sa.Integer,
                  sa.ForeignKey('deployments.id', ondelete='SET NULL')),
        sa.Column('config_snapshot_id', sa.Integer),
        sa.Column('state', sa.String(16), nullable=False, server_default='empty'),
        sa.Column('started_at', sa.DateTime),
        sa.Column('standby_until', sa.DateTime),
        sa.Column('last_health', sa.String(255)),
        sa.Column('updated_at', sa.DateTime),
        sa.UniqueConstraint('application_id', 'slot', name='uq_app_slots_app_slot'),
    )
    op.create_index('ix_app_slots_application_id', 'app_slots', ['application_id'])
    op.create_index('ix_app_slots_deployment_id', 'app_slots', ['deployment_id'])


def downgrade():
    op.drop_index('ix_app_slots_deployment_id', table_name='app_slots')
    op.drop_index('ix_app_slots_application_id', table_name='app_slots')
    op.drop_table('app_slots')
    op.drop_column('applications', 'slot_deploys_enabled')
    op.drop_column('applications', 'active_slot')
