"""End-to-end check that a day of attendance earns its wage.

Run from the project root:

    python scripts/smoke_attendance_wages.py

Builds its OWN in-memory SQLite engine and overrides `get_db`, so it never
touches the configured MySQL server. Do not import `app.database.engine`:
`Settings.connect_args` injects PyMySQL's `ssl` dict whenever DATABASE_URL is
set and `sqlite3.connect()` rejects it.

The bug this covers: marking a daily worker present used to write an
`attendance` row and nothing else, so `outstanding_payable` - the "still owed
to the crew" figure - reported zero for a crew that had worked all week.

What it asserts:

  1. Marking a daily worker present earns the day rate, and "still owed"
     moves by exactly that much. A half day earns half.
  2. Re-submitting the same grid does NOT pay twice.
  3. Correcting a day to absent takes the wage back; correcting it to a half
     day halves it; correcting back to present restores it.
  4. Unmarking the day removes the wage entirely.
  5. A piece-rate worker is paid by their log and does NOT also earn a day
     rate, and a worker with no day rate set earns nothing rather than
     crashing.
  6. A rain-out over a day already marked present takes that wage back.
  7. The wage cannot be deleted directly through the transactions route - the
     day has to be corrected instead.
  8. `net_payable` on the settlement screen now reflects days worked.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers the tables)
from app.database import Base, get_db
from app.main import app
from app.models.attendance import Attendance
from app.models.enums import TransactionType
from app.models.transaction import Transaction

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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
client = TestClient(app)

today = date.today()
d1 = (today - timedelta(days=1)).isoformat()
d2 = (today - timedelta(days=2)).isoformat()
d3 = (today - timedelta(days=3)).isoformat()


def money(value) -> Decimal:
    return Decimal(str(value))


r = client.post(
    "/auth/register",
    json={
        "name": "Ramesh Builders",
        "phone": "9990001111",
        "password": "secret123",
        "role": "admin",
    },
)
assert r.status_code == 201, r.text
auth = {"Authorization": "Bearer " + r.json()["access_token"]}


def add_worker(name, phone, wage="600.00", wage_type="daily") -> int:
    body = {"name": name, "phone": phone, "wage_type": wage_type}
    if wage is not None:
        body["daily_wage"] = wage
    r = client.post("/contractor/workers", headers=auth, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def owed() -> Decimal:
    r = client.get("/contractor/summary", headers=auth)
    assert r.status_code == 200, r.text
    return money(r.json()["outstanding_payable"])


def wages_of(worker_id: int) -> Decimal:
    with Session() as db:
        total = db.scalar(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.worker_id == worker_id,
                Transaction.type == TransactionType.WAGE,
            )
        )
    return money(total)


def bulk(marks, when, overwrite=False) -> dict:
    r = client.post(
        "/contractor/attendance/bulk",
        headers=auth,
        json={"date": when, "marks": marks, "overwrite": overwrite},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. A marked day earns the day rate
# ---------------------------------------------------------------------------

mason = add_worker("Suresh Kumar", "9990002222", "600.00")
helper = add_worker("Kishan Yadav", "9990003333", "450.00")

assert owed() == money("0.00"), "a crew that has not worked is owed nothing"

result = bulk(
    [
        {"worker_id": mason, "status": "present"},
        {"worker_id": helper, "status": "half_day"},
    ],
    d3,
)
assert result["created"] == 2, result

# 600 for the full day + 225 for the half.
assert wages_of(mason) == money("600.00"), wages_of(mason)
assert wages_of(helper) == money("225.00"), wages_of(helper)
assert owed() == money("825.00"), owed()
print("1. a marked day earns the day rate, a half day earns half - OK")


# ---------------------------------------------------------------------------
# 2. Re-submitting the same grid does not pay twice
# ---------------------------------------------------------------------------

again = bulk(
    [
        {"worker_id": mason, "status": "present"},
        {"worker_id": helper, "status": "half_day"},
    ],
    d3,
)
assert again["unchanged"] == 2, again
assert owed() == money("825.00"), owed()

# And with overwrite, which takes the edit branch rather than the skip branch.
bulk([{"worker_id": mason, "status": "present"}], d3, overwrite=True)
assert wages_of(mason) == money("600.00"), wages_of(mason)
assert owed() == money("825.00"), owed()
print("2. re-submitting the same day does not pay twice - OK")


# ---------------------------------------------------------------------------
# 3. Correcting the day moves the wage with it
# ---------------------------------------------------------------------------

with Session() as db:
    row = db.scalar(
        select(Attendance).where(
            Attendance.worker_id == mason, Attendance.date == date.fromisoformat(d3)
        )
    )
    mason_day = row.id
    assert row.transaction_id is not None, "a present day should be linked to its wage"

r = client.patch(
    f"/contractor/attendance/{mason_day}", headers=auth, json={"status": "absent"}
)
assert r.status_code == 200, r.text
assert wages_of(mason) == money("0.00"), "an absent day earns nothing"
assert owed() == money("225.00"), owed()

r = client.patch(
    f"/contractor/attendance/{mason_day}", headers=auth, json={"status": "half_day"}
)
assert r.status_code == 200, r.text
assert wages_of(mason) == money("300.00"), wages_of(mason)

r = client.patch(
    f"/contractor/attendance/{mason_day}", headers=auth, json={"status": "present"}
)
assert r.status_code == 200, r.text
assert wages_of(mason) == money("600.00"), wages_of(mason)
assert owed() == money("825.00"), owed()

# Moving the day to another date moves the wage's date too.
r = client.patch(
    f"/contractor/attendance/{mason_day}", headers=auth, json={"date": d2}
)
assert r.status_code == 200, r.text
with Session() as db:
    row = db.get(Attendance, mason_day)
    wage = db.get(Transaction, row.transaction_id)
    assert wage.date == date.fromisoformat(d2), wage.date
print("3. correcting a day moves its wage with it - OK")


# ---------------------------------------------------------------------------
# 4. Unmarking the day removes the wage
# ---------------------------------------------------------------------------

r = client.delete(f"/contractor/attendance/{mason_day}", headers=auth)
assert r.status_code == 204, r.text
assert wages_of(mason) == money("0.00"), "unmarking removes the wage"
assert owed() == money("225.00"), owed()

with Session() as db:
    orphans = db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.worker_id == mason, Transaction.type == TransactionType.WAGE)
    )
    assert orphans == 0, orphans
print("4. unmarking a day removes the wage it earned - OK")


# ---------------------------------------------------------------------------
# 5. Piece-rate and no-rate workers
# ---------------------------------------------------------------------------

tiler = add_worker("Anil Sharma", "9990004444", "800.00", wage_type="piece_rate")
nameless = add_worker("Ravi (new)", None, wage=None)

before = owed()
bulk(
    [
        {
            "worker_id": tiler,
            "status": "present",
            "quantity": "100.000",
            "rate": "35.00",
            "work_description": "Tiling",
        },
        {"worker_id": nameless, "status": "present"},
    ],
    d1,
)

# The tiler is paid for the tiles, once - 100 x 35 - and NOT a day rate too.
assert wages_of(tiler) == money("3500.00"), wages_of(tiler)
# Nobody with no rate set earns anything, and marking them did not blow up.
assert wages_of(nameless) == money("0.00"), wages_of(nameless)
assert owed() == before + money("3500.00"), owed()
print("5. piece-rate is paid once, a worker with no rate earns nothing - OK")


# ---------------------------------------------------------------------------
# 6. A rain-out takes back the day it cancelled
# ---------------------------------------------------------------------------

r = client.post("/contractor/sites", headers=auth, json={"name": "Udaipur Villa"})
assert r.status_code == 201, r.text
site = r.json()["id"]

roofer = add_worker("Mohan Lal", "9990005555", "700.00")
r = client.patch(
    f"/contractor/workers/{roofer}", headers=auth, json={"site_id": site}
)
assert r.status_code == 200, r.text

bulk([{"worker_id": roofer, "status": "present"}], d1)
assert wages_of(roofer) == money("700.00"), wages_of(roofer)

# `date` and `overwrite` are query parameters on this route, not a body.
r = client.post(
    f"/contractor/sites/{site}/rainout?date={d1}&overwrite=true",
    headers=auth,
)
assert r.status_code == 200, r.text
assert wages_of(roofer) == money("0.00"), "a rained-off day is not a paid day"
print("6. a rain-out takes back the wage of the day it cancelled - OK")


# ---------------------------------------------------------------------------
# 7. The wage cannot be deleted out from under the day
# ---------------------------------------------------------------------------

bulk([{"worker_id": helper, "status": "present"}], d1)
with Session() as db:
    row = db.scalar(
        select(Attendance).where(
            Attendance.worker_id == helper, Attendance.date == date.fromisoformat(d1)
        )
    )
    linked_wage = row.transaction_id
    assert linked_wage is not None

r = client.delete(f"/contractor/transactions/{linked_wage}", headers=auth)
assert r.status_code == 409, r.text
assert "attendance" in r.json()["detail"].lower(), r.text

with Session() as db:
    assert db.get(Transaction, linked_wage) is not None, "the refusal must not delete"
print("7. an attendance-backed wage is refused with a 409 - OK")


# ---------------------------------------------------------------------------
# 8. The settlement screen sees the days worked
# ---------------------------------------------------------------------------

r = client.get(f"/payroll/preview?worker_id={helper}", headers=auth)
assert r.status_code == 200, r.text
summary = r.json()
assert money(summary["total_wages"]) > money("0.00"), summary
assert money(summary["net_payable"]) > money("0.00"), summary
print(
    f"8. settlement sees {summary['days_worked']} days worked, "
    f"net payable {summary['net_payable']} - OK"
)

print("\nAll attendance-wage checks passed.")


# ---------------------------------------------------------------------------
# 9. PUT /contractor/workers/{id} - the correction route
# ---------------------------------------------------------------------------

r = client.put(
    f"/contractor/workers/{helper}",
    headers=auth,
    json={
        "name": "Kishan Yadav Jr",
        "daily_wage": "500.00",
        "trade": "Shuttering Carpenter",
        "wage_type": "daily",
        "site_id": site,
    },
)
assert r.status_code == 200, r.text
body = r.json()
assert body["name"] == "Kishan Yadav Jr", body
assert money(body["daily_wage"]) == money("500.00"), body
# Free text, normalised the way the server groups posts.
assert body["trade"] == "shuttering_carpenter", body
assert body["site_id"] == site, body

# Raising the rate does NOT reprice days already worked.
# 225 for the half day in section 1 plus 450 for the full day in section 7,
# both at the 450 rate they were worked at - not the 500 just set.
assert wages_of(helper) == money("675.00"), (
    "an old day must keep the rate it was worked at"
)

# An omitted field is cleared rather than left alone - that is what PUT means,
# and it is how the form clears a wage typed by mistake.
r = client.put(
    f"/contractor/workers/{helper}", headers=auth, json={"name": "Kishan Yadav"}
)
assert r.status_code == 200, r.text
assert r.json()["daily_wage"] is None, r.json()
assert r.json()["site_id"] is None, r.json()

# Another contractor's worker is a 404, never a 403.
r = client.post(
    "/auth/register",
    json={
        "name": "Verma Constructions",
        "phone": "9995550000",
        "password": "secret123",
        "role": "admin",
    },
)
assert r.status_code == 201, r.text
other = {"Authorization": "Bearer " + r.json()["access_token"]}
r = client.put(
    f"/contractor/workers/{helper}", headers=other, json={"name": "Hijacked"}
)
assert r.status_code == 404, r.text

# A name is the one thing a worker record cannot be without.
r = client.put(f"/contractor/workers/{helper}", headers=auth, json={})
assert r.status_code == 422, r.text
print("9. PUT replaces the whole record, keeps history, answers 404 - OK")

print("\nAll worker-edit checks passed.")
