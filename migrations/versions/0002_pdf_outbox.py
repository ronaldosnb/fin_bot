"""Persist PDF attachments until delivery.

Revision ID: 0002_pdf_outbox
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_pdf_outbox"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    # The initial revision creates tables from current metadata on new installs.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("outbox")}
    if "pdf_bytes" not in columns:
        op.add_column("outbox", sa.Column("pdf_bytes", sa.LargeBinary(), nullable=True))
    if "file_name" not in columns:
        op.add_column("outbox", sa.Column("file_name", sa.String(160), nullable=True))


def downgrade():
    raise RuntimeError("Não remova anexos pendentes; restaure um backup para reverter")
