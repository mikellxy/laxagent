"""Persist public assistant reasoning alongside messages."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("messages", "session_messages"):
        op.add_column(table, sa.Column("reasoning", postgresql.JSONB(), nullable=True))


def downgrade():
    for table in ("messages", "session_messages"):
        op.drop_column(table, "reasoning")
