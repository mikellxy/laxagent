import json

from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from laxcode.tools.builtin.ask_user_question import Answers
from laxcode.run_errors import RunError, registered_run_error
from laxcode.storage.database import Database, DatabaseError
from laxcode.storage.models import Appendix, Memory, Message, Project, Run, Session, SessionMessage, User, UserConfig
from laxcode.appendices import MAX_ATTACHMENTS, MAX_MESSAGE_BYTES
from laxcode.memory import MemoryCategory, MemoryUnavailableError


class Repository:
    def __init__(self, db: Database):
        self.db = db

    async def create_appendix(self, **values) -> Appendix:
        async with self.db.transaction() as transaction:
            row = Appendix(**values)
            transaction.add(row)
            await transaction.flush()
        return row

    async def get_appendices(self, user_id: str, ids: list[int]) -> dict[int, Appendix]:
        async with self.db.transaction() as transaction:
            rows = await transaction.scalars(select(Appendix).where(Appendix.user_id == user_id, Appendix.id.in_(ids)))
            return {row.id: row for row in rows}

    @staticmethod
    def appendix_history_query(session_id: int):
        return select(SessionMessage.id).where(
            SessionMessage.session_id == session_id, SessionMessage.role == "user",
            SessionMessage.appendix != [],
        ).exists().select()

    async def session_has_appendix(self, user_id: str, session_id: int) -> bool:
        async with self.db.transaction() as transaction:
            if await transaction.scalar(self.owned_session(user_id, session_id)) is None:
                raise DatabaseError("Session not found", 404)
            return bool(await transaction.scalar(self.appendix_history_query(session_id)))

    async def get_user_configs(self, user_id: str) -> dict[str, str]:
        async with self.db.transaction() as transaction:
            configs = await transaction.scalar(select(UserConfig.configs).where(UserConfig.user_id == user_id))
            return dict(configs or {})

    async def update_user_config(self, user_id: str, key: str, value: str) -> dict[str, str]:
        statement = insert(UserConfig).values(user_id=user_id, configs={key: value})
        # Merge in the database, including when concurrent requests create the first row.
        statement = statement.on_conflict_do_update(
            index_elements=[UserConfig.user_id],
            set_={"configs": UserConfig.configs.op("||")(statement.excluded.configs)},
        ).returning(UserConfig.configs)
        async with self.db.transaction() as transaction:
            return dict(await transaction.scalar(statement))

    async def list_memories(self, user_id: str) -> list[dict]:
        async with self.db.transaction() as transaction:
            rows = (await transaction.execute(select(
                Memory.category, Memory.id, Memory.abstract, Memory.detail,
            ).where(Memory.user_id == user_id).order_by(Memory.id))).mappings()
            return [dict(row) for row in rows]

    async def delete_memories(self, user_id: str, memory_id: int | None = None) -> None:
        statement = delete(Memory).where(Memory.user_id == user_id)
        if memory_id is not None:
            statement = statement.where(Memory.id == memory_id)
        async with self.db.transaction() as transaction:
            await transaction.execute(statement)

    async def write_memory(self, *, user_id: str, abstract: str, detail: str,
                           category: MemoryCategory, merge_ids: list[int]) -> dict:
        ids = sorted(set(merge_ids))
        async with self.db.transaction() as transaction:
            if ids:
                locked = list(await transaction.scalars(select(Memory.id).where(
                    Memory.user_id == user_id, Memory.id.in_(ids),
                ).order_by(Memory.id).with_for_update()))
                if locked != ids:
                    raise MemoryUnavailableError()
            memory = Memory(user_id=user_id, abstract=abstract, detail=detail, category=category)
            transaction.add(memory)
            await transaction.flush()
            if ids:
                await transaction.execute(delete(Memory).where(Memory.user_id == user_id, Memory.id.in_(ids)))
            result = {"id": memory.id, "category": memory.category,
                      "abstract": memory.abstract, "detail": memory.detail}
        return result

    async def list_memory_abstracts(self, *, user_id: str, categories: list[MemoryCategory]) -> list[dict]:
        async with self.db.transaction() as transaction:
            rows = (await transaction.execute(select(Memory.category, Memory.id, Memory.abstract).where(
                Memory.user_id == user_id, Memory.category.in_(set(categories)),
            ).order_by(Memory.category, Memory.id))).mappings()
            return [dict(row) for row in rows]

    async def get_memory_details(self, *, user_id: str, ids: list[int]) -> list[dict]:
        unique_ids = list(dict.fromkeys(ids))
        async with self.db.transaction() as transaction:
            rows = (await transaction.execute(select(Memory.category, Memory.id, Memory.detail).where(
                Memory.user_id == user_id, Memory.id.in_(unique_ids),
            ))).mappings()
            by_id = {row["id"]: dict(row) for row in rows}
            if len(by_id) != len(unique_ids):
                raise MemoryUnavailableError()
            return [by_id[memory_id] for memory_id in unique_ids]

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
                Session.user_id == user_id, Session.project_id == project.id, Session.name == "LaxCode",
                Session.deleted_at.is_(None),
            ).order_by(Session.id).limit(1))
            if session is None:
                session = Session(user_id=user_id, project_id=project.id, name="LaxCode")
                transaction.add(session)
                await transaction.flush()
            return session.id

    @staticmethod
    def owned_session(user_id: str, session_id: int, *, include_deleted=False):
        query = select(Session).join(Project, Project.id == Session.project_id).join(
            User, User.user_id == Session.user_id,
        ).where(Session.id == session_id, Session.user_id == user_id, Project.user_id == user_id)
        return query if include_deleted else query.where(Session.deleted_at.is_(None))

    async def session_for_deletion(self, user_id: str, session_id: int) -> Session:
        async with self.db.transaction() as transaction:
            session = await transaction.scalar(self.owned_session(user_id, session_id, include_deleted=True))
            if session is None:
                raise DatabaseError("Session not found", 404)
            return session

    async def delete_session(self, user_id: str, session_id: int):
        async with self.db.transaction() as transaction:
            session = await transaction.scalar(self.owned_session(
                user_id, session_id, include_deleted=True,
            ).with_for_update(of=Session))
            if session is None:
                raise DatabaseError("Session not found", 404)
            if session.deleted_at is not None:
                return
            if await transaction.scalar(select(Run.id).where(
                Run.session_id == session_id, Run.status.in_(["waiting", "running", "paused"]),
            )) is not None:
                raise DatabaseError("Session still has an active run", 409)
            session.deleted_at = func.now()

    async def create_run(self, *, user_id: str, session_id: int, user_query: str,
                         model: str, context_limit: int, output_limit: int,
                         appendix: list[dict] | None = None, supports_multimodal: bool = False) -> Run:
        async with self.db.transaction() as transaction:
            session = await transaction.scalar(self.owned_session(user_id, session_id).with_for_update(of=Session))
            if session is None:
                raise DatabaseError("Session not found", 404)
            await self.check_no_active_run(transaction, session.id)
            refs = appendix or []
            ids = [ref["id"] for ref in refs]
            if len(ids) > MAX_ATTACHMENTS or len(set(ids)) != len(ids):
                raise DatabaseError("最多添加 5 个附件，且不能重复", 422)
            rows = {row.id: row for row in await transaction.scalars(select(Appendix).where(
                Appendix.user_id == user_id, Appendix.id.in_(ids),
            ))} if ids else {}
            if set(ids) - rows.keys():
                raise DatabaseError("附件不存在", 404)
            if sum(row.size_bytes for row in rows.values()) > MAX_MESSAGE_BYTES:
                raise DatabaseError("附件合计不能超过 20 MiB", 422)
            if not supports_multimodal and (ids or await transaction.scalar(self.appendix_history_query(session.id))):
                raise DatabaseError("此会话包含附件，请选择支持多模态的模型，或新建会话", 422)
            run = Run(user_id=user_id, session_id=session.id, project_id=session.project_id,
                      user_query=user_query, model=model, context_limit=context_limit,
                      appendix=[{"id": identifier, "name": rows[identifier].name} for identifier in ids],
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

    @staticmethod
    async def check_no_active_run(transaction, session_id: int):
        # Call only while holding the session row lock, in the admitting transaction.
        if await transaction.scalar(select(Run.id).where(
            Run.session_id == session_id, Run.status.in_(["waiting", "running", "paused"]),
        )) is not None:
            raise DatabaseError("Session still has an active run", 409)

    async def claim_run(self, run_id: int, resume_time: int = 0) -> Run | None:
        async with self.db.transaction() as transaction:
            run = await transaction.scalar(update(Run).where(
                Run.id == run_id, Run.status == "waiting", Run.resume_time == resume_time,
            ).values(status="running", started_at=func.now()).returning(Run))
        return run

    async def pause_run(self, run_id: int, resume_time: int) -> Run:
        async with self.db.transaction() as transaction:
            run = await transaction.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.resume_time != resume_time:
                raise DatabaseError("任务执行次数已变化，请刷新后重试", 409)
            if run.status == "running":
                run.status = "paused"
                await transaction.flush()
        return run

    async def answer_run(self, user_id: str, run_id: int, resume_time: int) -> Run:
        return await self._continue_run(user_id, run_id, resume_time, answering=True)

    async def resume_run(self, user_id: str, run_id: int, resume_time: int) -> Run:
        return await self._continue_run(user_id, run_id, resume_time, answering=False)

    async def _continue_run(self, user_id: str, run_id: int, resume_time: int, *, answering: bool) -> Run:
        async with self.db.transaction() as transaction:
            run = await transaction.get(Run, run_id)
            if run is None or run.user_id != user_id:
                raise DatabaseError("Run not found", 404)
            session = await transaction.scalar(self.owned_session(user_id, run.session_id).with_for_update(of=Session))
            if session is None or session.project_id != run.project_id:
                raise DatabaseError("Run not found", 404)
            # Refresh after locking the session, which also serializes chat submission.
            run = await transaction.scalar(select(Run).where(Run.id == run_id).with_for_update()
                                           .execution_options(populate_existing=True))
            latest_id = await transaction.scalar(select(func.max(Run.id)).where(Run.session_id == session.id))
            allowed = ("paused",) if answering else ("failed", "cancelled")
            if (latest_id != run.id or run.status not in allowed or run.resume_time != resume_time
                    or (answering and run.cancel_requested_at is not None)):
                raise DatabaseError("任务状态已变化，请刷新后重试", 409)
            if not answering:
                await self.check_no_active_run(transaction, session.id)
            run.status = "waiting"
            run.resume_time += 1
            run.error = run.error_code = None
            run.started_at = run.finished_at = None
            run.cancel_requested_at = None
            await transaction.flush()
        return run

    async def finish_run(self, run_id: int, *, error: RunError | None = None, cancelled=False,
                         resume_time: int = 0) -> tuple[Run, bool]:
        """Commit one attempt's terminal state and snapshot under the same row lock."""
        error = registered_run_error(error) if error is not None else None
        async with self.db.transaction() as transaction:
            run = await transaction.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.resume_time != resume_time:
                raise DatabaseError("任务执行次数已变化，请刷新后重试", 409)
            changed = run.status in ("waiting", "running", "paused")
            if changed:
                run.status = "cancelled" if cancelled else "failed" if error is not None else "success"
                run.finished_at = func.now()
                run.error = str(error) if error else None
                run.error_code = error.code if error else None
                await transaction.flush()
                await transaction.refresh(run)
        return run, changed

    async def request_cancellation(self, user_id: str, run_id: int, resume_time: int) -> Run:
        """Fix intent to one attempt; never cancel a concurrently resumed execution."""
        async with self.db.transaction() as transaction:
            run = await transaction.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.user_id != user_id:
                raise DatabaseError("Run not found", 404)
            if await transaction.scalar(self.owned_session(user_id, run.session_id)) is None:
                raise DatabaseError("Run not found", 404)
            if run.resume_time != resume_time:
                raise DatabaseError("任务执行次数已变化，请刷新后重试", 409)
            if run.status in ("waiting", "running", "paused"):
                run.cancel_requested_at = run.cancel_requested_at or func.now()
                await transaction.flush()
                await transaction.refresh(run)
        return run

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
                Session.deleted_at.is_(None),
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
                    if row.tool_name == "ask_user_question":
                        try:
                            result = json.loads(row.content)
                            if result.get("error_code") is None:
                                item["answers"] = Answers.model_validate(result["result"]).model_dump()["answers"]
                        except (ValueError, KeyError, TypeError, AttributeError):
                            pass
                else:
                    item["content"] = row.content
                    if row.role == "user":
                        item["appendix"] = row.appendix or []
                    if row.role == "assistant":
                        item["reasoning"] = row.reasoning or []
                items.append(item)
            return {"items": items, "has_more": has_more,
                    "next_before_seq": rows[0].seq if has_more else None}

    async def append_message(
        self, *, user_id: str, project_id: int, session_id: int,
        role: str, content: str, expected_seq: int | None = None, load_context=False,
        tool_call_id: str | None = None, tool_name: str | None = None,
        tool_call: dict | list[dict] | None = None,
        reasoning: list[dict] | None = None,
        appendix: list[dict] | None = None,
        replay_safe: bool = False,
    ) -> tuple[int, list[dict]]:
        async with self.db.transaction() as transaction:
            session = await transaction.scalar(select(Session).where(
                Session.id == session_id, Session.user_id == user_id,
                Session.project_id == project_id,
                Session.deleted_at.is_(None),
            ).with_for_update())
            if session is None:
                raise DatabaseError("Session not found", 404)
            owner = await transaction.scalar(select(Project.id).join(
                User, User.user_id == Project.user_id,
            ).where(Project.id == project_id, Project.user_id == user_id))
            if owner is None:
                raise DatabaseError("Project not found", 404)
            seq = session.next_msg_seq
            replay = replay_safe and expected_seq is not None and expected_seq < seq
            if replay:
                seq = expected_seq
            elif expected_seq is not None and expected_seq != seq:
                raise DatabaseError("Session message sequence changed", 409)
            values = dict(seq=seq, session_id=session_id, role=role, content=content,
                          tool_call_id=tool_call_id, tool_name=tool_name, tool_call=tool_call,
                          reasoning=reasoning, appendix=appendix or [])
            if replay:
                for table in (Message, SessionMessage):
                    existing = await transaction.scalar(select(table).where(
                        table.session_id == session_id, table.seq == seq,
                    ))
                    if existing is None or any(getattr(existing, key) != value for key, value in values.items()):
                        raise DatabaseError("Session message replay mismatch", 409)
            else:
                transaction.add_all([Message(**values), SessionMessage(**values)])
                session.next_msg_seq = seq + 1
                await transaction.flush()
            messages = []
            if load_context:
                rows = (await transaction.execute(select(
                    SessionMessage.role, SessionMessage.content, SessionMessage.tool_call_id,
                    SessionMessage.tool_name, SessionMessage.tool_call,
                    SessionMessage.appendix,
                ).where(SessionMessage.session_id == session_id).order_by(SessionMessage.seq))).mappings()
                messages = [{key: value for key, value in row.items()
                             if value is not None and not (key == "appendix" and not value)} for row in rows]
        return seq + 1, messages
