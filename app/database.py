from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.sqlalchemy_url,
    echo=settings.db_echo,
    # A managed database sits behind a proxy that drops idle connections
    # without telling us; pre_ping turns a stale socket into a transparent
    # reconnect instead of an error on the next request.
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args=settings.connect_args,
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
    """Create the target schema if it does not exist yet.

    Skipped when a full DATABASE_URL is configured: a managed provider gives
    you a database that already exists and an account that usually cannot run
    CREATE DATABASE at all.
    """
    if settings.uses_managed_url:
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
