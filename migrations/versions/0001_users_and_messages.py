"""Create user resources and message history without foreign keys."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("users",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, unique=True),
    )
    op.create_table("projects",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
    )
    op.create_index("ix_projects_user_id", "projects", ["user_id"])
    op.create_table("sessions",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("next_msg_seq", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("next_msg_seq > 0", name="ck_sessions_next_msg_seq"),
    )
    op.create_index("ix_sessions_user_id_project_id", "sessions", ["user_id", "project_id"])
    op.create_index("ix_sessions_project_id", "sessions", ["project_id"])
    for table in ("messages", "session_messages"):
        op.create_table(table,
            sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("role", sa.Text(), nullable=False),
            sa.Column("tool_call_id", sa.Text(), nullable=True),
            sa.Column("tool_call", postgresql.JSONB(none_as_null=True), nullable=True),
            sa.UniqueConstraint("session_id", "seq", name=f"uq_{table}_session_seq"),
            sa.CheckConstraint("seq > 0", name=f"ck_{table}_seq"),
            sa.CheckConstraint("role IN ('system', 'user', 'assistant', 'tool')", name=f"ck_{table}_role"),
        )


def downgrade():
    for table in ("session_messages", "messages", "sessions", "projects", "users"):
        op.drop_table(table)
