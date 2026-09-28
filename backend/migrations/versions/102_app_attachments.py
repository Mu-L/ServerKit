"""App attachments to services they use (plan 86 §C2)."""
from alembic import op
import sqlalchemy as sa

revision = '102_app_attachments'
down_revision = '101_micro_cache_ttl'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if 'app_attachments' in inspector.get_table_names():
        return
    op.create_table(
        'app_attachments',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('app_id', sa.Integer,
                  sa.ForeignKey('applications.id', ondelete='CASCADE'), nullable=False),
        sa.Column('service_app_id', sa.Integer,
                  sa.ForeignKey('applications.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('details_json', sa.Text),
        sa.Column('created_at', sa.DateTime),
        sa.UniqueConstraint('app_id', 'kind', name='uq_app_attachments_app_kind'),
    )
    op.create_index('ix_app_attachments_app_id', 'app_attachments', ['app_id'])
    op.create_index('ix_app_attachments_service_app_id', 'app_attachments', ['service_app_id'])


def downgrade():
    op.drop_index('ix_app_attachments_service_app_id', table_name='app_attachments')
    op.drop_index('ix_app_attachments_app_id', table_name='app_attachments')
    op.drop_table('app_attachments')
