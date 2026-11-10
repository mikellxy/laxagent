"""Track execution attempts and stable failure codes on the same run."""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("resume_time", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("runs", sa.Column("error_code", sa.Text(), nullable=True))
    op.create_check_constraint("ck_runs_resume_time", "runs", "resume_time >= 0")


def downgrade():
    op.drop_constraint("ck_runs_resume_time", "runs", type_="check")
    op.drop_column("runs", "error_code")
    op.drop_column("runs", "resume_time")
