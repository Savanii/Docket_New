"""add employee push key and last push info to sites

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-11-25 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd3e4f5a6b7c8'
down_revision = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sites', sa.Column('sync_key_hash', sa.String(length=64), nullable=True))
    op.add_column('sites', sa.Column('last_push_at', sa.DateTime(), nullable=True))
    op.add_column('sites', sa.Column('last_push_result', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('sites') as batch:
        batch.drop_column('last_push_result')
        batch.drop_column('last_push_at')
        batch.drop_column('sync_key_hash')
