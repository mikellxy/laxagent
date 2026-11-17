"""Bind Docker evaluation projects to a prepared container workspace."""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("container_id", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("container_workspace", sa.Text(), nullable=True))
    op.create_check_constraint("ck_projects_container_binding", "projects",
                               "(container_id IS NULL) = (container_workspace IS NULL)")


def downgrade():
    op.drop_constraint("ck_projects_container_binding", "projects", type_="check")
    op.drop_column("projects", "container_workspace")
    op.drop_column("projects", "container_id")
