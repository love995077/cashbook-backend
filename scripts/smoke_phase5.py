"""End-to-end check of the Phase 5 cash box, analytics and exports.

Run from the project root:

    python scripts/smoke_phase5.py

Everything runs against an in-memory SQLite database through FastAPI's
TestClient, so it never touches the configured MySQL server and leaves nothing
behind. Needs `httpx` (TestClient depends on it); the rest is already in
requirements.txt.

It registers a contractor, adds a worker, moves some money, then asserts the
numbers that come back out of /contractor/summary, /contractor/analytics and
every export. If a change to the balance arithmetic or the export scoping
breaks something, this says so in one run.
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
yesterday = (today - timedelta(days=1)).isoformat()

r = client.post(
    "/auth/register",
    json={"name": "Ramesh Builders", "phone": "9990001111", "password": "secret123", "role": "admin"},
)
assert r.status_code == 201, r.text
token = r.json()["access_token"]
auth = {"Authorization": "Bearer " + token}

r = client.post(
    "/contractor/workers",
    headers=auth,
    json={"name": "Suresh Kumar", "phone": "9990002222", "password": "secret123", "daily_wage": "600.00"},
)
assert r.status_code == 201, r.text
worker_id = r.json()["id"]

for kind, amount, day in (("wage", "600.00", yesterday), ("advance", "200.00", iso), ("payment", "150.00", iso)):
    r = client.post(
        "/contractor/transactions",
        headers=auth,
        json={"worker_id": worker_id, "type": kind, "amount": amount, "date": day},
    )
    assert r.status_code == 201, r.text

r = client.get("/contractor/categories", headers=auth)
assert r.status_code == 200, r.text
categories = r.json()
print("seeded categories:", [c["name"] for c in categories])
by_name = {c["name"]: c for c in categories}
assert by_name["Site Advance"]["direction"] == "in"

r = client.post(
    "/contractor/categories",
    headers=auth,
    json={"name": "Scaffolding", "direction": "out", "icon": "layers-outline", "color": "#7C5CFF"},
)
assert r.status_code == 201, r.text
scaffolding = r.json()["id"]

r = client.post("/contractor/categories", headers=auth, json={"name": "Fuel", "direction": "out"})
assert r.status_code == 409, r.text

entries = [
    {"category_id": by_name["Site Advance"]["id"], "amount": "50000.00", "date": yesterday, "note": "First tranche"},
    {"category_id": by_name["Fuel"]["id"], "amount": "1200.00", "date": iso},
    {"category_id": by_name["Materials"]["id"], "amount": "8000.00", "date": iso, "note": "40 bags cement"},
    {"category_id": scaffolding, "amount": "3000.00", "date": iso, "client_ref": "abc-123"},
]
for payload in entries:
    r = client.post("/contractor/cash-entries", headers=auth, json=payload)
    assert r.status_code == 201, r.text

# Replaying a queued write must not create a second row.
r = client.post(
    "/contractor/cash-entries",
    headers=auth,
    json={"category_id": scaffolding, "amount": "3000.00", "date": iso, "client_ref": "abc-123"},
)
assert r.status_code == 201, r.text
replayed = r.json()

r = client.get("/contractor/cash-entries", headers=auth)
listed = r.json()
assert len(listed) == 4, [e["id"] for e in listed]
assert sum(1 for e in listed if e["id"] == replayed["id"]) == 1
print("entries:", [(e["category_name"], e["amount"], e["direction"]) for e in listed])

r = client.get("/contractor/summary", headers=auth)
summary = r.json()
print("summary:", summary)
assert summary["cash_in"] == "50000.00"
assert summary["wages_paid"] == "350.00"       # advance 200 + payment 150
assert summary["expenses_paid"] == "12200.00"  # 1200 + 8000 + 3000
assert summary["total_out"] == "12550.00"
assert summary["balance"] == "37450.00"
assert summary["wages_earned"] == "600.00"
assert summary["outstanding_payable"] == "250.00"  # 600 - 200 - 150
assert summary["worker_count"] == 1

for period in ("day", "week", "month"):
    r = client.get("/contractor/analytics", headers=auth, params={"period": period})
    series = r.json()
    assert r.status_code == 200, r.text
    hits = [b for b in series["buckets"] if b["total_out"] != "0.00" or b["cash_in"] != "0.00"]
    print(period, "buckets:", len(series["buckets"]), "with activity:", [(b["label"], b["total_out"], b["cash_in"]) for b in hits])
    assert series["totals"]["balance"] == "37450.00"

print("by_category:", [(c["name"], c["total"]) for c in series["by_category"]])

# A used category is retired, not deleted; an unused one is removed outright.
r = client.delete("/contractor/categories/" + str(scaffolding), headers=auth)
assert r.status_code == 204, r.text
r = client.get("/contractor/categories", headers=auth)
assert "Scaffolding" not in [c["name"] for c in r.json()]
r = client.get("/contractor/categories", headers=auth, params={"include_inactive": True})
assert "Scaffolding" in [c["name"] for c in r.json()]
r = client.get("/contractor/cash-entries", headers=auth)
assert len(r.json()) == 4, "retiring a category must not drop its entries"

print("ALL CASH CHECKS PASSED")

# --- exports ---------------------------------------------------------------

r = client.get("/api/reports/transactions/csv", headers=auth)
assert r.status_code == 200, r.text
assert r.headers["content-type"].startswith("text/csv")
body = r.content.decode("utf-8-sig")
print("transactions.csv:")
print(body.strip())
assert body.splitlines()[0].startswith("date,worker,phone")
assert r.content.startswith(b"\xef\xbb\xbf"), "Excel needs the BOM"

r = client.get("/api/reports/attendance/csv", headers=auth)
assert r.status_code == 200, r.text
print("attendance.csv header:", r.content.decode("utf-8-sig").splitlines()[0])

r = client.get("/api/reports/cash-entries/csv", headers=auth)
assert r.status_code == 200, r.text
print("cash-entries.csv:")
print(r.content.decode("utf-8-sig").strip())

r = client.get("/api/reports/business/pdf", headers=auth)
assert r.status_code == 200, r.text
assert r.content.startswith(b"%PDF-"), r.content[:40]
print("business report:", len(r.content), "bytes")

r = client.get("/api/reports/worker/" + str(worker_id) + "/pdf", headers=auth)
assert r.status_code == 200 and r.content.startswith(b"%PDF-"), r.status_code
print("worker statement still fine:", len(r.content), "bytes")

# A worker may export only themselves, whatever worker_id they pass.
r = client.post("/auth/login", json={"phone": "9990002222", "password": "secret123"})
worker_auth = {"Authorization": "Bearer " + r.json()["access_token"]}
r = client.get("/api/reports/transactions/csv", headers=worker_auth, params={"worker_id": 999})
assert r.status_code == 200, r.text
lines = r.content.decode("utf-8-sig").strip().splitlines()
assert all("Suresh Kumar" in line for line in lines[1:]), lines
r = client.get("/api/reports/cash-entries/csv", headers=worker_auth)
assert r.status_code == 403, r.status_code
r = client.get("/api/reports/business/pdf", headers=worker_auth)
assert r.status_code == 403, r.status_code

print("ALL EXPORT CHECKS PASSED")
