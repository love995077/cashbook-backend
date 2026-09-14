"""Run the API against a throwaway SQLite file, seeded with a demo crew.

    python scripts/dev_server_sqlite.py

For driving the app in a browser (`npx expo start --web`) without pointing it
at the configured MySQL. `.env` holds a live Aiven URL; nothing here should
ever reach it.

Same rule as every smoke script: build the engine HERE and override `get_db`.
Importing `app.database.engine` would pick up `Settings.connect_args`, which
injects PyMySQL's `ssl` dict whenever DATABASE_URL is set, and
`sqlite3.connect()` rejects it.

The database is a file rather than `sqlite://` in memory so the data survives
a reload, and it is recreated on every start so each session begins from the
same known crew. Sign in with 9990001111 / secret123.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date, timedelta
from decimal import Decimal

import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (registers the tables)
from app.core.security import hash_password
from app.database import Base, get_db
from app.main import app
from app.models.attendance import Attendance
from app.models.cash import CashCategory, CashEntry
from app.models.enums import (
    AttendanceStatus,
    CashDirection,
    PaymentMode,
    TransactionType,
    UserRole,
    WageType,
)
from app.models.site import Site
from app.models.transaction import Transaction
from app.models.user import User

DB_PATH = Path(__file__).resolve().parent.parent / "dev_browser_test.sqlite3"
if DB_PATH.exists():
    DB_PATH.unlink()

# NOT StaticPool. That shares ONE sqlite3 connection across every request, and
# the dashboard opens several in parallel on load - concurrent use of a single
# connection surfaces as "sqlite3.InterfaceError: bad parameter or other API
# misuse", which looks exactly like an application bug and is not one. The
# default pool hands each thread its own connection, which a file-backed
# database supports. (The in-memory smoke scripts need StaticPool for the
# opposite reason: a second connection would see a second, empty database.)
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)


def _db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _db

today = date.today()


def seed() -> None:
    with Session() as db:
        boss = User(
            name="Ramesh Builders",
            phone="9990001111",
            password_hash=hash_password("secret123"),
            role=UserRole.ADMIN,
        )
        db.add(boss)
        db.flush()

        villa = Site(name="Udaipur Villa", address="Sector 4", contractor_id=boss.id)
        block = Site(name="Jaipur Block B", address="Ring Road", contractor_id=boss.id)
        db.add_all([villa, block])
        db.flush()

        crew = [
            ("Suresh Kumar", "9990002222", "mason", "600.00", villa.id, WageType.DAILY),
            ("Mohan Lal", "9990003333", "carpenter", "700.00", villa.id, WageType.DAILY),
            ("Kishan Yadav", "9990004444", "helper", "450.00", block.id, WageType.DAILY),
            # Free-text post the old enum could not express.
            ("Vikram Singh", "9990005555", "shuttering_carpenter", "800.00", block.id, WageType.DAILY),
            ("Anil Sharma", "9990006666", "tile_layer", "0.00", villa.id, WageType.PIECE_RATE),
            # No phone, no PIN - the "only a name is required" case.
            ("Ravi (new)", None, None, None, None, WageType.DAILY),
        ]
        workers = []
        for name, phone, trade, wage, site_id, wage_type in crew:
            worker = User(
                name=name,
                phone=phone,
                password_hash=hash_password("secret123") if phone else None,
                role=UserRole.WORKER,
                trade=trade,
                daily_wage=Decimal(wage) if wage else None,
                wage_type=wage_type,
                site_id=site_id,
                contractor_id=boss.id,
            )
            db.add(worker)
            workers.append(worker)
        db.flush()

        # Cash categories, so the entry form has chips to pick.
        for name, direction in [
            ("Materials", CashDirection.OUT),
            ("Fuel", CashDirection.OUT),
            ("Scaffolding", CashDirection.OUT),
            ("Site Advance", CashDirection.IN),
            ("Client Payment", CashDirection.IN),
        ]:
            db.add(
                CashCategory(name=name, direction=direction, contractor_id=boss.id)
            )

        # Some money in, so the balance is not zero and the cards have figures.
        db.add(
            CashEntry(
                contractor_id=boss.id,
                site_id=villa.id,
                direction=CashDirection.IN,
                amount=Decimal("50000.00"),
                date=today - timedelta(days=5),
                note="Opening site advance",
            )
        )
        db.add(
            CashEntry(
                contractor_id=boss.id,
                site_id=villa.id,
                direction=CashDirection.OUT,
                amount=Decimal("8000.00"),
                date=today - timedelta(days=3),
                note="40 bags cement",
            )
        )

        # A couple of wages and an advance, so a worker has a balance.
        db.add(
            Transaction(
                worker_id=workers[0].id,
                type=TransactionType.WAGE,
                amount=Decimal("600.00"),
                date=today - timedelta(days=2),
            )
        )
        db.add(
            Transaction(
                worker_id=workers[0].id,
                type=TransactionType.ADVANCE,
                amount=Decimal("1000.00"),
                date=today - timedelta(days=2),
                payment_mode=PaymentMode.CASH,
            )
        )

        # One day already marked, a few days back - so the grid has a locked
        # row to correct and to unmark without having to create one first.
        db.add(
            Attendance(
                worker_id=workers[1].id,
                date=today - timedelta(days=1),
                site_id=villa.id,
                status=AttendanceStatus.ABSENT,
            )
        )

        db.commit()


seed()

print(f"SQLite dev database: {DB_PATH}")
print("Sign in as 9990001111 / secret123")
print("Serving on http://localhost:8000 - the web build points here by default.")

uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
