"""The Alembic chain, on the engines it actually has to build.

Skipped on SQLite, which the chain does not support (see alembic/env.py).
Run with TEST_DATABASE_URL pointing at PostgreSQL - the production engine -
and MySQL, the database the data is moving from.
"""

import os
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests import conftest

pytestmark = pytest.mark.skipif(
    conftest.DIALECT == "sqlite", reason="the migration chain targets PostgreSQL and MySQL"
)

ROOT = os.path.join(os.path.dirname(__file__), "..")
BEFORE_LEDGER_INTEGRITY = "c4e8b2f71d95"


@pytest.fixture
def alembic_config():
    config = Config(os.path.join(ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = conftest.TEST_DATABASE_URL
    try:
        yield config
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)
        # Whatever a test did, leave the schema at head for the rest.
        os.environ["ALEMBIC_DATABASE_URL"] = conftest.TEST_DATABASE_URL
        command.upgrade(config, "head")
        os.environ.pop("ALEMBIC_DATABASE_URL", None)


def test_the_migrated_schema_is_exactly_what_the_models_declare(server_engine, alembic_config):
    # Raises if autogenerate would emit anything at all.
    command.check(alembic_config)


def test_the_chain_downgrades_to_nothing_and_rebuilds(server_engine, alembic_config):
    # From empty: some downgrades are deliberately unguarded against data the
    # newer schema allows (b7e2c9d41a83 restores NOT NULL on phone numbers),
    # and rows left by earlier tests would fail them by design.
    conftest._truncate(server_engine)
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    command.check(alembic_config)


def test_wage_rate_backfill_recovers_the_rate_each_day_was_paid_at(
    server_engine, alembic_config
):
    """Historical days get the rate their linked wage proves, piece-rate days
    get 0, and days that never earned are left NULL for the reconcile."""
    conftest._truncate(server_engine)
    command.downgrade(alembic_config, BEFORE_LEDGER_INTEGRITY)

    with server_engine.begin() as conn:
        insert_user = text(
            "INSERT INTO users (id, name, phone, password_hash, role, daily_wage, wage_type, "
            "contractor_id, is_active) VALUES (:id, :name, NULL, NULL, :role, :rate, :wtype, "
            ":boss, :active)"
        )
        conn.execute(insert_user, dict(id=1, name="Boss", role="admin", rate=None, wtype="daily", boss=None, active=True))
        conn.execute(insert_user, dict(id=2, name="Day", role="worker", rate=Decimal("601.01"), wtype="daily", boss=1, active=True))
        conn.execute(insert_user, dict(id=3, name="Raised", role="worker", rate=Decimal("700"), wtype="daily", boss=1, active=True))
        conn.execute(insert_user, dict(id=4, name="Tiler", role="worker", rate=None, wtype="piece_rate", boss=1, active=True))

        insert_wage = text(
            "INSERT INTO transactions (id, worker_id, type, amount, date) "
            "VALUES (:id, :worker, 'wage', :amount, :on)"
        )
        conn.execute(insert_wage, dict(id=10, worker=2, amount=Decimal("601.01"), on="2026-09-01"))
        conn.execute(insert_wage, dict(id=11, worker=2, amount=Decimal("300.51"), on="2026-09-02"))
        # Paid at 500 before a raise to 700: the wage, not today's rate, is the truth.
        conn.execute(insert_wage, dict(id=12, worker=3, amount=Decimal("500"), on="2026-09-01"))

        insert_day = text(
            "INSERT INTO attendance (id, worker_id, date, status, transaction_id, is_disputed) "
            "VALUES (:id, :worker, :on, :status, :tx, :disputed)"
        )
        days = [
            (100, 2, "2026-09-01", "present", 10),
            (101, 2, "2026-09-02", "half_day", 11),
            (102, 2, "2026-09-03", "present", None),  # pre-fix day: never earned
            (103, 3, "2026-09-01", "present", 12),
            (104, 4, "2026-09-01", "present", None),
            (105, 2, "2026-09-04", "absent", None),
        ]
        for row_id, worker, on, status, tx in days:
            conn.execute(
                insert_day,
                dict(id=row_id, worker=worker, on=on, status=status, tx=tx, disputed=False),
            )

    command.upgrade(alembic_config, "head")

    with server_engine.connect() as conn:
        rates = dict(conn.execute(text("SELECT id, wage_rate FROM attendance")).all())
    assert rates == {
        100: Decimal("601.01"),  # current rate, and it matches the wage
        101: Decimal("601.01"),  # 300.51 is round_half_up(601.01 / 2): same rate
        102: None,  # never earned: left for the reconcile, not invented here
        103: Decimal("500.00"),  # the rate it was paid at, not today's 700
        104: Decimal("0.00"),  # piece rate: not paid by the day
        105: None,
    }
    conftest._truncate(server_engine)


@pytest.mark.skipif(conftest.DIALECT != "postgresql", reason="PostgreSQL-only indexes")
def test_names_are_unique_without_regard_to_case_on_postgresql(server_engine):
    """MySQL's collation refused 'Fuel' beside 'fuel'. PostgreSQL would not -
    the migration's lower(name) indexes restore it."""
    conftest._truncate(server_engine)
    # Ids left to the sequences: an explicit id would not advance them, and
    # the next insert would trip the primary key before the index under test.
    with server_engine.begin() as conn:
        boss = conn.execute(
            text(
                "INSERT INTO users (name, role, wage_type, is_active) "
                "VALUES ('Boss', 'admin', 'daily', true) RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text("INSERT INTO sites (contractor_id, name, is_active) VALUES (:b, 'Villa', true)"),
            {"b": boss},
        )

    with pytest.raises(Exception) as caught:
        with server_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sites (contractor_id, name, is_active) "
                    "VALUES (:b, 'VILLA', true)"
                ),
                {"b": boss},
            )
    assert "uq_sites_contractor_lower_name_ci" in str(caught.value)
    conftest._truncate(server_engine)
