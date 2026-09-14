"""Check DELETE /contractor/transactions/{id} - undoing a mistyped ledger entry.

Run from the project root:

    python scripts/smoke_transaction_delete.py

In-memory SQLite through TestClient, so it never touches the configured MySQL
server. Covers the case that sent a contractor's dashboard wrong in the field:
an advance typed against a worker who was then removed from the crew, leaving
the cash balance down by money nobody had actually handed over.

Asserts that deleting the entry clears it from the summary, that another
contractor cannot reach it (404, not 403), and that an entry other rows were
built on is refused rather than silently corrupting them.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers the tables)
from app.database import Base, get_db
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

iso = date.today().isoformat()


def register(name, phone):
    r = client.post(
        "/auth/register",
        json={"name": name, "phone": phone, "password": "secret123", "role": "admin"},
    )
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def add_worker(auth, name, phone):
    r = client.post(
        "/contractor/workers",
        headers=auth,
        json={"name": name, "phone": phone, "password": "secret123", "daily_wage": "600.00"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def advance(auth, worker_id, amount):
    r = client.post(
        "/contractor/transactions",
        headers=auth,
        json={"worker_id": worker_id, "type": "advance", "amount": amount, "date": iso},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def summary(auth):
    r = client.get("/contractor/summary", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


auth = register("Ramesh Builders", "9990001111")
other = register("Rival Contractors", "9990009999")

worker_id = add_worker(auth, "Sinu", "9990002222")
keep_id = add_worker(auth, "Kapil", "9990003333")

# The field scenario: a real advance, plus one typed by mistake.
advance(auth, keep_id, "20000.00")
typo_id = advance(auth, worker_id, "800.00")

assert summary(auth)["wages_paid"] == "20800.00", summary(auth)
print("logged 20800.00 across two workers")

# Removing the worker must NOT drop the money - the cash left the tin.
r = client.delete(f"/contractor/workers/{worker_id}", headers=auth)
assert r.status_code == 204, r.text
after_removal = summary(auth)
assert after_removal["wages_paid"] == "20800.00", after_removal
assert after_removal["worker_count"] == 1, after_removal
assert after_removal["outstanding_payable"] == "0.00", after_removal
print("worker removed: wages_paid still 20800.00 across 1 active worker (correct)")

# Another contractor gets 404, not 403 - no confirming the row exists.
r = client.delete(f"/contractor/transactions/{typo_id}", headers=other)
assert r.status_code == 404, r.text
print("other contractor refused with 404")

# A transaction backed by an instalment plan is refused, not deleted.
r = client.post(
    "/contractor/transactions",
    headers=auth,
    json={
        "worker_id": keep_id,
        "type": "advance",
        "amount": "5000.00",
        "date": iso,
        "daily_deduction": "100.00",
    },
)
assert r.status_code == 201, r.text
planned_id = r.json()["id"]
r = client.delete(f"/contractor/transactions/{planned_id}", headers=auth)
assert r.status_code == 409, r.text
assert "instalment plan" in r.json()["detail"], r.json()
print("entry backed by an instalment plan refused with 409")

# The actual correction.
r = client.delete(f"/contractor/transactions/{typo_id}", headers=auth)
assert r.status_code == 204, r.text
r = client.delete(f"/contractor/transactions/{typo_id}", headers=auth)
assert r.status_code == 404, r.text

final = summary(auth)
assert final["wages_paid"] == "25000.00", final
assert final["worker_count"] == 1, final
print("typo deleted: wages_paid now 25000.00 (20000 real + 5000 planned)")

print("\nsmoke_transaction_delete OK")
