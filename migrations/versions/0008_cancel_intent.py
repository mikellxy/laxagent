"""Persist cancellation intent for the current run attempt."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("runs", "cancel_requested_at")
