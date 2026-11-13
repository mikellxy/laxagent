"""Snapshot the effective attachment question model for each run."""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("appendix_model", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("runs", "appendix_model")
