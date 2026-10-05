"""The ledger matches the register after ANY sequence of edits.

The example-based tests check the cases somebody thought of. This one lets
Hypothesis generate the sequences nobody would: mark through the grid, correct
through the PATCH, rain the site out, raise the rate, unmark, re-price,
deactivate, re-scan, settle - in random order, on random days, for a crew of
three - and after EVERY step checks the books against an independent model of
what they must say:

  * every worker's attendance wages == sum over their days of
    round_half_up(frozen_rate x weight(status)), computed here, not by the app;
  * every worker's balance == that + any piece/manual wages - advances -
    payments, all tracked here;
  * `audit_wages` finds nothing wrong (apart from days waiting for a rate).

When Hypothesis finds a failing sequence it shrinks it to the shortest one
that still fails and prints it as a reproducible script.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule, run_state_machine_as_test
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from tests import conftest
from tests.helpers import Api, day, expected_day_wage, money

STATUSES = ["present", "half_day", "absent", "rain_out"]
# Four days, not a month: random steps keep landing on days that already
# exist, which is where every re-sync - and every bug in one - happens.
DAYS = st.integers(-3, 0)
RATES = ["450", "600", "733.33", "512.25", "0.01", None]
CENTS = Decimal("0.01")

_CTX: dict = {}


class LedgerMachine(RuleBasedStateMachine):
    """A crew of three on one site, and a model of what their books must say."""

    @initialize()
    def setup(self) -> None:
        _CTX["reset"]()
        session = _CTX["session_factory"]()
        self.api = Api(_CTX["client"], session)
        self.admin = self.api.admin()
        self.site = self.admin.site()
        self.workers = [
            self.admin.worker(f"Worker {i}", daily_wage=rate, site_id=self.site)
            for i, rate in enumerate(["600", "512.25", None])
        ]
        # The model.
        self.rate: dict[int, Optional[Decimal]] = {
            self.workers[0]: Decimal("600.00"),
            self.workers[1]: Decimal("512.25"),
            self.workers[2]: None,
        }
        self.active = {w: True for w in self.workers}
        self.last_day: dict[int, Optional[date]] = {w: None for w in self.workers}
        self.days: dict[tuple[int, date], tuple[str, Optional[Decimal]]] = {}
        self.advances = {w: Decimal("0.00") for w in self.workers}
        self.payments = {w: Decimal("0.00") for w in self.workers}

    def teardown(self) -> None:
        self.api.db.close()

    # --- model helpers -----------------------------------------------------

    def _snapshot(self, worker: int) -> Optional[Decimal]:
        rate = self.rate[worker]
        return rate if rate is not None and rate > 0 else None

    def _markable(self, worker: int, on: date) -> bool:
        return self.active[worker] or (
            self.last_day[worker] is not None and on <= self.last_day[worker]
        )

    def _expected_wages(self, worker: int) -> Decimal:
        return money(
            sum(
                (
                    expected_day_wage(status, rate)
                    for (w, _), (status, rate) in self.days.items()
                    if w == worker
                ),
                Decimal(0),
            )
        )

    # --- rules: marking -----------------------------------------------------

    @rule(w=st.integers(0, 2), offset=DAYS, status=st.sampled_from(STATUSES))
    def mark_single(self, w: int, offset: int, status: str) -> None:
        worker, on = self.workers[w], day(offset)
        response = self.admin.mark(worker, on, status)
        if (worker, on) in self.days:
            assert response.status_code == 409, response.text
        elif not self._markable(worker, on):
            assert response.status_code == 422, response.text
        else:
            assert response.status_code == 201, response.text
            self.days[(worker, on)] = (status, self._snapshot(worker))

    @rule(
        offset=DAYS,
        statuses=st.lists(st.sampled_from(STATUSES), min_size=3, max_size=3),
        overwrite=st.booleans(),
    )
    def mark_grid(self, offset: int, statuses: list[str], overwrite: bool) -> None:
        on = day(offset)
        marks = [
            {"worker_id": w, "status": s}
            for w, s in zip(self.workers, statuses)
            if self.active[w] or (w, on) in self.days or self._markable(w, on)
        ]
        if not marks:
            return
        response = self.admin.mark_bulk(marks, on, site_id=self.site, overwrite=overwrite)
        assert response.status_code == 201, response.text
        for mark in marks:
            key = (mark["worker_id"], on)
            if key not in self.days:
                self.days[key] = (mark["status"], self._snapshot(mark["worker_id"]))
            elif overwrite:
                self.days[key] = (mark["status"], self.days[key][1])

    @rule(w=st.integers(0, 2), offset=DAYS, status=st.sampled_from(STATUSES))
    def scan(self, w: int, offset: int, status: str) -> None:
        worker, on = self.workers[w], day(offset)
        response = self.admin.scan(worker, on, status)
        if not self._markable(worker, on):
            assert response.status_code == 422, response.text
            return
        assert response.status_code == 200, response.text
        rate = self.days[(worker, on)][1] if (worker, on) in self.days else self._snapshot(worker)
        self.days[(worker, on)] = (status, rate)

    @rule(offset=DAYS, overwrite=st.booleans())
    def rain_out(self, offset: int, overwrite: bool) -> None:
        on = day(offset)
        crew = [w for w in self.workers if self.active[w]]
        response = self.admin.rain_out(self.site, on, overwrite=overwrite)
        if not crew:
            assert response.status_code == 409, response.text
            return
        assert response.status_code == 200, response.text
        for worker in crew:
            key = (worker, on)
            if key not in self.days:
                self.days[key] = ("rain_out", self._snapshot(worker))
            elif overwrite:
                self.days[key] = ("rain_out", self.days[key][1])

    # --- rules: changing a marked day ----------------------------------------

    @rule(w=st.integers(0, 2), offset=DAYS, status=st.sampled_from(STATUSES))
    def correct(self, w: int, offset: int, status: str) -> None:
        worker, on = self.workers[w], day(offset)
        row = self.api.attendance(worker, on)
        if row is None:
            return
        response = self.admin.correct(row.id, status=status)
        assert response.status_code == 200, response.text
        self.days[(worker, on)] = (status, self.days[(worker, on)][1])

    @rule(w=st.integers(0, 2), offset=DAYS, to=DAYS)
    def move(self, w: int, offset: int, to: int) -> None:
        worker, on, target = self.workers[w], day(offset), day(to)
        row = self.api.attendance(worker, on)
        if row is None or on == target:
            return
        response = self.admin.correct(row.id, date=target)
        # Validity before uniqueness: a date the worker may not be marked for
        # is refused as such (422) even when it is also already taken.
        if not self._markable(worker, target):
            assert response.status_code == 422, response.text
        elif (worker, target) in self.days:
            assert response.status_code == 409, response.text
        else:
            assert response.status_code == 200, response.text
            self.days[(worker, target)] = self.days.pop((worker, on))

    @rule(w=st.integers(0, 2), offset=DAYS)
    def unmark(self, w: int, offset: int) -> None:
        worker, on = self.workers[w], day(offset)
        row = self.api.attendance(worker, on)
        if row is None:
            return
        response = self.admin.unmark(row.id)
        assert response.status_code == 204, response.text
        del self.days[(worker, on)]

    # --- rules: the worker ----------------------------------------------------

    @rule(w=st.integers(0, 2), rate=st.sampled_from(RATES), reprice=st.booleans())
    def change_rate(self, w: int, rate: Optional[str], reprice: bool) -> None:
        worker = self.workers[w]
        new = Decimal(rate).quantize(CENTS) if rate is not None else None
        positive = new is not None and new > 0
        response = self.admin.replace_worker(
            worker, daily_wage=rate, reprice_existing=reprice and positive
        )
        assert response.status_code == 200, response.text
        self.rate[worker] = new
        if positive:
            for key, (status, snap) in list(self.days.items()):
                if key[0] != worker:
                    continue
                if snap is None or reprice:
                    self.days[key] = (status, new)

    @rule(w=st.integers(0, 2), last=DAYS)
    def deactivate(self, w: int, last: int) -> None:
        worker = self.workers[w]
        response = self.admin.deactivate(worker, last_day=day(last))
        assert response.status_code == 204, response.text
        if self.active[worker]:
            self.active[worker] = False
            self.last_day[worker] = day(last)

    @rule(w=st.integers(0, 2))
    def reactivate(self, w: int) -> None:
        worker = self.workers[w]
        response = self.admin.patch(f"/contractor/workers/{worker}", json={"is_active": True})
        assert response.status_code == 200, response.text
        self.active[worker] = True
        self.last_day[worker] = None

    # --- rules: money ---------------------------------------------------------

    @rule(w=st.integers(0, 2), amount=st.integers(1, 2000), offset=DAYS)
    def advance(self, w: int, amount: int, offset: int) -> None:
        worker = self.workers[w]
        response = self.admin.advance(worker, amount, day(offset))
        assert response.status_code == 201, response.text
        self.advances[worker] += Decimal(amount)

    @rule(w=st.integers(0, 2))
    def settle(self, w: int) -> None:
        worker = self.workers[w]
        owed = self._expected_wages(worker) - self.advances[worker] - self.payments[worker]
        response = self.admin.settle(worker)
        if owed > 0:
            assert response.status_code == 201, response.text
            assert Decimal(response.json()["transaction"]["amount"]) == owed
            self.payments[worker] += owed
        else:
            assert response.status_code == 409, response.text

    # --- the invariant ----------------------------------------------------------

    @invariant()
    def books_match_the_model(self) -> None:
        if not hasattr(self, "workers"):
            return
        self.api.assert_ledger_matches_register(*self.workers)
        for worker in self.workers:
            expected = self._expected_wages(worker)
            assert self.api.wages(worker) == expected
            balance = self.admin.balance(worker)
            assert balance["total_wages"] == expected
            assert balance["total_advances"] == self.advances[worker]
            assert balance["total_payments"] == self.payments[worker]
            assert balance["balance"] == expected - self.advances[worker] - self.payments[worker]


@pytest.mark.slow
def test_any_sequence_of_edits_keeps_the_ledger_equal_to_the_register(server_engine, frozen_now):
    if server_engine is None:
        engine = conftest._build_engine()

        @event.listens_for(engine, "connect")
        def _fks(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        def reset() -> None:
            Base.metadata.drop_all(engine)
            Base.metadata.create_all(engine)
    else:
        engine = server_engine

        def reset() -> None:
            conftest._truncate(engine)

    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def _get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _get_db
    _CTX.update(reset=reset, session_factory=session_factory, client=TestClient(app))
    try:
        run_state_machine_as_test(
            LedgerMachine,
            settings=settings(
                max_examples=int(os.environ.get("LEDGER_EXAMPLES", "100")),
                stateful_step_count=30,
                deadline=None,
                suppress_health_check=list(HealthCheck),
                print_blob=True,
            ),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        if server_engine is None:
            engine.dispose()
