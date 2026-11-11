import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx2
import pytest
from sqlalchemy import select

from laxcode.api.app import create_app
from laxcode.runtime.cancellation import CancelCommand, CancelResult, RunSnapshot
from laxcode.runtime.events import EventStore
from laxcode.runtime.task_manager import TaskManager
from laxcode.storage.database import Database, DatabaseError
from laxcode.storage.models import Run
from laxcode.storage.repositories import Repository
from model_helpers import ModelStream, client_for
from test_chat import chat_api, chat_path, payload


@asynccontextmanager
async def second_api(h):
    # Separate connections and managers: no shared task maps, locks, or cancel flags.
    async with Database.open(h.config.database) as db, EventStore.connect(h.config.nats.url) as events:
        create = events.create_run
        async def tracked_create(run_id, **kwargs):
            await create(run_id, **kwargs)
            if run_id not in h.created:
                h.created.append(run_id)
        events.create_run = tracked_create
        context = replace(h.context, repository=Repository(db), events=events)
        manager = TaskManager(h.manager.graph, context, cancel_timeout=2)
        await manager.start()
        app = create_app()
        app.state.config = h.config
        app.state.agent_context = context
        app.state.task_manager = manager
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url='http://other',
                                     trust_env=False, headers=dict(h.api.headers)) as api:
            try:
                yield api, manager
            finally:
                await manager.close()


@pytest.mark.anyio
@pytest.mark.parametrize('action', ['cancel', 'delete'])
async def test_remote_control_waits_for_owner_cleanup_and_coalesces_duplicates(chat_api, action):
    h = chat_api
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Graph:
        async def ainvoke(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
    h.manager.graph = Graph()
    async with second_api(h) as (remote, manager):
        run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
        await asyncio.wait_for(entered.wait(), 2)
        cancel_path = f'/api/v1/runs/{run_id}/cancel'
        tasks = [asyncio.create_task(remote.post(cancel_path)) for _ in range(3)]
        await asyncio.wait_for(cleaning.wait(), 2)
        if action == 'delete':
            tasks.append(asyncio.create_task(remote.delete(f"/api/v1/sessions/{h.conversation['session_id']}")))
        try:
            await asyncio.sleep(.05)
            assert all(not task.done() for task in tasks)
            assert (await remote.get(f'/api/v1/runs/{run_id}')).json()['status'] == 'running'
            assert not manager.tasks
            assert h.manager.tasks[run_id].cancelling() == 1
        finally:
            release.set()
        responses = await asyncio.gather(*tasks)
        assert all(r.status_code == 200 and r.json()['status'] == 'cancelled' for r in responses[:3])
        if action == 'delete':
            assert responses[-1].status_code == 204
        assert not h.manager.tasks


@pytest.mark.anyio
async def test_orphan_times_out_keeps_intent_and_blocks_chat_and_delete(chat_api):
    h = chat_api
    run = await h.context.repository.create_run(user_id=h.conversation['user_id'], session_id=h.conversation['session_id'],
        user_query='orphan', model='example:model-name', context_limit=100, output_limit=10)
    async with second_api(h) as (remote, manager):
        manager.cancel_timeout = .15
        path = f'/api/v1/runs/{run.id}/cancel'
        assert (await remote.post(path)).status_code == 504
        current = await h.context.repository.get_run(run.id, run.user_id)
        requested = current.cancel_requested_at
        assert current.status == 'waiting' and requested is not None
        assert (await remote.post(chat_path(h), json=payload())).status_code == 409
        assert (await remote.delete(f'/api/v1/sessions/{run.session_id}')).status_code == 504
        assert (await remote.get(f'/api/v1/sessions/{run.session_id}')).status_code == 200
        assert (await remote.post(path)).status_code == 504
        assert (await h.context.repository.get_run(run.id, run.user_id)).cancel_requested_at == requested
        assert (await manager.cancellation.js.stream_info('cancel_result')).state.consumer_count == 0


@pytest.mark.anyio
async def test_remote_cancel_during_commit_to_dispatch_gap(chat_api, monkeypatch):
    h = chat_api
    entered, release = asyncio.Event(), asyncio.Event()
    create = h.context.events.create_run
    async def blocked(run_id, **kwargs):
        entered.set()
        await release.wait()
        await create(run_id, **kwargs)
    monkeypatch.setattr(h.context.events, 'create_run', blocked)
    async with second_api(h) as (remote, manager):
        submitting = asyncio.create_task(h.api.post(chat_path(h), json=payload()))
        await asyncio.wait_for(entered.wait(), 2)
        async with h.context.repository.db.transaction() as tx:
            run_id = await tx.scalar(select(Run.id))
        cancelling = asyncio.create_task(remote.post(f'/api/v1/runs/{run_id}/cancel'))
        try:
            async with asyncio.timeout(2):
                while (await h.context.repository.get_run(run_id, h.conversation['user_id'])).cancel_requested_at is None:
                    await asyncio.sleep(.01)
            # Deliver the command while neither manager has a task.
            await asyncio.sleep(.05)
            assert run_id not in h.manager.tasks and not manager.tasks
        finally:
            release.set()
        assert (await submitting).status_code == 202
        result = await cancelling
        assert result.status_code == 200 and result.json()['status'] == 'cancelled'
        assert result.json()['started_at'] is None and not h.captured


@pytest.mark.anyio
async def test_remote_failure_reply_is_prompt_safe_and_retryable(chat_api, monkeypatch):
    h = chat_api
    entered = asyncio.Event()
    async with client_for(ModelStream([], wait=entered), []) as client:
        h.context.client = client
        async with second_api(h) as (remote, manager):
            run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
            await asyncio.wait_for(entered.wait(), 2)
            finish = h.context.repository.finish_cancellation
            async def broken(*args):
                raise DatabaseError('credential-secret')
            monkeypatch.setattr(h.context.repository, 'finish_cancellation', broken)
            async with asyncio.timeout(1):
                result = await remote.post(f'/api/v1/runs/{run_id}/cancel')
            assert result.status_code == 503
            assert '取消收尾状态保存失败' in result.text and 'credential-secret' not in result.text
            assert (await remote.get(f'/api/v1/runs/{run_id}')).json()['status'] == 'running'
            monkeypatch.setattr(h.context.repository, 'finish_cancellation', finish)
            assert (await remote.post(f'/api/v1/runs/{run_id}/cancel')).json()['status'] == 'cancelled'


@pytest.mark.anyio
async def test_old_commands_and_results_do_not_cancel_resumed_attempt(chat_api):
    h = chat_api
    first, second = asyncio.Event(), asyncio.Event()
    async with client_for([ModelStream([], wait=first), ModelStream([], wait=second)], []) as client:
        h.context.client = client
        async with second_api(h) as (remote, manager):
            run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
            await asyncio.wait_for(first.wait(), 2)
            assert (await remote.post(f'/api/v1/runs/{run_id}/cancel')).status_code == 200
            old = await h.context.repository.get_run(run_id, h.conversation['user_id'])
            assert (await h.api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})).status_code == 202
            await asyncio.wait_for(second.wait(), 2)
            await manager.cancellation.request(CancelCommand(run_id=run_id, resume_time=0))
            await manager.cancellation.reply(CancelResult(run_id=run_id, resume_time=0, run=RunSnapshot.model_validate(old)))
            await asyncio.sleep(.05)
            current = await h.context.repository.get_run(run_id, old.user_id)
            assert current.resume_time == 1 and current.cancel_requested_at is None and current.status == 'running'
            assert not h.manager.tasks[run_id].cancelling()
            assert await h.context.repository.claim_run(run_id, 0) is None
            result = await remote.post(f'/api/v1/runs/{run_id}/cancel')
            assert result.json()['status'] == 'cancelled' and result.json()['resume_time'] == 1


