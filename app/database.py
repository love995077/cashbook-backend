from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

settings = get_settings()


def engine_options(url: str, connect_args: dict[str, Any], echo: bool = False) -> dict[str, Any]:
    """Keyword arguments for `create_engine`, shaped for the engine in use.

    Shared with the test suite and the migration tooling so a PostgreSQL
    engine built anywhere in this project behaves like the app's own.
    """
    options: dict[str, Any] = {"echo": echo, "connect_args": connect_args}

    if url.startswith("sqlite"):
        # An in-memory SQLite database exists per connection; StaticPool keeps
        # the one connection that holds it. Only tests and the throwaway dev
        # server use SQLite.
        if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
            options["poolclass"] = StaticPool
        return options

    options.update(
        # The database sits behind a network that can drop idle connections
        # without telling us; pre_ping turns a stale socket into a transparent
        # reconnect instead of an error on the next request.
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    return options


engine = create_engine(
    settings.sqlalchemy_url,
    **engine_options(settings.sqlalchemy_url, settings.connect_args, settings.db_echo),
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_database_if_missing() -> None:
    """Create the target MySQL schema if it does not exist yet.

    Skipped when a full DATABASE_URL is configured - a managed provider gives
    you a database that already exists and an account that usually cannot run
    CREATE DATABASE at all - and for any engine other than MySQL. The
    PostgreSQL container creates its database from POSTGRES_DB on first start.
    """
    if settings.uses_managed_url or settings.dialect != "mysql":
        return

    server_engine = create_engine(
        settings.server_url, echo=settings.db_echo, connect_args=settings.connect_args
    )
    with server_engine.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{settings.db_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
        conn.commit()
    server_engine.dispose()


def init_db() -> None:
    """Create the database (if needed) and all tables declared on Base."""
    # Importing the models registers them on Base.metadata.
    from app import models  # noqa: F401

    create_database_if_missing()
    Base.metadata.create_all(bind=engine)
