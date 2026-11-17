"""Evaluation-only database operations, using the shared application's database."""

from contextlib import asynccontextmanager
from uuid import uuid4

from sqlalchemy import select

from laxagent.storage.database import DatabaseError
from laxagent.storage.models import Message, Project, Run, Session, User


class EvalStore:
    def __init__(self, db):
        self.db = db

    async def create_session(self, name: str, *, container_id: str | None = None, container_workspace: str | None = None) -> Session:
        # All three records commit together; a failed request leaves no orphan user.
        user_id = str(uuid4())
        async with self.db.transaction() as tx:
            tx.add(User(user_id=user_id))
            project = Project(user_id=user_id, name=name, container_id=container_id, container_workspace=container_workspace)
            tx.add(project)
            await tx.flush()
            session = Session(user_id=user_id, project_id=project.id, name=name)
            tx.add(session)
            await tx.flush()
        return session

    @staticmethod
    def session_query(session_id: int):
        return select(Session).join(Project, Project.id == Session.project_id).join(
            User, User.user_id == Session.user_id,
        ).where(Session.id == session_id, Session.deleted_at.is_(None),
                Project.user_id == Session.user_id)

    async def get_session(self, session_id: int) -> Session:
        async with self.db.transaction() as tx:
            session = await tx.scalar(self.session_query(session_id))
            if session is None:
                raise DatabaseError("Session not found", 404)
            return session

    @asynccontextmanager
    async def initialization(self, session_id: int):
        async with self.db.transaction() as tx:
            # Lock the project first: create_session uses it too. Then lock all
            # its sessions, since chat admission locks sessions and workspace is shared.
            project_id = await tx.scalar(self.session_query(session_id).with_only_columns(Session.project_id))
            if project_id is None:
                raise DatabaseError("Session not found", 404)
            await tx.scalar(select(Project).where(Project.id == project_id).with_for_update())
            sessions = list(await tx.scalars(select(Session).where(
                Session.project_id == project_id,
            ).order_by(Session.id).with_for_update()))
            session = next((row for row in sessions if row.id == session_id and row.deleted_at is None), None)
            if session is None:
                raise DatabaseError("Session not found", 404)
            if await tx.scalar(select(Run.id).where(Run.project_id == project_id).limit(1)) is not None:
                raise DatabaseError("Initialization requires a project with no runs", 409)
            yield session

    @asynccontextmanager
    async def snapshot(self, session_id: int, run_id: int):
        # create_run/resume/delete take the same session lock. Keep it until the
        # archive is built so another turn cannot start halfway through collection.
        async with self.db.transaction() as tx:
            session = await tx.scalar(self.session_query(session_id).with_for_update(of=Session))
            if session is None:
                raise DatabaseError("Session not found", 404)
            run = await tx.scalar(select(Run).where(
                Run.id == run_id, Run.session_id == session.id,
                Run.user_id == session.user_id, Run.project_id == session.project_id,
            ))
            if run is None:
                raise DatabaseError("Run not found", 404)
            latest_id = await tx.scalar(select(Run.id).where(
                Run.session_id == session.id,
            ).order_by(Run.id.desc()).limit(1))
            if latest_id != run_id:
                raise DatabaseError("Collection requires the latest run", 409)
            if run.status in ("waiting", "running", "paused"):
                raise DatabaseError("Run has not finished", 409)
            rows = (await tx.execute(select(Message.__table__).where(
                Message.session_id == session.id,
            ).order_by(Message.seq))).mappings()
            yield session, [dict(row) for row in rows]