@pytest.mark.anyio
async def test_delayed_result_returns_original_snapshot_after_resume(chat_api, monkeypatch):
    h = chat_api
    first, second, replying, release = (asyncio.Event() for _ in range(4))
    reply = h.manager.cancellation.reply
    async def blocked(result):
        if result.resume_time == 0:
            replying.set()
            await release.wait()
        await reply(result)
    monkeypatch.setattr(h.manager.cancellation, 'reply', blocked)
    async with client_for([ModelStream([], wait=first), ModelStream([], wait=second)], []) as client:
        h.context.client = client
        async with second_api(h) as (remote, manager):
            run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
            await asyncio.wait_for(first.wait(), 2)
            cancelling = asyncio.create_task(remote.post(f'/api/v1/runs/{run_id}/cancel'))
            await asyncio.wait_for(replying.wait(), 2)
            try:
                assert (await h.api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})).status_code == 202
                await asyncio.wait_for(second.wait(), 2)
            finally:
                release.set()
            ended = (await cancelling).json()
            assert ended['resume_time'] == 0 and ended['status'] == 'cancelled'
            assert (await remote.get(f'/api/v1/runs/{run_id}')).json()['resume_time'] == 1
            assert (await remote.post(f'/api/v1/runs/{run_id}/cancel')).json()['resume_time'] == 1


@pytest.mark.anyio
async def test_concurrent_admission_across_managers_uses_database(chat_api):
    h = chat_api
    entered = asyncio.Event()
    async with client_for(ModelStream([], wait=entered), []) as client:
        h.context.client = client
        async with second_api(h) as (remote, manager):
            responses = await asyncio.gather(h.api.post(chat_path(h), json=payload()), remote.post(chat_path(h), json=payload()))
            assert sorted(r.status_code for r in responses) == [202, 409]
            run_id = next(r.json()['run_id'] for r in responses if r.status_code == 202)
            await asyncio.wait_for(entered.wait(), 2)
            assert (await h.api.post(f'/api/v1/runs/{run_id}/cancel')).json()['status'] == 'cancelled'



@pytest.mark.anyio
async def test_delete_loses_to_new_run_without_cancelling_it(chat_api, monkeypatch):
    h = chat_api
    entered = asyncio.Event()
    async with client_for(ModelStream([], wait=entered), []) as client:
        h.context.client = client
        async with second_api(h) as (remote, manager):
            run_id = (await h.api.post(chat_path(h), json=payload())).json()['run_id']
            await asyncio.wait_for(entered.wait(), 2)
            delete = manager.context.repository.delete_session
            created = []
            async def raced(user_id, session_id):
                created.append(await h.context.repository.create_run(user_id=user_id, session_id=session_id,
                    user_query='new', model='example:model-name', context_limit=100, output_limit=10))
                await delete(user_id, session_id)
            monkeypatch.setattr(manager.context.repository, 'delete_session', raced)
            response = await remote.delete(f"/api/v1/sessions/{h.conversation['session_id']}")
            assert response.status_code == 409
            assert (await remote.get(f'/api/v1/runs/{run_id}')).json()['status'] == 'cancelled'
            newer = await h.context.repository.get_run(created[0].id, created[0].user_id)
            assert newer.status == 'waiting' and newer.cancel_requested_at is None
