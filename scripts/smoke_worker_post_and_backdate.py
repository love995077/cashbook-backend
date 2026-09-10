"""End-to-end check of free-text worker posts, optional worker fields and
back-dated attendance.

Run from the project root:

    python scripts/smoke_worker_post_and_backdate.py

Everything runs against an in-memory SQLite database through FastAPI's
TestClient, so it never touches the configured MySQL server and leaves nothing
behind. Needs `httpx` (TestClient depends on it).

Covers the three things that changed:

  1. `trade` is free text, normalised on the way in, so "Shuttering Carpenter"
     is accepted and stored as `shuttering_carpenter` - and the well-known
     posts still land on exactly the values the old enum used.
  2. `name` is the only required field on a worker. A worker booked with no
     phone and no PIN is created, and cannot sign in.
  3. Attendance accepts a back-dated day, and the rows land on the date sent
     rather than on today.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers the tables)
from app.database import Base, get_db
from app.models import Attendance

# Rebinds the name `app` from the package to the FastAPI instance, so every
# import from the package has to happen above this line.
from app.main import app

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
last_week = (today - timedelta(days=7)).isoformat()

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


# -------------------------------------------------- 1. free-text worker post

# A post the old enum did not have. This is the whole point of the change.
r = client.post(
    "/contractor/workers",
    headers=auth,
    json={
        "name": "Imran Shaikh",
        "phone": "9990002222",
        "password": "secret123",
        "trade": "Shuttering Carpenter",
        "daily_wage": "700.00",
    },
)
assert r.status_code == 201, r.text
custom = r.json()
assert custom["trade"] == "shuttering_carpenter", custom["trade"]

# A well-known post typed by hand must land on the same value the suggestion
# chip sends, or the app's icon lookup misses it.
r = client.post(
    "/contractor/workers",
    headers=auth,
    json={"name": "Suresh Kumar", "phone": "9990003333", "trade": "Mason"},
)
assert r.status_code == 201, r.text
assert r.json()["trade"] == "mason", r.json()["trade"]

# Hyphens, extra whitespace and mixed case all fold to one spelling, so a
# breakdown by post still groups.
for typed in ("bar-bender", "  BAR   BENDER ", "Bar_Bender"):
    r = client.post(
        "/contractor/workers",
        headers=auth,
        json={"name": f"Bender {typed.strip()}", "trade": typed},
    )
    assert r.status_code == 201, r.text
    assert r.json()["trade"] == "bar_bender", (typed, r.json()["trade"])

# Blank is not a post, it is an unset one.
r = client.post(
    "/contractor/workers", headers=auth, json={"name": "No Post", "trade": "   "}
)
assert r.status_code == 201, r.text
assert r.json()["trade"] is None, r.json()["trade"]

# Still bounded: the column is VARCHAR(50) and the schema says so, so an
# over-long post is a 422 rather than a truncated row.
r = client.post(
    "/contractor/workers", headers=auth, json={"name": "Too Long", "trade": "x" * 51}
)
assert r.status_code == 422, r.text


# ------------------------------------------- 2. name is the only requirement

r = client.post("/contractor/workers", headers=auth, json={"name": "Kishan Lal"})
assert r.status_code == 201, r.text
bare = r.json()
assert bare["phone"] is None, bare
assert bare["trade"] is None, bare
assert bare["daily_wage"] is None, bare
assert bare["wage_type"] == "daily", bare
assert bare["is_active"] is True, bare

# A worker with no PIN has no digest, and must not be able to sign in. The
# empty string is the interesting attempt: hashing it on create would have made
# it a real, guessable password.
#
# 401 from the login route, or 422 from the schema's own minimum length - both
# are a refusal. What matters is that no attempt yields a token.
for attempt in ("", " ", "secret123"):
    r = client.post("/auth/login", json={"phone": "9990003333", "password": attempt})
    assert r.status_code in (401, 422), (attempt, r.status_code, r.text)
    assert "access_token" not in r.json(), (attempt, r.text)

# Name is still required - "everything optional" stops short of the one field
# that makes the row mean anything.
r = client.post("/contractor/workers", headers=auth, json={"trade": "mason"})
assert r.status_code == 422, r.text
r = client.post("/contractor/workers", headers=auth, json={"name": "   "})
assert r.status_code == 422, r.text

# A duplicate phone is still a 409 when one is actually given...
r = client.post(
    "/contractor/workers",
    headers=auth,
    json={"name": "Someone Else", "phone": "9990002222"},
)
assert r.status_code == 409, r.text

# ...but two workers with no phone at all do not collide, which is the thing
# the nullable UNIQUE index has to get right.
for name in ("Nameless One", "Nameless Two"):
    r = client.post("/contractor/workers", headers=auth, json={"name": name})
    assert r.status_code == 201, (name, r.text)


# ------------------------------------------------ 3. back-dated attendance

worker_id = custom["id"]

r = client.post(
    "/contractor/attendance/bulk",
    headers=auth,
    json={
        "date": last_week,
        "marks": [{"worker_id": worker_id, "status": "present", "shift": "day"}],
    },
)
assert r.status_code in (200, 201), r.text
result = r.json()
assert result["date"] == last_week, result["date"]
assert result["created"] == 1, result

# The row has to be ON that date, not merely reported as such. There is no
# GET /contractor/attendance to ask, so this reads the table directly - the
# point is where the row landed, and that is the most direct way to see it.
def _attendance_by_date(wid: int) -> dict[str, str]:
    with Session() as db:
        return {
            row.date.isoformat(): row.status.value
            for row in db.query(Attendance).filter(Attendance.worker_id == wid).all()
        }


assert set(_attendance_by_date(worker_id)) == {last_week}, _attendance_by_date(worker_id)

# Today is a different day, so the same worker can also be marked for it -
# the unique (worker_id, date) constraint is per day, and back-dating must not
# consume today's slot.
r = client.post(
    "/contractor/attendance/bulk",
    headers=auth,
    json={
        "date": iso,
        "marks": [{"worker_id": worker_id, "status": "half_day", "shift": "day"}],
    },
)
assert r.status_code in (200, 201), r.text
assert r.json()["created"] == 1, r.json()

by_date = _attendance_by_date(worker_id)
assert by_date == {last_week: "present", iso: "half_day"}, by_date

# Re-sending the back-dated day changes nothing without overwrite, so a retry
# after a dropped response is safe.
r = client.post(
    "/contractor/attendance/bulk",
    headers=auth,
    json={
        "date": last_week,
        "marks": [{"worker_id": worker_id, "status": "absent", "shift": "day"}],
    },
)
assert r.status_code in (200, 201), r.text
assert r.json()["unchanged"] == 1, r.json()
by_date = _attendance_by_date(worker_id)
assert by_date[last_week] == "present", by_date


# ------------------------------------------------------ reports still render

# The PDF statement and the CSV export both read `worker.phone`, which is now
# nullable. Exercised against the worker who has none.
r = client.get(f"/api/reports/worker/{bare['id']}/pdf", headers=auth)
assert r.status_code == 200, r.text
assert r.content[:4] == b"%PDF", r.content[:16]

r = client.get("/api/reports/transactions/csv", headers=auth)
assert r.status_code == 200, r.text
assert r.content.startswith(b"\xef\xbb\xbf"), "CSV lost its BOM"

print("smoke_worker_post_and_backdate: OK")
