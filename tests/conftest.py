"""Test harness: a throwaway database per test, the real app, a pinned clock.

WHICH DATABASE
--------------
SQLite in memory by default - fast, and no setup. Set TEST_DATABASE_URL to run
the same suite against a real engine:

    TEST_DATABASE_URL=postgresql+psycopg://mm:mm@127.0.0.1:55432/mm_test pytest
    TEST_DATABASE_URL=mysql+pymysql://mm:mm@127.0.0.1:53306/mm_test pytest

PostgreSQL is the production engine and the one that matters most: it is
exact about NUMERIC (SQLite stores DECIMAL through a float), case-sensitive
about strings, and actually takes the row locks the money paths ask for. The
schema there is built by the real Alembic chain, so the suite also proves the
migrations produce what the models expect.

THE CONFIGURED DATABASE IS NEVER TOUCHED
----------------------------------------
`.env` points at a live database. The environment is overridden before the
app is imported - DATABASE_URL to an in-memory SQLite - so even code that
reached for `app.database.engine` directly would land somewhere harmless, and
every request goes through `get_db`, which is overridden to the test engine.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from datetime import datetime, time
from zoneinfo import ZoneInfo

# --- before anything imports the app ----------------------------------------
_MEDIA = tempfile.mkdtemp(prefix="moneymate-test-media-")
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AUTO_CREATE_TABLES"] = "false"
os.environ["MEDIA_ROOT"] = _MEDIA
os.environ["JWT_SECRET"] = "test-only-secret-not-for-production-use-0123456789"
os.environ["BUSINESS_TIMEZONE"] = "Asia/Kolkata"
os.environ["NOTIFICATIONS_ENABLED"] = "false"
os.environ.pop("ALEMBIC_DATABASE_URL", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app import models  # noqa: E402,F401  (registers every table)
from app.config import connect_args_for, dialect_of, normalise_database_url  # noqa: E402
from app.core import clock  # noqa: E402
from app.database import Base, engine_options, get_db  # noqa: E402
from app.main import app  # noqa: E402

from tests.helpers import TODAY  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

#: The moment every test treats as "now" unless it moves the clock itself:
#: midday IST on TODAY (a Tuesday), well clear of the midnight window.
NOON_IST = datetime.combine(TODAY, time(12, 0), tzinfo=IST)

TEST_DATABASE_URL = normalise_database_url(os.environ.get("TEST_DATABASE_URL", "sqlite://"))
DIALECT = dialect_of(TEST_DATABASE_URL)


def _build_engine() -> Engine:
    return create_engine(
        TEST_DATABASE_URL,
        **engine_options(TEST_DATABASE_URL, connect_args_for(TEST_DATABASE_URL)),
    )


def _migrate_to_head(engine: Engine) -> None:
    """Empty the database and build it with the real Alembic chain."""
    from alembic import command
    from alembic.config import Config

    with engine.begin() as conn:
        if DIALECT == "postgresql":
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        elif DIALECT == "mysql":
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            for (table,) in conn.execute(text("SHOW TABLES")).all():
                conn.execute(text(f"DROP TABLE `{table}`"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    config = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    config.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic")
    )
    os.environ["ALEMBIC_DATABASE_URL"] = TEST_DATABASE_URL
    try:
        command.upgrade(config, "head")
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)


def _truncate(engine: Engine) -> None:
    tables = [table.name for table in Base.metadata.sorted_tables]
    with engine.begin() as conn:
        if DIALECT == "postgresql":
            conn.execute(
                text(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE")
            )
        elif DIALECT == "mysql":
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            for table in tables:
                conn.execute(text(f"TRUNCATE TABLE `{table}`"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


@pytest.fixture(scope="session")
def server_engine() -> Iterator[Engine]:
    """For a real engine: one schema per session, built by the migrations."""
    if DIALECT == "sqlite":
        yield None  # type: ignore[misc]
        return
    engine = _build_engine()
    _migrate_to_head(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def engine(server_engine: Engine) -> Iterator[Engine]:
    """A clean database for one test."""
    if DIALECT == "sqlite":
        fresh = _build_engine()

        # SQLite does not enforce foreign keys unless asked, and the ledger
        # leans on them (SET NULL, CASCADE). Ask, on every connection.
        from sqlalchemy import event

        @event.listens_for(fresh, "connect")
        def _fks(dbapi_connection, _record):  # pragma: no cover - trivial
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(fresh)
        yield fresh
        fresh.dispose()
        return

    _truncate(server_engine)
    yield server_engine


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=True)


@pytest.fixture
def db(session_factory: sessionmaker) -> Iterator[Session]:
    """A session for assertions, separate from the ones requests use."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch):
    """Pin the business clock. Returns a setter for tests that move it."""
    state = {"now": NOON_IST}

    def _now() -> datetime:
        return state["now"]

    monkeypatch.setattr(clock, "_now", _now)

    def set_now(moment: datetime) -> None:
        state["now"] = moment

    return set_now


@pytest.fixture
def client(session_factory: sessionmaker, frozen_now) -> Iterator[TestClient]:
    def _get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def api(client: TestClient, db: Session):
    from tests.helpers import Api

    return Api(client, db)
