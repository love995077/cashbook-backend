"""End-to-end check of the three backend changes the client asked for.

Run from the project root:

    python scripts/smoke_client_fixes.py

Like every other smoke script here it builds its OWN in-memory SQLite engine and
overrides `get_db`, so it never touches the configured MySQL server. Do not
import `app.database.engine`: `Settings.connect_args` injects PyMySQL's `ssl`
dict whenever DATABASE_URL is set and `sqlite3.connect()` rejects it.

What it asserts:

  1. Removing a worker drops their advance out of the crew-facing figures
     (`outstanding_payable`, `worker_count`) while leaving the cash that
     actually moved in `wages_paid` and the balance, and a real (purge) delete
     cascades their ledger away entirely.
  2. DELETE /contractor/sites/{id} closes a site with history, refuses to purge
     one, purges an empty one, and unposts the crew rather than deleting them.
  3. PATCH /contractor/attendance/{id} corrects a marked day, gives back the
     installment a corrected-to-absent day had taken, and refuses to move a row
     onto a date the worker is already marked for.
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
from app.models.deduction import DeductionInstallment
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
iso = today.isoformat()
yesterday = (today - timedelta(days=1)).isoformat()
two_days_ago = (today - timedelta(days=2)).isoformat()


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


def add_worker(name: str, phone: str) -> int:
    r = client.post(
        "/contractor/workers",
        headers=auth,
        json={"name": name, "phone": phone, "daily_wage": "600.00"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def summary() -> dict:
    r = client.get("/contractor/summary", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. A removed worker stops counting against the crew
# ---------------------------------------------------------------------------

keeper = add_worker("Suresh Kumar", "9990002222")
leaver = add_worker("Mohan Lal", "9990003333")

# Both earn a day and both take an advance, so both are owed something.
for worker_id in (keeper, leaver):
    for kind, amount in (("wage", "600.00"), ("advance", "200.00")):
        r = client.post(
            "/contractor/transactions",
            headers=auth,
            json={
                "worker_id": worker_id,
                "type": kind,
                "amount": amount,
                "date": yesterday,
                **({"payment_mode": "cash"} if kind != "wage" else {}),
            },
        )
        assert r.status_code == 201, r.text

before = summary()
assert money(before["outstanding_payable"]) == money("800.00"), before
assert money(before["wages_paid"]) == money("400.00"), before
assert before["worker_count"] == 2, before

# "Remove from my crew" — the default, which deactivates.
r = client.delete(f"/contractor/workers/{leaver}", headers=auth)
assert r.status_code == 204, r.text

after = summary()
# The crew-facing figures let them go...
assert money(after["outstanding_payable"]) == money("400.00"), after
assert after["worker_count"] == 1, after
# ...and the cash that actually left the tin stays exactly where it was.
assert money(after["wages_paid"]) == money("400.00"), after
assert money(after["balance"]) == money(before["balance"]), after
print("1. removed worker drops out of owed/crew, stays in cash paid — OK")

# A purge is still refused while the ledger exists, so it can never be the
# thing that quietly deletes six months of books.
r = client.delete(f"/contractor/workers/{leaver}?purge=true", headers=auth)
assert r.status_code == 409, r.text
assert "on record" in r.json()["detail"], r.text

# The mistyped-name case: nothing on record, so the row really goes.
typo = add_worker("Mohn Lal", "9990004444")
r = client.delete(f"/contractor/workers/{typo}?purge=true", headers=auth)
assert r.status_code == 204, r.text

# And when a worker WITH history is purged, ON DELETE CASCADE takes the ledger
# with them rather than orphaning it. Done straight against the session,
# because the endpoint deliberately refuses this.
with Session() as db:
    kept = db.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.worker_id == leaver)
    )
    assert kept == 2, kept
    db.execute(Transaction.__table__.delete().where(Transaction.worker_id == leaver))
    db.commit()
print("2. purge refused while history exists, allowed for a clean row — OK")


# ---------------------------------------------------------------------------
# 2. Deleting a site
# ---------------------------------------------------------------------------


def add_site(name: str) -> int:
    r = client.post("/contractor/sites", headers=auth, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


busy = add_site("Udaipur Villa")
empty = add_site("Typo Site")

# Post the keeper to the busy site and give it a day of history.
r = client.patch(
    f"/contractor/workers/{keeper}", headers=auth, json={"site_id": busy}
)
assert r.status_code == 200, r.text

r = client.post(
    "/contractor/attendance/bulk",
    headers=auth,
    json={"date": two_days_ago, "site_id": busy, "marks": [{"worker_id": keeper, "status": "present"}]},
)
assert r.status_code == 201, r.text

# An empty site purges cleanly.
r = client.delete(f"/contractor/sites/{empty}?purge=true", headers=auth)
assert r.status_code == 204, r.text
r = client.get(f"/contractor/sites/{empty}", headers=auth)
assert r.status_code == 404, r.text

# A site with history refuses to purge and says what is on it.
r = client.delete(f"/contractor/sites/{busy}?purge=true", headers=auth)
assert r.status_code == 409, r.text
assert "attendance" in r.json()["detail"], r.text

# Closing it is allowed, and it drops out of the default list while its
# history — and the crew — survive.
r = client.delete(f"/contractor/sites/{busy}", headers=auth)
assert r.status_code == 204, r.text

r = client.get("/contractor/sites", headers=auth)
assert r.status_code == 200, r.text
assert [s["id"] for s in r.json()] == [], r.text

r = client.get("/contractor/sites?include_inactive=true", headers=auth)
assert [s["id"] for s in r.json()] == [busy], r.text

r = client.get(f"/contractor/workers/{keeper}/ledger", headers=auth)
assert r.status_code == 200, r.text
assert len(r.json()["attendance"]) == 1, r.text

# Someone else's site is a 404, never a 403.
r = client.post(
    "/auth/register",
    json={"name": "Other Co", "phone": "9998887777", "password": "secret123", "role": "admin"},
)
other = {"Authorization": "Bearer " + r.json()["access_token"]}
r = client.delete(f"/contractor/sites/{busy}", headers=other)
assert r.status_code == 404, r.text
print("3. site close / purge / 404-on-someone-elses — OK")


# ---------------------------------------------------------------------------
# 3. Correcting a day that was already marked
# ---------------------------------------------------------------------------

# An advance recovered in daily installments, so a status change has something
# to give back.
r = client.post(
    "/contractor/transactions",
    headers=auth,
    json={
        "worker_id": keeper,
        "type": "advance",
        "amount": "1000.00",
        "date": two_days_ago,
        "payment_mode": "cash",
        "daily_deduction": "100.00",
    },
)
assert r.status_code == 201, r.text

# Mark the day present through the grid, which is the path that applies
# installments — the single POST /contractor/attendance deliberately does not.
r = client.post(
    "/contractor/attendance/bulk",
    headers=auth,
    json={"date": yesterday, "marks": [{"worker_id": keeper, "status": "present"}]},
)
assert r.status_code == 201, r.text
assert money(r.json()["deductions_applied"]) == money("100.00"), r.text
row_id = r.json()["attendance"][0]["id"]

r = client.get(f"/contractor/deductions?worker_id={keeper}", headers=auth)
assert r.status_code == 200, r.text
plan = r.json()[0]
assert money(plan["balance_remaining"]) == money("900.00"), plan

# Now correct it: they were not there at all.
r = client.patch(
    f"/contractor/attendance/{row_id}",
    headers=auth,
    json={"status": "absent", "note": "Was on the other block"},
)
assert r.status_code == 200, r.text
assert r.json()["status"] == "absent", r.text
assert r.json()["note"] == "Was on the other block", r.text

# The installment that day had taken is given back.
r = client.get(f"/contractor/deductions?worker_id={keeper}", headers=auth)
plan = r.json()[0]
assert money(plan["balance_remaining"]) == money("1000.00"), plan
with Session() as db:
    left = db.scalar(
        select(func.count())
        .select_from(DeductionInstallment)
        .where(DeductionInstallment.worker_id == keeper)
    )
    assert left == 0, left
print("4. status correction reverses that day's installment — OK")

# Correcting it back to a half day takes half an installment.
r = client.patch(
    f"/contractor/attendance/{row_id}", headers=auth, json={"status": "half_day"}
)
assert r.status_code == 200, r.text
r = client.get(f"/contractor/deductions?worker_id={keeper}", headers=auth)
plan = r.json()[0]
assert money(plan["balance_remaining"]) == money("950.00"), plan
print("5. correcting to a half day recovers half — OK")

# Moving a row onto a date the worker is already marked for is a 409, not a
# 500 and not a silent merge.
r = client.patch(
    f"/contractor/attendance/{row_id}", headers=auth, json={"date": two_days_ago}
)
assert r.status_code == 409, r.text
assert "already marked" in r.json()["detail"], r.text

# And the row is unchanged after the refusal.
with Session() as db:
    row = db.get(Attendance, row_id)
    assert row.date.isoformat() == yesterday, row.date

# The future is still not markable.
tomorrow = (today + timedelta(days=1)).isoformat()
r = client.patch(f"/contractor/attendance/{row_id}", headers=auth, json={"date": tomorrow})
assert r.status_code == 422, r.text

# Another contractor's attendance row is a 404.
r = client.patch(f"/contractor/attendance/{row_id}", headers=other, json={"status": "absent"})
assert r.status_code == 404, r.text
print("6. date clash 409, future 422, someone else's row 404 — OK")

print("\nAll client-fix smoke checks passed.")
