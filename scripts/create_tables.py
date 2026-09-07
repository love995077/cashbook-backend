"""Create the cash_book schema and every table declared on Base.

Run from the project root:

    python scripts/create_tables.py

Safe to re-run: CREATE DATABASE / create_all both skip what already exists.
Note that create_all does NOT alter existing tables — if a model gains a
column later, that column has to be added by hand (see README).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import Base, engine, init_db  # noqa: E402


def main() -> int:
    settings = get_settings()
    print(f"Target: {settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}")

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - surface the driver's own message
        print(f"\nFailed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    declared = set(Base.metadata.tables)
    present = set(inspect(engine).get_table_names())

    print("\nTables in the database:")
    for name in sorted(present):
        marker = "declared" if name in declared else "not declared by any model"
        print(f"  {name:<14} ({marker})")

    missing = declared - present
    if missing:
        print(f"\nStill missing: {', '.join(sorted(missing))}", file=sys.stderr)
        return 1

    print(f"\nOK - all {len(declared)} declared tables exist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
