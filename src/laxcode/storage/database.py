from contextlib import asynccontextmanager

from sqlalchemy import URL, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from laxcode.config import DatabaseConfig

SCHEMA_REVISION = "0002"


class DatabaseError(Exception):
    def __init__(self, message="Database unavailable", status=503):
        super().__init__(message)
        self.status = status


def database_url(config: DatabaseConfig) -> URL:
    return URL.create(
        "postgresql+psycopg", username=config.user, password=config.password.get_secret_value(),
        host=config.host, port=config.port, database=config.name,
    )


class Database:
    def __init__(self, config: DatabaseConfig):
        self.engine = create_async_engine(
            database_url(config), pool_pre_ping=True, hide_parameters=True,
            connect_args={"connect_timeout": 5},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    @asynccontextmanager
    async def open(cls, config: DatabaseConfig):
        db = cls(config)
        try:
            async with db.transaction() as session:
                versions = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
                if versions != [SCHEMA_REVISION]:
                    raise DatabaseError("Database schema is not current; run Alembic upgrade head")
            yield db
        finally:
            await db.engine.dispose()

    @asynccontextmanager
    async def transaction(self):
        try:
            async with self.sessions.begin() as session:
                yield session
        except IntegrityError:
            raise DatabaseError("Database record conflicts with existing data", 409) from None
        except SQLAlchemyError:
            raise DatabaseError() from None
