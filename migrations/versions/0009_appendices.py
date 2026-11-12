"""Persist uploaded attachments and immutable message references."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "appendices",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("thumbnail_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_appendices_user_id", "appendices", ["user_id"])
    for table in ("runs", "messages", "session_messages"):
        op.add_column(table, sa.Column("appendix", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade():
    for table in ("runs", "messages", "session_messages"):
        op.drop_column(table, "appendix")
    op.drop_table("appendices")
