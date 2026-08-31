"""Database engine, session factory, and declarative base.

Works unchanged against SQLite (dev default) or Postgres — the only
SQLite-specific tweak (`check_same_thread=False`) is applied conditionally so a
`DATABASE_URL` pointing at Postgres needs no code changes. See CLAUDE.md "Stack".
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DATABASE_URL

# SQLite requires check_same_thread=False for use across FastAPI worker threads.
# This connect arg is invalid for other backends, so only pass it for SQLite.
_is_sqlite = DATABASE_URL.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session and closes it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
