from sqlalchemy import select

from laxcode.storage.models import Message, Session, SessionMessage


async def message_snapshot(repository, session_id):
    async with repository.db.transaction() as transaction:
        session = await transaction.get(Session, session_id)
        rows = []
        for model in (Message, SessionMessage):
            rows.append((await transaction.execute(select(
                model.seq, model.role, model.content, model.tool_call_id, model.tool_call, model.tool_name,
            ).where(model.session_id == session_id).order_by(model.seq))).all())
        return session.next_msg_seq, rows

