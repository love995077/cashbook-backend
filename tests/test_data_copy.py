"""The MySQL -> PostgreSQL copy, end to end, on real engines.

Needs both throwaway databases:

    TEST_COPY_MYSQL_URL=mysql+pymysql://mm:mm@127.0.0.1:53306/mm_copy
    TEST_COPY_PG_URL=postgresql+psycopg://mm:mm@127.0.0.1:55432/mm_copy

and is skipped otherwise. The source is seeded at revision c4e8b2f71d95 - the
newest one the live database can be on - with the shapes real data has:
linked day wages, a day marked before wages were linked, a half day, piece
work, a worker posted to a site (the users <-> sites cycle) and money.
"""

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
MYSQL = os.environ.get("TEST_COPY_MYSQL_URL")
PG = os.environ.get("TEST_COPY_PG_URL")

pytestmark = pytest.mark.skipif(not (MYSQL and PG), reason="needs TEST_COPY_MYSQL_URL and TEST_COPY_PG_URL")


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = url
    return config


def _seed_source() -> None:
    engine = create_engine(MYSQL)
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for (table,) in conn.execute(text("SHOW TABLES")).all():
            conn.execute(text(f"DROP TABLE `{table}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    command.upgrade(_config(MYSQL), "c4e8b2f71d95")
    os.environ.pop("ALEMBIC_DATABASE_URL", None)

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, name, phone, password_hash, role, daily_wage, wage_type, contractor_id, site_id, is_active) VALUES "
            "(1, 'Boss', '9000000001', NULL, 'admin', NULL, 'daily', NULL, NULL, 1),"
            "(2, 'Day', NULL, NULL, 'worker', 600, 'daily', 1, NULL, 1),"
            "(3, 'Tiler', NULL, NULL, 'worker', NULL, 'piece_rate', 1, NULL, 1)"
        ))
        conn.execute(text("INSERT INTO sites (id, contractor_id, name, is_active) VALUES (7, 1, 'Villa', 1)"))
        conn.execute(text("UPDATE users SET site_id = 7 WHERE id IN (2, 3)"))
        conn.execute(text(
            "INSERT INTO transactions (id, worker_id, type, amount, date) VALUES "
            "(10, 2, 'wage', 600.00, '2026-09-21'), (11, 2, 'wage', 300.00, '2026-09-22'),"
            "(12, 2, 'advance', 250.00, '2026-09-22'), (13, 3, 'wage', 2169.00, '2026-09-21')"
        ))
        conn.execute(text(
            "INSERT INTO attendance (id, worker_id, date, status, transaction_id, is_disputed, site_id) VALUES "
            "(100, 2, '2026-09-21', 'present', 10, 0, 7), (101, 2, '2026-09-22', 'half_day', 11, 0, NULL),"
            "(102, 2, '2026-09-15', 'present', NULL, 0, NULL), (103, 3, '2026-09-21', 'present', NULL, 0, 7)"
        ))
        conn.execute(text(
            "INSERT INTO piece_rate_logs (id, worker_id, site_id, work_description, quantity, rate, total_amount, date, transaction_id) "
            "VALUES (50, 3, 7, 'Tiling', 120.5, 18, 2169.00, '2026-09-21', 13)"
        ))
        conn.execute(text(
            "INSERT INTO cash_entries (id, contractor_id, site_id, direction, amount, date) VALUES "
            "(20, 1, 7, 'out', 1234.56, '2026-09-21')"
        ))


def test_the_copy_keeps_every_row_and_every_rupee():
    _seed_source()

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "migrate_mysql_to_postgres.py"), "--source", MYSQL, "--target", PG, "--apply"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Copied and verified" in result.stdout

    pg = create_engine(PG)
    with pg.connect() as conn:
        assert conn.execute(text("SELECT site_id FROM users WHERE id = 2")).scalar() == 7
        rates = dict(conn.execute(text("SELECT id, wage_rate FROM attendance")).all())
        # Backfills ran over the real rows: linked days get the rate they were
        # paid at, the piece-rate day 0, the never-paid day stays NULL for the
        # reviewed reconcile.
        assert rates == {100: Decimal("600.00"), 101: Decimal("600.00"), 102: None, 103: Decimal("0.00")}
        sites = dict(conn.execute(text("SELECT id, site_id FROM transactions")).all())
        assert sites == {10: 7, 11: 7, 12: 7, 13: 7}
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()

    from app.core.migration_support import is_postgres  # noqa: F401  (importable)

    head = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"], capture_output=True, text=True, cwd=ROOT
    ).stdout.split()[0]
    assert version == head

    # Sequences moved past the copied ids: a new row does not collide.
    with pg.begin() as conn:
        new_id = conn.execute(text(
            "INSERT INTO transactions (worker_id, type, amount, date) VALUES (2, 'payment', 1, '2026-09-23') RETURNING id"
        )).scalar()
    assert new_id > 13


def test_a_dry_run_writes_nothing():
    _seed_source()
    pg = create_engine(PG)
    with pg.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "migrate_mysql_to_postgres.py"), "--source", MYSQL, "--target", PG],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Dry run" in result.stdout
    with pg.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'")).scalar() == 0
