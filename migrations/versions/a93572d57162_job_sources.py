"""job sources (P13 multi-source B-roll)

Revision ID: a93572d57162
Revises: d6a6c4083440
Create Date: 2026-09-23 11:30:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a93572d57162'
down_revision: str | None = 'd6a6c4083440'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'job_sources',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('job_id', sa.Uuid(), nullable=False),
        sa.Column('upload_id', sa.Uuid(), nullable=False),
        sa.Column('role', sa.Enum('PRIMARY', 'BROLL', name='sourcerole', native_enum=False, length=32), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_job_sources_job_id_jobs')),
        sa.ForeignKeyConstraint(['upload_id'], ['uploads.id'], name=op.f('fk_job_sources_upload_id_uploads')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_job_sources')),
        sa.UniqueConstraint('job_id', 'upload_id', name='uq_job_sources_job_id_upload_id'),
    )


def downgrade() -> None:
    op.drop_table('job_sources')
