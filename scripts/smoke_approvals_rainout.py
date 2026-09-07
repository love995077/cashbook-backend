"""End-to-end check of the two contractor actions that are one tap away from
being irreversible: approving an advance request, and marking a site rained out.

Run from the project root:

    python scripts/smoke_approvals_rainout.py

Own in-memory SQLite engine + a get_db override, like scripts/smoke_phase5.py,
so it never touches the configured MySQL server.

WHAT IT PINS
------------
Both screens were reported as "nothing happens" on the web build. The cause was
client-side (react-native-web's Alert.alert is an empty method, so the confirm
never appeared and the handler never ran), but that made the payloads worth
proving rather than assuming:

  * the approval sends exactly {status, payment_mode} — no site_id, no
    approved_amount — and the server writes the ADVANCE transaction in the same
    call, which is why an approval cannot be replayed;
  * a second decision on the same request answers 409, so a double tap cannot
    pay a worker twice;
  * the rain-out takes site_id from the PATH and the day from the QUERY, and
    the `rain_out` status is the server's to write for the whole crew — the
    client sends no per-worker payload at all.
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

r = client.post(
    "/auth/register",
    json={
        "name": "Ramesh Builders",
        "phone": "9990001111",
        "password": "secret123",
        "role": "admin",  # UserRole.CONTRACTOR is an alias of ADMIN
    },
)
assert r.status_code == 201, r.text
auth = {"Authorization": "Bearer " + r.json()["access_token"]}

r = client.post(
    "/contractor/sites", headers=auth, json={"name": "Tower B", "address": "MG Road"}
)
assert r.status_code == 201, r.text
site_id = r.json()["id"]

crew = []
for n, name in enumerate(["Suresh Kumar", "Anil Yadav", "Farid Khan"]):
    r = client.post(
        "/contractor/workers",
        headers=auth,
        json={
            "name": name,
            "phone": f"999000222{n}",
            "password": "secret123",
            "daily_wage": "600.00",
            "site_id": site_id,
        },
    )
    assert r.status_code == 201, r.text
    crew.append(r.json()["id"])

r = client.post("/auth/login", json={"phone": "9990002220", "password": "secret123"})
assert r.status_code == 200, r.text
worker_auth = {"Authorization": "Bearer " + r.json()["access_token"]}

# --- 1. Approving an advance request ----------------------------------------

r = client.post(
    "/worker/advance-requests",
    headers=worker_auth,
    json={"amount": "2000.00", "reason": "School fees"},
)
assert r.status_code == 201, r.text
request_id = r.json()["id"]

r = client.get("/contractor/advance-requests?status=pending", headers=auth)
assert r.status_code == 200, r.text
assert [row["id"] for row in r.json()] == [request_id], r.json()

# Exactly what the Approve button sends: the segmented control's mode, and
# nothing else. No site_id is involved anywhere in this route.
r = client.patch(
    f"/contractor/advance-requests/{request_id}",
    headers=auth,
    json={"status": "approved", "payment_mode": "upi"},
)
assert r.status_code == 200, r.text
decided = r.json()
assert decided["status"] == "approved", decided
assert decided["transaction_id"] is not None, decided
print("approve          ->", decided["status"], "txn", decided["transaction_id"])

# The money is in the ledger, not just a status flag.
r = client.get(f"/contractor/workers/{crew[0]}/ledger", headers=auth)
assert r.status_code == 200, r.text
advances = [row for row in r.json()["transactions"] if row["type"] == "advance"]
assert len(advances) == 1 and advances[0]["amount"] == "2000.00", advances
assert advances[0]["payment_mode"] == "upi", advances[0]
print("ledger           ->", advances[0]["amount"], advances[0]["payment_mode"])

# A second tap must not pay twice.
r = client.patch(
    f"/contractor/advance-requests/{request_id}",
    headers=auth,
    json={"status": "approved", "payment_mode": "upi"},
)
assert r.status_code == 409, r.text
print("double approve   -> 409", r.json()["detail"])

# The queue the screen shows after a decision no longer holds it.
r = client.get("/contractor/advance-requests?status=pending", headers=auth)
assert r.json() == [], r.json()

# Declining sends payment_mode: null, which the schema must accept.
r = client.post(
    "/worker/advance-requests", headers=worker_auth, json={"amount": "500.00"}
)
assert r.status_code == 201, r.text
r = client.patch(
    f"/contractor/advance-requests/{r.json()['id']}",
    headers=auth,
    json={"status": "rejected", "payment_mode": None},
)
assert r.status_code == 200, r.text
assert r.json()["status"] == "rejected", r.json()
assert r.json()["transaction_id"] is None, r.json()
print("decline (null mode) ->", r.json()["status"])

# --- 2. Marking the site rained out -----------------------------------------

# One worker already marked present, to prove the overwrite arithmetic the
# confirmation promises ("anyone already marked present will be changed").
r = client.post(
    "/contractor/attendance",
    headers=auth,
    json={"worker_id": crew[1], "date": iso, "status": "present"},
)
assert r.status_code == 201, r.text

# site_id in the path, the day in the query, no body at all.
r = client.post(f"/contractor/sites/{site_id}/rainout?date={iso}", headers=auth)
assert r.status_code == 200, r.text
result = r.json()
assert result["created"] == 2, result
assert result["updated"] == 1, result
assert result["unchanged"] == 0, result
print("rain-out         ->", result)

r = client.get(f"/contractor/sites/{site_id}/attendance?date={iso}", headers=auth)
assert r.status_code == 200, r.text
marks = {row["worker_id"]: row["status"] for row in r.json()}
assert set(marks) == set(crew), marks
assert set(marks.values()) == {"rain_out"}, marks
print("crew after       ->", marks)

# Running it again is idempotent — the button can be pressed twice.
r = client.post(f"/contractor/sites/{site_id}/rainout?date={iso}", headers=auth)
assert r.status_code == 200, r.text
again = r.json()
assert (again["created"], again["updated"], again["unchanged"]) == (0, 0, 3), again
print("rain-out again   ->", again)

print("\napprovals + rain-out OK")
