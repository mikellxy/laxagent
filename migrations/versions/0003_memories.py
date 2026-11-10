"""Persist user-scoped derived memories."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "category IN ('profile', 'preference', 'instruction', 'knowledge', 'project', 'workflow')",
            name="ck_memories_category",
        ),
        sa.CheckConstraint("abstract ~ '[^[:space:]]'", name="ck_memories_abstract_not_blank"),
        sa.CheckConstraint("detail ~ '[^[:space:]]'", name="ck_memories_detail_not_blank"),
    )
    op.create_index("ix_memories_user_id_category_id", "memories", ["user_id", "category", "id"])


def downgrade():
    op.drop_table("memories")
