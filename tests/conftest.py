import os
import json
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4

import nats
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from laxcode.runtime.events import EventStore
from laxcode.config import DatabaseConfig
from laxcode.storage.database import Database
from laxcode.storage.repositories import Repository


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def run_store():
    async with EventStore.connect(os.getenv("NATS_URL", "nats://127.0.0.1:4222")) as store:
        run_id = uuid4().int % (2**63 - 1) + 1
        yield store, run_id
        try:
            await store.js.delete_stream(store.stream_name(run_id))
        except nats.js.errors.NotFoundError:
            pass


@pytest.fixture(scope="session")
def database_config():
    return {
        "host": "127.0.0.1", "port": 5432, "name": "laxcode_test",
        "user": "laxcode_test", "password": "test-only-password",
    }


@pytest.fixture
def config_data(database_config):
    return {
        "llm": {
            "default_model": "example:model-name",
            "provider_list": [{
                "name": "example",
                "base_url": "https://provider.example.com/v1",
                "api_key": "sk-private-test",
                "model_list": [{
                    "name": "model-name",
                    "limit": {"context": 128000, "output": 16000},
                }],
            }],
        },
        "api_server": {"port": 8000},
        "llm_router": {"port": 8001},
        "database": dict(database_config),
        "auth": {"jwt_secret": "test-only-jwt-secret-at-least-32-bytes-for-test-coverage"},
    }


@pytest.fixture(scope="session")
def migrated_database(tmp_path_factory, database_config):
    path = tmp_path_factory.mktemp("database") / "config.json"
    config_data = json.loads(Path("config/config.example.json").read_text())
    config_data["database"] = database_config
    path.write_text(json.dumps(config_data))
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"config={path}"])
    command.upgrade(config, "head")
    return DatabaseConfig.model_validate(config_data["database"])


@pytest.fixture
async def repository(migrated_database):
    assert migrated_database.name == "laxcode_test"
    async with Database.open(migrated_database) as db:
        # Isolate JetStream names from the developer database, whose run IDs also start at 1.
        async with db.transaction() as transaction:
            await transaction.execute(text("SELECT setval(pg_get_serial_sequence('runs', 'id'), :start, false)"),
                                      {"start": 1_000_000_000 + uuid4().int % 900_000_000})
        try:
            yield Repository(db)
        finally:
            async with db.transaction() as transaction:
                await transaction.execute(text("TRUNCATE runs, session_messages, messages, sessions, projects, users RESTART IDENTITY"))


@pytest.fixture
async def conversation(repository):
    user_id = str(uuid4())
    await repository.create_user(user_id)
    project = await repository.create_project(user_id, "test-project")
    session = await repository.create_session(user_id, project.id, "test-session")
    return {"user_id": user_id, "project_id": project.id, "session_id": session.id}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home
