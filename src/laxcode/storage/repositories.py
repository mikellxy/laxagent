from sqlalchemy import select

from laxcode.storage.database import Database, DatabaseError
from laxcode.storage.models import Message, Project, Session, SessionMessage, User


class Repository:
    def __init__(self, db: Database):
        self.db = db

    async def create_user(self, user_id: str) -> User:
        async with self.db.transaction() as transaction:
            user = User(user_id=user_id)
            transaction.add(user)
            await transaction.flush()
        return user

    async def create_project(self, user_id: str, name: str) -> Project:
        async with self.db.transaction() as transaction:
            if await transaction.scalar(select(User.id).where(User.user_id == user_id)) is None:
                raise DatabaseError("User not found", 404)
            project = Project(user_id=user_id, name=name)
            transaction.add(project)
            await transaction.flush()
        return project

    async def create_session(self, user_id: str, project_id: int, name: str) -> Session:
        async with self.db.transaction() as transaction:
            project = await transaction.scalar(select(Project).join(
                User, User.user_id == Project.user_id,
            ).where(Project.id == project_id, Project.user_id == user_id))
            if project is None:
                raise DatabaseError("Project not found", 404)
            session = Session(user_id=user_id, project_id=project_id, name=name)
            transaction.add(session)
            await transaction.flush()
        return session

    async def append_message(
        self, *, user_id: str, project_id: int, session_id: int,
        role: str, content: str, expected_seq: int | None = None, load_context=False,
    ) -> tuple[int, list[dict]]:
        async with self.db.transaction() as transaction:
            session = await transaction.scalar(select(Session).where(
                Session.id == session_id, Session.user_id == user_id,
                Session.project_id == project_id,
            ).with_for_update())
            if session is None:
                raise DatabaseError("Session not found", 404)
            owner = await transaction.scalar(select(Project.id).join(
                User, User.user_id == Project.user_id,
            ).where(Project.id == project_id, Project.user_id == user_id))
            if owner is None:
                raise DatabaseError("Project not found", 404)
            seq = session.next_msg_seq
            if expected_seq is not None and expected_seq != seq:
                raise DatabaseError("Session message sequence changed", 409)
            values = dict(seq=seq, session_id=session_id, role=role, content=content,
                          tool_call_id=None, tool_call=None)
            transaction.add_all([Message(**values), SessionMessage(**values)])
            session.next_msg_seq = seq + 1
            await transaction.flush()
            messages = []
            if load_context:
                rows = (await transaction.execute(select(
                    SessionMessage.role, SessionMessage.content,
                ).where(SessionMessage.session_id == session_id).order_by(SessionMessage.seq))).mappings()
                messages = [dict(row) for row in rows]
        return seq + 1, messages
