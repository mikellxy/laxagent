from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Identity, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from laxcode.memory import MEMORY_CATEGORY_DESCRIPTIONS


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), unique=True)


class UserConfig(Base):
    __tablename__ = "user_configs"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(configs) = 'object'", name="ck_user_configs_object"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), unique=True)
    configs: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint(
            "category IN (" + ", ".join(repr(category) for category in MEMORY_CATEGORY_DESCRIPTIONS) + ")",
            name="ck_memories_category",
        ),
        CheckConstraint("abstract ~ '[^[:space:]]'", name="ck_memories_abstract_not_blank"),
        CheckConstraint("detail ~ '[^[:space:]]'", name="ck_memories_detail_not_blank"),
        Index("ix_memories_user_id_category_id", "user_id", "category", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36))
    abstract: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32))


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(String(36), index=True)


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("next_msg_seq > 0", name="ck_sessions_next_msg_seq"),
        Index("ix_sessions_user_id_project_id", "user_id", "project_id"),
        Index("ix_sessions_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    project_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[str] = mapped_column(String(36))
    next_msg_seq: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("resume_time >= 0", name="ck_runs_resume_time"),
        CheckConstraint("status IN ('waiting', 'running', 'success', 'failed', 'cancelled')", name="ck_runs_status"),
        CheckConstraint("context_limit > 0 AND output_limit > 0 AND output_limit <= context_limit",
                        name="ck_runs_limits"),
        Index("ix_runs_session_id_id", "session_id", "id"),
        Index("uq_runs_active_session", "session_id", unique=True,
              postgresql_where=text("status IN ('waiting', 'running')")),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    session_id: Mapped[int] = mapped_column(Integer)
    project_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'waiting'"))
    user_query: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    context_limit: Mapped[int] = mapped_column(Integer)
    output_limit: Mapped[int] = mapped_column(Integer)
    thread_id: Mapped[str] = mapped_column(String(36), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    resume_time: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageColumns:
    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer)
    session_id: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    reasoning: Mapped[list[dict] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    role: Mapped[str] = mapped_column(Text)
    tool_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call: Mapped[dict | list[dict] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)


class Message(MessageColumns, Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_messages_session_seq"),
        CheckConstraint("seq > 0", name="ck_messages_seq"),
        CheckConstraint("role IN ('system', 'user', 'assistant', 'tool')", name="ck_messages_role"),
    )


class SessionMessage(MessageColumns, Base):
    __tablename__ = "session_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_session_messages_session_seq"),
        CheckConstraint("seq > 0", name="ck_session_messages_seq"),
        CheckConstraint("role IN ('system', 'user', 'assistant', 'tool')", name="ck_session_messages_role"),
    )
