"""Keep runs awaiting human answers active in their session."""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint("ck_runs_status", "runs",
                               "status IN ('waiting', 'running', 'paused', 'success', 'failed', 'cancelled')")
    op.drop_index("uq_runs_active_session", table_name="runs")
    op.create_index("uq_runs_active_session", "runs", ["session_id"], unique=True,
                    postgresql_where=sa.text("status IN ('waiting', 'running', 'paused')"))


def downgrade():
    # Refuse to silently discard a pending human interaction on downgrade.
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint("ck_runs_status", "runs",
                               "status IN ('waiting', 'running', 'success', 'failed', 'cancelled')")
    op.drop_index("uq_runs_active_session", table_name="runs")
    op.create_index("uq_runs_active_session", "runs", ["session_id"], unique=True,
                    postgresql_where=sa.text("status IN ('waiting', 'running')"))
