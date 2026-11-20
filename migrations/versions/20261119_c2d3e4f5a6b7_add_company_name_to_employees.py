"""add company_name to employees

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-11-19 12:35:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c2d3e4f5a6b7'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'employees' in tables:
        cols = [c['name'] for c in inspector.get_columns('employees')]
        if 'company_name' not in cols:
            op.add_column('employees', sa.Column('company_name', sa.String(length=200), nullable=True))
            try:
                op.create_index('ix_employees_company_name', 'employees', ['company_name'])
            except Exception:
                pass


def downgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'employees' in tables:
        cols = [c['name'] for c in inspector.get_columns('employees')]
        if 'company_name' in cols:
            op.drop_column('employees', 'company_name')
