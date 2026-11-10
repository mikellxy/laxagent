import asyncio
import json
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text


@pytest.mark.anyio
async def test_control_migration_roundtrip_preserves_records(repository, conversation, config_data, tmp_path):
    run = await repository.create_run(user_id=conversation['user_id'], session_id=conversation['session_id'],
        user_query='test', model='example:model-name', context_limit=100, output_limit=10)
    await repository.finish_run(run.id, cancelled=True)
    await repository.delete_session(conversation['user_id'], conversation['session_id'])
    path = tmp_path / 'migration.json'
    path.write_text(json.dumps(config_data))
    config = Config('alembic.ini')
    config.cmd_opts = SimpleNamespace(x=[f'config={path}'])
    try:
        await asyncio.to_thread(command.downgrade, config, '0003')
        async with repository.db.transaction() as tx:
            result = (await tx.execute(text('SELECT status, error FROM runs WHERE id = :id'), {'id': run.id})).one()
            assert result == ('failed', 'Execution cancelled')
            assert await tx.scalar(text('SELECT count(*) FROM sessions WHERE id = :id'), {'id': conversation['session_id']}) == 1
    finally:
        await asyncio.to_thread(command.upgrade, config, 'head')
    async with repository.db.transaction() as tx:
        assert await tx.scalar(text('SELECT deleted_at FROM sessions WHERE id = :id'), {'id': conversation['session_id']}) is None
        assert await tx.scalar(text('SELECT version_num FROM alembic_version')) == '0006'
