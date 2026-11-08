"""Persist asynchronous agent runs."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'waiting'"), nullable=False),
        sa.Column("user_query", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("context_limit", sa.Integer(), nullable=False),
        sa.Column("output_limit", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.String(36), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint("status IN ('waiting', 'running', 'success', 'failed')", name="ck_runs_status"),
        sa.CheckConstraint("context_limit > 0 AND output_limit > 0 AND output_limit <= context_limit",
                           name="ck_runs_limits"),
    )
    op.create_index("ix_runs_session_id_id", "runs", ["session_id", "id"])
    op.create_index("uq_runs_active_session", "runs", ["session_id"], unique=True,
                    postgresql_where=sa.text("status IN ('waiting', 'running')"))


def downgrade():
    op.drop_table("runs")
