"""add sites table and site_id to models

Revision ID: b1c2d3e4f5a6
Revises: e1f2a3b4c5d6
Create Date: 2026-11-19 11:45:00.000000
"""
from datetime import datetime
from alembic import op
import sqlalchemy as sa


revision = 'b1c2d3e4f5a6'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    # 1. Create sites table if not exists
    if 'sites' not in tables:
        op.create_table(
            'sites',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('code', sa.String(length=50), nullable=False),
            sa.Column('name', sa.String(length=150), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        )
        op.create_index('ix_sites_code', 'sites', ['code'], unique=True)

    # 2. Add site_id to target tables if missing
    target_tables = [
        'employees',
        'gh_guest_houses',
        'gh_rooms',
        'gh_vehicles',
        'gh_locations',
        'gh_food_locations',
        'gh_requests',
    ]

    for tbl in target_tables:
        if tbl in tables:
            cols = [c['name'] for c in inspector.get_columns(tbl)]
            if 'site_id' not in cols:
                op.add_column(tbl, sa.Column('site_id', sa.Integer(), nullable=True))
                try:
                    op.create_index(f'ix_{tbl}_site_id', tbl, ['site_id'])
                except Exception:
                    pass

    # 3. Seed default sites
    sites_table = sa.table(
        'sites',
        sa.column('id', sa.Integer),
        sa.column('code', sa.String),
        sa.column('name', sa.String),
        sa.column('is_active', sa.Boolean),
        sa.column('created_at', sa.DateTime),
    )

    now = datetime.now()
    existing_codes = set()
    result = conn.execute(sa.text("SELECT id, code FROM sites"))
    jaigad_id = None
    for row in result:
        existing_codes.add(str(row[1]))
        if str(row[1]) == '5110':
            jaigad_id = row[0]

    if '5110' not in existing_codes:
        conn.execute(
            sites_table.insert().values(code='5110', name='JSW Jaigarh Port', is_active=True, created_at=now)
        )
        row = conn.execute(sa.text("SELECT id FROM sites WHERE code='5110'")).fetchone()
        if row:
            jaigad_id = row[0]

    if '5130' not in existing_codes:
        conn.execute(
            sites_table.insert().values(code='5130', name='JSW Dharamtar', is_active=True, created_at=now)
        )

    # 4. Associate existing data to Jaigad (5110)
    if jaigad_id:
        for tbl in ['gh_guest_houses', 'gh_rooms', 'gh_vehicles', 'gh_locations', 'gh_food_locations', 'gh_requests']:
            if tbl in tables:
                conn.execute(sa.text(f"UPDATE {tbl} SET site_id = :sid WHERE site_id IS NULL"), {"sid": jaigad_id})


def downgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    target_tables = [
        'employees',
        'gh_guest_houses',
        'gh_rooms',
        'gh_vehicles',
        'gh_locations',
        'gh_food_locations',
        'gh_requests',
    ]
    for tbl in target_tables:
        if tbl in tables:
            cols = [c['name'] for c in inspector.get_columns(tbl)]
            if 'site_id' in cols:
                op.drop_column(tbl, 'site_id')

    if 'sites' in tables:
        op.drop_table('sites')
