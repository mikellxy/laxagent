"""Soft-delete sessions and distinguish user cancellation."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint("ck_runs_status", "runs",
                               "status IN ('waiting', 'running', 'success', 'failed', 'cancelled')")


def downgrade():
    op.execute("UPDATE runs SET status = 'failed', error = 'Execution cancelled' WHERE status = 'cancelled'")
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint("ck_runs_status", "runs",
                               "status IN ('waiting', 'running', 'success', 'failed')")
    op.drop_column("sessions", "deleted_at")
