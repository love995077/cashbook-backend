"""Driving the app the way the phone does, and reading the books back.

Everything that WRITES goes through the HTTP API with a real bearer token, so
the tests exercise validation, auth, routing and the transaction boundary
exactly as a request from the app would. Assertions read the database
directly, through a session of their own, so they see what was committed and
not what a response chose to say.

The oracle (`expected_day_wage`) is written out longhand here rather than
imported from app/services/wages.py. A test that computed the expected answer
with the code under test would agree with any bug in it.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.attendance import Attendance
from app.models.enums import TransactionType
from app.models.transaction import Transaction
from app.models.user import User

TODAY = date(2026, 9, 15)
CENTS = Decimal("0.01")

_phones = itertools.count(9_000_000_001)


def day(offset: int) -> date:
    """TODAY shifted by `offset` days. day(0) is today, day(-1) yesterday."""
    return TODAY + timedelta(days=offset)


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENTS)


def expected_day_wage(status: str, rate: Optional[Decimal]) -> Decimal:
    """What one day earns, computed independently of the app."""
    share = {"present": Decimal("1"), "half_day": Decimal("0.5")}.get(status)
    if share is None or rate is None or Decimal(rate) <= 0:
        return Decimal("0.00")
    return (Decimal(rate) * share).quantize(CENTS, rounding=ROUND_HALF_UP)


def ok(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, (
        f"expected {status}, got {response.status_code}: {response.text}"
    )
    return response.json() if response.content else None


class Api:
    def __init__(self, client: TestClient, db: Session) -> None:
        self.client = client
        self.db = db

    def admin(self, name: str = "Ramesh Contractor") -> "Admin":
        phone = str(next(_phones))
        body = ok(
            self.client.post(
                "/auth/register",
                json={"name": name, "phone": phone, "password": "secret12", "role": "admin"},
            ),
            201,
        )
        return Admin(self, body["access_token"], body["user"]["id"])

    # --- reading the books back ------------------------------------------

    def fresh(self) -> Session:
        """End this session's transaction, so the next read sees the latest commit.

        Expiring the identity map is not enough on MySQL: its default
        isolation is REPEATABLE READ, so a transaction keeps reading the
        snapshot it started with and never sees what the API committed since.
        Ending the transaction is what makes the next read current on every
        engine (and expires everything as a side effect).
        """
        self.db.rollback()
        return self.db

    def attendance(self, worker_id: int, on: date) -> Optional[Attendance]:
        return self.fresh().scalar(
            select(Attendance).where(Attendance.worker_id == worker_id, Attendance.date == on)
        )

    def wages(self, worker_id: int) -> Decimal:
        total = self.fresh().scalar(
            select(func.sum(Transaction.amount)).where(
                Transaction.worker_id == worker_id, Transaction.type == TransactionType.WAGE
            )
        )
        return money(total or 0)

    def attendance_wages(self, worker_id: int) -> Decimal:
        """WAGE money that hangs off attendance rows, and only that."""
        total = self.fresh().scalar(
            select(func.sum(Transaction.amount))
            .join(Attendance, Attendance.transaction_id == Transaction.id)
            .where(Attendance.worker_id == worker_id)
        )
        return money(total or 0)

    def oracle_attendance_wages(self, worker_id: int) -> Decimal:
        """Sum over the register of rate x weight, worked out independently."""
        rows = self.fresh().scalars(
            select(Attendance).where(Attendance.worker_id == worker_id)
        ).all()
        return money(sum((expected_day_wage(r.status.value, r.wage_rate) for r in rows), Decimal(0)))

    def assert_ledger_matches_register(self, *worker_ids: int) -> None:
        """The invariant, checked two independent ways.

        1. Every attendance row's linked wage is exactly rate x weight, on the
           row's date, and a non-earning row has none (`audit_wages`, which
           reads row by row).
        2. Per worker, the money hanging off attendance equals the register
           summed by this file's own arithmetic.
        """
        from app.services.integrity import audit_wages

        issues = [i for i in audit_wages(self.fresh()) if i.kind != "unpriced"]
        assert issues == [], f"ledger does not match register: {issues}"
        for worker_id in worker_ids:
            assert self.attendance_wages(worker_id) == self.oracle_attendance_wages(worker_id)

    def worker_row(self, worker_id: int) -> User:
        return self.fresh().get(User, worker_id)


class Admin:
    def __init__(self, api: Api, token: str, user_id: int) -> None:
        self.api = api
        self.client = api.client
        self.headers = {"Authorization": f"Bearer {token}"}
        self.id = user_id

    # --- raw verbs ------------------------------------------------------------
    def get(self, url: str, **kw) -> Response:
        return self.client.get(url, headers=self.headers, **kw)

    def post(self, url: str, **kw) -> Response:
        return self.client.post(url, headers=self.headers, **kw)

    def patch(self, url: str, **kw) -> Response:
        return self.client.patch(url, headers=self.headers, **kw)

    def put(self, url: str, **kw) -> Response:
        return self.client.put(url, headers=self.headers, **kw)

    def delete(self, url: str, **kw) -> Response:
        return self.client.delete(url, headers=self.headers, **kw)

    # --- crew and sites --------------------------------------------------------
    def site(self, name: str = "Udaipur Villa") -> int:
        return ok(self.post("/contractor/sites", json={"name": name}), 201)["id"]

    def worker(
        self,
        name: str = "Suresh",
        daily_wage: Any = "600.00",
        wage_type: str = "daily",
        site_id: Optional[int] = None,
    ) -> int:
        body: dict[str, Any] = {"name": name, "wage_type": wage_type}
        if daily_wage is not None:
            body["daily_wage"] = str(daily_wage)
        if site_id is not None:
            body["site_id"] = site_id
        return ok(self.post("/contractor/workers", json=body), 201)["id"]

    def replace_worker(self, worker_id: int, **fields: Any) -> Response:
        current = self.api.worker_row(worker_id)
        body: dict[str, Any] = {
            "name": current.name,
            "daily_wage": str(current.daily_wage) if current.daily_wage is not None else None,
            "trade": current.trade,
            "wage_type": current.wage_type.value,
            "site_id": current.site_id or 0,
            "is_active": current.is_active,
        }
        body.update(fields)
        if "daily_wage" in fields and fields["daily_wage"] is not None:
            body["daily_wage"] = str(fields["daily_wage"])
        if isinstance(body.get("reprice_from"), date):
            body["reprice_from"] = body["reprice_from"].isoformat()
        return self.put(f"/contractor/workers/{worker_id}", json=body)

    def deactivate(self, worker_id: int, last_day: Optional[date] = None) -> Response:
        params = {"last_day": last_day.isoformat()} if last_day else {}
        return self.delete(f"/contractor/workers/{worker_id}", params=params)

    # --- attendance -------------------------------------------------------------
    def mark(self, worker_id: int, on: date, status: str = "present", **extra: Any) -> Response:
        body = {"worker_id": worker_id, "date": on.isoformat(), "status": status, **extra}
        return self.post("/contractor/attendance", json=body)

    def mark_bulk(
        self,
        marks: list[dict[str, Any]],
        on: date,
        site_id: Optional[int] = None,
        overwrite: bool = False,
    ) -> Response:
        body: dict[str, Any] = {"marks": marks, "date": on.isoformat(), "overwrite": overwrite}
        if site_id is not None:
            body["site_id"] = site_id
        return self.post("/contractor/attendance/bulk", json=body)

    def correct(self, attendance_id: int, **fields: Any) -> Response:
        body = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in fields.items()}
        return self.patch(f"/contractor/attendance/{attendance_id}", json=body)

    def unmark(self, attendance_id: int) -> Response:
        return self.delete(f"/contractor/attendance/{attendance_id}")

    def scan(self, worker_id: int, on: Optional[date] = None, status: str = "present") -> Response:
        body: dict[str, Any] = {"worker_id": worker_id, "status": status}
        if on is not None:
            body["date"] = on.isoformat()
        return self.post("/contractor/attendance/scan", json=body)

    def rain_out(self, site_id: int, on: date, overwrite: bool = True) -> Response:
        return self.post(
            f"/contractor/sites/{site_id}/rainout",
            params={"date": on.isoformat(), "overwrite": str(overwrite).lower()},
        )

    # --- money -------------------------------------------------------------------
    def transaction(
        self, worker_id: int, type_: str, amount: Any, on: date, **extra: Any
    ) -> Response:
        body = {
            "worker_id": worker_id,
            "type": type_,
            "amount": str(amount),
            "date": on.isoformat(),
            **extra,
        }
        return self.post("/contractor/transactions", json=body)

    def advance(self, worker_id: int, amount: Any, on: date, **extra: Any) -> Response:
        return self.transaction(worker_id, "advance", amount, on, **extra)

    def payment(self, worker_id: int, amount: Any, on: date, **extra: Any) -> Response:
        return self.transaction(worker_id, "payment", amount, on, **extra)

    def ledger(self, worker_id: int) -> dict:
        return ok(self.get(f"/contractor/workers/{worker_id}/ledger"))

    def balance(self, worker_id: int) -> dict[str, Decimal]:
        raw = self.ledger(worker_id)["balance"]
        return {key: money(value) for key, value in raw.items()}

    def preview(self, worker_id: int, **params: Any) -> dict:
        query = {"worker_id": worker_id}
        query.update({k: (v.isoformat() if isinstance(v, date) else v) for k, v in params.items()})
        return ok(self.get("/payroll/preview", params=query))

    def settle(self, worker_id: int, **fields: Any) -> Response:
        body: dict[str, Any] = {"worker_id": worker_id}
        body.update({k: (v.isoformat() if isinstance(v, date) else v) for k, v in fields.items()})
        return self.post("/payroll/settle", json=body)

    def summary(self, **params: Any) -> dict:
        return ok(self.get("/contractor/summary", params=params))

    # --- integrity ----------------------------------------------------------
    def audit(self) -> dict:
        return ok(self.get("/contractor/integrity/wages"))

    def reconcile(self, apply: bool = False) -> dict:
        return ok(
            self.post(
                "/contractor/integrity/wages/reconcile",
                params={"apply": str(apply).lower()},
            )
        )

    def attendance_id(self, worker_id: int, on: date) -> int:
        row = self.api.attendance(worker_id, on)
        assert row is not None, f"no attendance for worker {worker_id} on {on}"
        return row.id
