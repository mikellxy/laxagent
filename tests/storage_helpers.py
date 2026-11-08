from sqlalchemy import select

from laxcode.storage.models import Message, Session, SessionMessage


async def run_agent(graph, state, context):
    """Seed a persisted run for the existing graph/message regression scenarios."""
    from laxcode.runtime.runner import run_agent as execute

    run = await context.repository.create_run(
        user_id=state["user_id"], session_id=state["session_id"], user_query=state["user_query"],
        model=state["model"], context_limit=128000, output_limit=16000,
    )
    await context.events.create_run(run.id)
    try:
        return await execute(graph, run.id, context)
    finally:
        stored = await context.repository.get_run(run.id, run.user_id)
        assert stored.status in ("success", "failed") and stored.finished_at is not None
        assert (stored.error is not None) == (stored.status == "failed")


async def message_snapshot(repository, session_id):
    async with repository.db.transaction() as transaction:
        session = await transaction.get(Session, session_id)
        rows = []
        for model in (Message, SessionMessage):
            rows.append((await transaction.execute(select(
                model.seq, model.role, model.content, model.tool_call_id, model.tool_call, model.tool_name,
            ).where(model.session_id == session_id).order_by(model.seq))).all())
        return session.next_msg_seq, rows

