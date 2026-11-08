from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from laxcode.storage.database import Database, DatabaseError
from laxcode.storage.models import Message, Project, Run, Session, SessionMessage, User


class Repository:
    def __init__(self, db: Database):
        self.db = db

    async def user_exists(self, user_id: str) -> bool:
        async with self.db.transaction() as transaction:
            return await transaction.scalar(select(User.id).where(User.user_id == user_id)) is not None

    async def ensure_user(self, user_id: str, *, guest: bool = False) -> int | None:
        async with self.db.transaction() as transaction:
            await transaction.execute(insert(User).values(user_id=user_id).on_conflict_do_nothing(
                index_elements=[User.user_id],
            ))
            if not guest:
                return None
            await transaction.scalar(select(User.id).where(User.user_id == user_id).with_for_update())
            project = await transaction.scalar(select(Project).where(
                Project.user_id == user_id, Project.name == "LaxCode",
            ).order_by(Project.id).limit(1).with_for_update())
            if project is None:
                project = Project(user_id=user_id, name="LaxCode")
                transaction.add(project)
                await transaction.flush()
            session = await transaction.scalar(select(Session).where(
                Session.user_id == user_id, Session.project_id == project.id, Session.name == "laxcode",
            ).order_by(Session.id).limit(1))
            if session is None:
                session = Session(user_id=user_id, project_id=project.id, name="laxcode")
                transaction.add(session)
                await transaction.flush()
            return session.id

    @staticmethod
    def owned_session(user_id: str, session_id: int):
        return select(Session).join(Project, Project.id == Session.project_id).join(
            User, User.user_id == Session.user_id,
        ).where(Session.id == session_id, Session.user_id == user_id, Project.user_id == user_id)

    async def create_run(self, *, user_id: str, session_id: int, user_query: str,
                         model: str, context_limit: int, output_limit: int) -> Run:
        async with self.db.transaction() as transaction:
            session = await transaction.scalar(self.owned_session(user_id, session_id).with_for_update(of=Session))
            if session is None:
                raise DatabaseError("Session not found", 404)
            run = Run(user_id=user_id, session_id=session.id, project_id=session.project_id,
                      user_query=user_query, model=model, context_limit=context_limit,
                      output_limit=output_limit, thread_id=str(uuid4()))
            transaction.add(run)
            await transaction.flush()
        return run

    async def get_run(self, run_id: int, user_id: str) -> Run:
        async with self.db.transaction() as transaction:
            run = await transaction.get(Run, run_id)
            if run is None or run.user_id != user_id:
                raise DatabaseError("Run not found", 404)
            session = await transaction.scalar(self.owned_session(user_id, run.session_id))
            if session is None or session.project_id != run.project_id:
                raise DatabaseError("Run not found", 404)
        return run

    async def claim_run(self, run_id: int) -> Run | None:
        async with self.db.transaction() as transaction:
            run = await transaction.scalar(update(Run).where(
                Run.id == run_id, Run.status == "waiting",
            ).values(status="running", started_at=func.now()).returning(Run))
        return run

    async def finish_run(self, run_id: int, *, error: str | None = None) -> bool:
        async with self.db.transaction() as transaction:
            result = await transaction.execute(update(Run).where(
                Run.id == run_id, Run.status.in_(["waiting", "running"]),
            ).values(status="failed" if error is not None else "success",
                     finished_at=func.now(), error=error))
        return result.rowcount == 1

    async def fail_interrupted_runs(self):
        async with self.db.transaction() as transaction:
            await transaction.execute(update(Run).where(Run.status.in_(["waiting", "running"])).values(
                status="failed", finished_at=func.now(), error="Service execution interrupted",
            ))

    async def create_user(self, user_id: str) -> User:
        async with self.db.transaction() as transaction:
            user = User(user_id=user_id)
            transaction.add(user)
            await transaction.flush()
        return user

    async def create_project(self, user_id: str, name: str) -> Project:
        async with self.db.transaction() as transaction:
            if await transaction.scalar(select(User.id).where(User.user_id == user_id).with_for_update()) is None:
                raise DatabaseError("User not found", 404)
            project = Project(user_id=user_id, name=name)
            transaction.add(project)
            await transaction.flush()
        return project

    async def create_session(self, user_id: str, project_id: int, name: str | None = None) -> Session:
        async with self.db.transaction() as transaction:
            project = await transaction.scalar(select(Project).join(
                User, User.user_id == Project.user_id,
            ).where(Project.id == project_id, Project.user_id == user_id).with_for_update(of=Project))
            if project is None:
                raise DatabaseError("Project not found", 404)
            if name is None:
                count = await transaction.scalar(select(func.count()).select_from(Session).where(
                    Session.project_id == project_id, Session.user_id == user_id,
                ))
                name = f"会话 {count + 1}"
            session = Session(user_id=user_id, project_id=project_id, name=name)
            transaction.add(session)
            await transaction.flush()
        return session

    async def list_projects(self, user_id: str) -> list[Project]:
        async with self.db.transaction() as transaction:
            return list(await transaction.scalars(select(Project).where(
                Project.user_id == user_id,
            ).order_by(Project.id)))

    async def list_sessions(self, user_id: str, project_id: int) -> list[Session]:
        async with self.db.transaction() as transaction:
            if await transaction.scalar(select(Project.id).where(
                Project.id == project_id, Project.user_id == user_id,
            )) is None:
                raise DatabaseError("Project not found", 404)
            return list(await transaction.scalars(select(Session).where(
                Session.project_id == project_id, Session.user_id == user_id,
            ).order_by(Session.id)))

    async def rename_session(self, user_id: str, session_id: int, name: str) -> Session:
        async with self.db.transaction() as transaction:
            session = await transaction.scalar(self.owned_session(user_id, session_id).with_for_update(of=Session))
            if session is None:
                raise DatabaseError("Session not found", 404)
            session.name = name
        return session

    async def session_detail(self, user_id: str, session_id: int) -> tuple[Session, Run | None]:
        # A single statement keeps the sequence watermark and latest run in one PG snapshot.
        latest = select(Run.id).where(
            Run.session_id == Session.id, Run.user_id == Session.user_id, Run.project_id == Session.project_id,
        ).order_by(Run.id.desc()).limit(1).correlate(Session).scalar_subquery()
        async with self.db.transaction() as transaction:
            row = (await transaction.execute(self.owned_session(user_id, session_id).add_columns(Run).outerjoin(
                Run, Run.id == latest,
            ))).first()
            if row is None:
                raise DatabaseError("Session not found", 404)
            return row[0], row[1]

    async def list_messages(self, user_id: str, session_id: int, *, before_seq: int | None, limit: int) -> dict:
        async with self.db.transaction() as transaction:
            if await transaction.scalar(self.owned_session(user_id, session_id)) is None:
                raise DatabaseError("Session not found", 404)
            query = select(Message).where(
                Message.session_id == session_id, Message.role.in_(("user", "assistant", "tool")),
            )
            if before_seq is not None:
                query = query.where(Message.seq < before_seq)
            rows = list(await transaction.scalars(query.order_by(Message.seq.desc()).limit(limit + 1)))
            has_more = len(rows) > limit
            rows = list(reversed(rows[:limit]))
            items = []
            for row in rows:
                item = {"seq": row.seq, "role": row.role}
                if row.role == "tool":
                    call = row.tool_call if isinstance(row.tool_call, dict) else {}
                    item.update(tool_name=row.tool_name or "", arguments=call.get("arguments", ""))
                else:
                    item["content"] = row.content
                items.append(item)
            return {"items": items, "has_more": has_more,
                    "next_before_seq": rows[0].seq if has_more else None}

    async def append_message(
        self, *, user_id: str, project_id: int, session_id: int,
        role: str, content: str, expected_seq: int | None = None, load_context=False,
        tool_call_id: str | None = None, tool_name: str | None = None,
        tool_call: dict | list[dict] | None = None,
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
                          tool_call_id=tool_call_id, tool_name=tool_name, tool_call=tool_call)
            transaction.add_all([Message(**values), SessionMessage(**values)])
            session.next_msg_seq = seq + 1
            await transaction.flush()
            messages = []
            if load_context:
                rows = (await transaction.execute(select(
                    SessionMessage.role, SessionMessage.content, SessionMessage.tool_call_id,
                    SessionMessage.tool_name, SessionMessage.tool_call,
                ).where(SessionMessage.session_id == session_id).order_by(SessionMessage.seq))).mappings()
                messages = [{key: value for key, value in row.items() if value is not None} for row in rows]
        return seq + 1, messages
