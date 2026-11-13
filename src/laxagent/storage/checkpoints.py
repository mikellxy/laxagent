"""PostgreSQL checkpoints share the business database, but own their schema."""
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import Error as PsycopgError
from psycopg.conninfo import make_conninfo

from laxagent.config import DatabaseConfig
from laxagent.storage.database import DatabaseError


@asynccontextmanager
async def open_checkpointer(config: DatabaseConfig):
    conninfo = make_conninfo(
        host=config.host, port=config.port, dbname=config.name, user=config.user,
        password=config.password.get_secret_value(), connect_timeout=5,
    )
    try:
        async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
            # Library-owned migrations are idempotent; Alembic owns only business tables.
            await saver.setup()
            yield saver
    except PsycopgError:
        raise DatabaseError() from None
