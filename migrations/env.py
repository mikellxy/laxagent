from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.exc import SQLAlchemyError

from laxcode.config import load_config
from laxcode.storage.database import database_url
from laxcode.storage.models import Base

config_path = Path(context.get_x_argument(as_dictionary=True).get("config", "config/config.json"))
url = database_url(load_config(config_path).database)


def migrate(connection=None):
    context.configure(
        connection=connection, url=url if connection is None else None,
        target_metadata=Base.metadata, literal_binds=connection is None,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    migrate()
else:
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            migrate(connection)
    except SQLAlchemyError:
        raise RuntimeError("Database migration failed; check connection and schema") from None
    finally:
        engine.dispose()
