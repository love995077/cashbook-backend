"""Copy every row from the old MySQL database into a fresh PostgreSQL one.

    python scripts/migrate_mysql_to_postgres.py \\
        --source "mysql+pymysql://user:pass@host:port/db" [--source-ssl] \\
        --target "postgresql+psycopg://user:pass@host:port/db" \\
        [--apply]

Without --apply it connects to both, checks them, and prints what it would
copy - it writes nothing.

HOW, AND WHY THIS WAY
---------------------
1. The source's Alembic revision is read. The TARGET is built - by the real
   migration chain - to that SAME revision, so both have exactly the same
   tables and columns and rows can be copied as they are.
2. Every table is copied in foreign-key order, ids included, in one target
   transaction. users <-> sites reference each other, so users go in without
   `site_id`, sites follow, and `site_id` is filled in afterwards.
3. Each id sequence is moved past the highest id copied, or the first new row
   the app writes would collide with an old one.
4. The target is upgraded to head. The newer migrations' backfills - the rate
   each day was paid at, the site each wage belongs to - then run over the
   real data, exactly as they would have on MySQL.
5. Row counts and money totals are compared table by table, and any
   difference fails the run loudly. The copy itself (step 2) is one
   transaction - an error part-way leaves no rows behind - and the target is
   only ever a database built for this, so a failed run is re-run from empty.

The source is only ever READ. Nothing here can change the old database.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Never let the app's own .env choose a database for this script.
os.environ.setdefault("DATABASE_URL", "sqlite://")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import Boolean, MetaData, create_engine, func, select, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from app.config import connect_args_for, normalise_database_url  # noqa: E402

#: Money columns whose totals must match exactly after the copy.
MONEY = {
    "transactions": ["amount"],
    "cash_entries": ["amount"],
    "petty_cash": ["amount"],
    "piece_rate_logs": ["total_amount"],
    "auto_deductions": ["principal_amount", "balance_remaining"],
    "deduction_installments": ["amount"],
    "advance_requests": ["amount"],
    "invoices": ["total_amount"],
    "machinery_logs": ["fuel_cost"],
    "users": ["daily_wage"],
}


def _alembic(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = url
    return config


def _revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        try:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:
            return None


def _tables(engine: Engine) -> list:
    """The target's tables, as reflected, in the app's foreign-key order.

    Ordered by the MODELS rather than by the reflection: reflection sees the
    users <-> sites cycle as two plain foreign keys and may put sites first,
    while the models mark `users.site_id` as the deferred side (use_alter), so
    their order is users, then sites - and `site_id` is filled in afterwards.
    """
    from app import models  # noqa: F401  (registers every table)
    from app.database import Base

    meta = MetaData()
    meta.reflect(bind=engine)
    order = {table.name: index for index, table in enumerate(Base.metadata.sorted_tables)}
    tables = [t for t in meta.tables.values() if t.name != "alembic_version"]
    return sorted(tables, key=lambda t: order.get(t.name, len(order)))


def _count(conn, table: str) -> int:
    return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0


def _sum(conn, table: str, column: str) -> Decimal:
    return Decimal(conn.execute(text(f"SELECT COALESCE(SUM({column}), 0) FROM {table}")).scalar())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="MySQL URL to copy from (read only)")
    parser.add_argument("--source-ssl", action="store_true", help="connect to the source over TLS")
    parser.add_argument("--source-ssl-ca", default="", help="CA certificate for the source")
    parser.add_argument("--target", required=True, help="empty PostgreSQL URL to copy into")
    parser.add_argument("--apply", action="store_true", help="actually copy (default: dry run)")
    args = parser.parse_args()

    source_url = normalise_database_url(args.source)
    target_url = normalise_database_url(args.target)
    source = create_engine(
        source_url,
        connect_args=connect_args_for(source_url, args.source_ssl, args.source_ssl_ca, managed=args.source_ssl),
    )
    target = create_engine(target_url, connect_args=connect_args_for(target_url))

    revision = _revision(source)
    if revision is None:
        print("The source has no alembic_version - refusing to guess its schema.")
        return 2
    print(f"source revision: {revision}")

    existing = _revision(target)
    if existing is not None:
        with target.connect() as conn:
            occupied = [
                t.name for t in _tables(target) if _count(conn, t.name) > 0
            ]
        if occupied:
            print(f"The target already holds data ({', '.join(occupied)}). Refusing to merge.")
            return 2
        print(f"target is empty at revision {existing}; it will be rebuilt to {revision}")

    with source.connect() as conn:
        meta = MetaData()
        meta.reflect(bind=conn)
        counts = {
            name: _count(conn, name) for name in meta.tables if name != "alembic_version"
        }
    for name, n in sorted(counts.items()):
        print(f"  {name:28} {n:>8} rows")

    if not args.apply:
        print("\nDry run: nothing written. Re-run with --apply to copy.")
        return 0

    # --- 1. the target, at the source's revision -------------------------------
    config = _alembic(target_url)
    with target.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(config, revision)

    # --- 2. every row, in one transaction --------------------------------------
    tables = _tables(target)
    source_meta = MetaData()
    source_meta.reflect(bind=source)

    with source.connect() as src, target.begin() as dst:
        deferred_site: list[dict] = []
        for table in tables:
            if table.name not in source_meta.tables:
                print(f"  {table.name:28} not in source, left empty")
                continue
            source_table = source_meta.tables[table.name]
            shared = [c.name for c in table.columns if c.name in source_table.columns]
            rows = [dict(row._mapping) for row in src.execute(select(*[source_table.c[c] for c in shared]))]
            booleans = [c.name for c in table.columns if isinstance(c.type, Boolean) and c.name in shared]
            for row in rows:
                for column in booleans:
                    if row[column] is not None:
                        row[column] = bool(row[column])
                for key, value in list(row.items()):
                    # Enums arrive as their label; keep only the string.
                    row[key] = getattr(value, "value", value)
            if table.name == "users":
                for row in rows:
                    if row.get("site_id") is not None:
                        deferred_site.append({"id": row["id"], "site_id": row["site_id"]})
                        row["site_id"] = None
            if rows:
                dst.execute(table.insert(), rows)
            print(f"  {table.name:28} {len(rows):>8} rows copied")

        if deferred_site:
            users = next(t for t in tables if t.name == "users")
            for item in deferred_site:
                dst.execute(users.update().where(users.c.id == item["id"]).values(site_id=item["site_id"]))

        # --- 3. sequences past the highest id ---------------------------------
        for table in tables:
            if "id" in table.columns:
                dst.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                        f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table.name}"
                    )
                )

    # --- 4. the newer migrations, over the real data ---------------------------
    command.upgrade(config, "head")

    # --- 5. nothing lost --------------------------------------------------------
    failures = []
    with source.connect() as src, target.connect() as dst:
        for name in counts:
            if name not in [t.name for t in tables]:
                continue
            if _count(src, name) != _count(dst, name):
                failures.append(f"{name}: {_count(src, name)} rows in source, {_count(dst, name)} in target")
            for column in MONEY.get(name, []):
                a, b = _sum(src, name, column), _sum(dst, name, column)
                if a != b:
                    failures.append(f"{name}.{column}: {a} in source, {b} in target")

    if failures:
        print("\nVERIFICATION FAILED:")
        for line in failures:
            print("  " + line)
        return 1
    print(f"\nCopied and verified: every row count and money total matches. Target is at head.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
