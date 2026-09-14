"""End-to-end check of DELETE /contractor/attendance/{id}.

Run from the project root:

    python scripts/smoke_attendance_delete.py

Builds its OWN in-memory SQLite engine and overrides `get_db`, so it never
touches the configured MySQL server. Do not import `app.database.engine`:
`Settings.connect_args` injects PyMySQL's `ssl` dict whenever DATABASE_URL is
set and `sqlite3.connect()` rejects it.

What it asserts:

  1. A day marked by mistake is removed, and the date frees up again - the
     UNIQUE on (worker_id, date) no longer blocks re-marking it, which is the
     whole point: the wrong worker was tapped and the right one has to go in.
  2. Deleting a day that took a deduction instalment hands the instalment
     back, and the plan's balance_remaining goes up by exactly that much.
  3. A day that paid piece-rate wages is refused with a 409 naming the log,
     rather than leaving money paid for a day nobody is recorded as working.
  4. Another contractor's attendance row answers 404, never 403.
  5. A supervisor may delete a day on the site they run, because they are the
     one who marked it - and is refused on a site they do not run.
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
from app.core.security import hash_password
from app.models.deduction import AutoDeduction, DeductionInstallment
from app.models.enums import UserRole
from app.models.user import User

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
yesterday = (today - timedelta(days=1)).isoformat()
two_days_ago = (today - timedelta(days=2)).isoformat()
three_days_ago = (today - timedelta(days=3)).isoformat()


def money(value) -> Decimal:
    return Decimal(str(value))


def register(name: str, phone: str) -> dict:
    r = client.post(
        "/auth/register",
        json={"name": name, "phone": phone, "password": "secret123", "role": "admin"},
    )
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


auth = register("Ramesh Builders", "9990001111")


def add_worker(headers: dict, name: str, phone: str, wage_type: str = "daily") -> int:
    body = {"name": name, "phone": phone, "daily_wage": "600.00", "wage_type": wage_type}
    r = client.post("/contractor/workers", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def mark(headers: dict, worker_id: int, when: str, status: str = "present") -> int:
    r = client.post(
        "/contractor/attendance",
        headers=headers,
        json={"worker_id": worker_id, "date": when, "status": status},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# 1. The wrong name was tapped - the row goes, and the day frees up
# ---------------------------------------------------------------------------

wrong = add_worker(auth, "Mohan Lal", "9990002222")
right = add_worker(auth, "Suresh Kumar", "9990003333")

row = mark(auth, wrong, yesterday)

# Re-marking the same worker for the same day is refused while the row exists,
# which is exactly the wall the contractor hits with no delete route.
r = client.post(
    "/contractor/attendance",
    headers=auth,
    json={"worker_id": wrong, "date": yesterday, "status": "absent"},
)
assert r.status_code == 409, r.text

r = client.delete(f"/contractor/attendance/{row}", headers=auth)
assert r.status_code == 204, r.text

with Session() as db:
    assert db.get(Attendance, row) is None

# The date is free again, for this worker and for the one who was actually there.
again = mark(auth, wrong, yesterday, "absent")
mark(auth, right, yesterday, "present")
print("1. a mistaken mark is removed and the day frees up - OK")

# A row that is not there is a 404 rather than a 500. Deliberately NOT the id
# just deleted: SQLite hands a reclaimed rowid straight back to the next
# insert, so `row` is now the id of the row re-marked above.
r = client.delete("/contractor/attendance/99999", headers=auth)
assert r.status_code == 404, r.text

# Tidy up, so the days below start from a clean slate.
r = client.delete(f"/contractor/attendance/{again}", headers=auth)
assert r.status_code == 204, r.text


# ---------------------------------------------------------------------------
# 2. A deleted day hands back the instalment it took
# ---------------------------------------------------------------------------

borrower = add_worker(auth, "Kishan Yadav", "9990004444")

r = client.post(
    "/contractor/transactions",
    headers=auth,
    json={
        "worker_id": borrower,
        "type": "advance",
        "amount": "1000.00",
        "date": three_days_ago,
        "payment_mode": "cash",
        "auto_deduct": True,
        "daily_deduction": "100.00",
    },
)
assert r.status_code == 201, r.text

with Session() as db:
    plan = db.scalar(select(AutoDeduction).where(AutoDeduction.worker_id == borrower))
    assert plan is not None, "the advance did not open a deduction plan"
    opening = money(plan.balance_remaining)
    plan_id = plan.id

# A present day takes one instalment off the plan. Marked through the bulk
# route rather than the single one because that is where the deduction is
# applied - and it is the route the grid actually posts.
r = client.post(
    "/contractor/attendance/bulk",
    headers=auth,
    json={
        "date": two_days_ago,
        "marks": [{"worker_id": borrower, "status": "present"}],
    },
)
assert r.status_code == 201, r.text
day = r.json()["attendance"][0]["id"]

with Session() as db:
    plan = db.get(AutoDeduction, plan_id)
    after_mark = money(plan.balance_remaining)
    taken = db.scalar(
        select(func.count())
        .select_from(DeductionInstallment)
        .where(
            DeductionInstallment.worker_id == borrower,
            DeductionInstallment.date == date.fromisoformat(two_days_ago),
        )
    )
    assert taken == 1, taken
    assert after_mark == opening - money("100.00"), (opening, after_mark)

# Deleting the day gives it straight back.
r = client.delete(f"/contractor/attendance/{day}", headers=auth)
assert r.status_code == 204, r.text

with Session() as db:
    plan = db.get(AutoDeduction, plan_id)
    assert money(plan.balance_remaining) == opening, (opening, plan.balance_remaining)
    left = db.scalar(
        select(func.count())
        .select_from(DeductionInstallment)
        .where(
            DeductionInstallment.worker_id == borrower,
            DeductionInstallment.date == date.fromisoformat(two_days_ago),
        )
    )
    assert left == 0, left
print("2. deleting a deducted day hands the instalment back - OK")


# ---------------------------------------------------------------------------
# 3. A day that paid piece-rate wages is refused, not silently unpaid
# ---------------------------------------------------------------------------

tiler = add_worker(auth, "Anil Sharma", "9990005555", wage_type="piece_rate")

r = client.post(
    "/contractor/piece-rate-logs",
    headers=auth,
    json={
        "worker_id": tiler,
        "work_description": "Tiling, first floor",
        "quantity": "120.500",
        "rate": "35.00",
        "date": two_days_ago,
    },
)
assert r.status_code == 201, r.text

paid_day = mark(auth, tiler, two_days_ago, "present")
r = client.delete(f"/contractor/attendance/{paid_day}", headers=auth)
assert r.status_code == 409, r.text
assert "piece-rate" in r.json()["detail"], r.text

# And the row is still there - a refusal must not half-delete.
with Session() as db:
    assert db.get(Attendance, paid_day) is not None
print("3. a piece-rate day is refused with a 409 naming the log - OK")


# ---------------------------------------------------------------------------
# 4. Another contractor's row answers 404, never 403
# ---------------------------------------------------------------------------

other = register("Verma Constructions", "9995550000")
theirs = add_worker(other, "Dinesh Patel", "9995551111")
their_day = mark(other, theirs, yesterday)

r = client.delete(f"/contractor/attendance/{their_day}", headers=auth)
assert r.status_code == 404, r.text

# Still intact for its actual owner.
r = client.delete(f"/contractor/attendance/{their_day}", headers=other)
assert r.status_code == 204, r.text
print("4. another contractor's day answers 404, never 403 - OK")


# ---------------------------------------------------------------------------
# 5. A supervisor may fix the site they run, and only that site
# ---------------------------------------------------------------------------

r = client.post("/contractor/sites", headers=auth, json={"name": "Udaipur Villa"})
assert r.status_code == 201, r.text
run_site = r.json()["id"]

r = client.post("/contractor/sites", headers=auth, json={"name": "Jaipur Block B"})
assert r.status_code == 201, r.text
other_site = r.json()["id"]

# Built straight against the session: `POST /contractor/workers` always writes
# role=WORKER (see `add_worker`), so there is no HTTP route that mints a
# supervisor, and this is setup rather than the thing under test.
with Session() as db:
    boss = db.scalar(select(User).where(User.phone == "9990001111"))
    supervisor = User(
        name="Vikram Singh",
        phone="9990006666",
        password_hash=hash_password("secret123"),
        role=UserRole.SUPERVISOR,
        daily_wage=Decimal("900.00"),
        contractor_id=boss.id,
    )
    db.add(supervisor)
    db.commit()
    supervisor_id = supervisor.id

r = client.patch(
    f"/contractor/sites/{run_site}", headers=auth, json={"supervisor_id": supervisor_id}
)
assert r.status_code == 200, r.text

r = client.post("/auth/login", json={"phone": "9990006666", "password": "secret123"})
assert r.status_code == 200, r.text
sup_auth = {"Authorization": "Bearer " + r.json()["access_token"]}

# A worker posted to the site the supervisor runs.
on_site = add_worker(auth, "Ravi Kumar", "9990007777")
r = client.patch(
    f"/contractor/workers/{on_site}", headers=auth, json={"site_id": run_site}
)
assert r.status_code == 200, r.text

# ...and one posted elsewhere.
elsewhere = add_worker(auth, "Sunil Das", "9990008888")
r = client.patch(
    f"/contractor/workers/{elsewhere}", headers=auth, json={"site_id": other_site}
)
assert r.status_code == 200, r.text

mine = mark(auth, on_site, yesterday)
not_mine = mark(auth, elsewhere, yesterday)

r = client.delete(f"/contractor/attendance/{not_mine}", headers=sup_auth)
assert r.status_code == 404, r.text

r = client.delete(f"/contractor/attendance/{mine}", headers=sup_auth)
assert r.status_code == 204, r.text
print("5. a supervisor may fix their own site's day, and only that one - OK")

print("\nAll attendance-delete checks passed.")
