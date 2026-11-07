from sqlalchemy import CheckConstraint, Identity, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), unique=True)


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


class MessageColumns:
    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer)
    session_id: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
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
