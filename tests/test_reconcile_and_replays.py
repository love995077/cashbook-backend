"""Putting old books right, and making sure a phone cannot write money twice."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.models.attendance import Attendance
from app.models.enums import AttendanceStatus, TransactionType
from app.models.transaction import Transaction
from tests.helpers import day, ok


def _legacy_day(db, worker_id: int, on: date, status: str) -> None:
    """A day marked before attendance earned wages: no rate, no wage."""
    db.add(
        Attendance(
            worker_id=worker_id,
            date=on,
            status=AttendanceStatus(status),
            wage_rate=None,
            transaction_id=None,
        )
    )
    db.commit()


def _hand_typed_wage(db, worker_id: int, on: date, amount: str) -> int:
    wage = Transaction(
        worker_id=worker_id, type=TransactionType.WAGE, amount=Decimal(amount), date=on,
        note="typed by hand",
    )
    db.add(wage)
    db.commit()
    return wage.id


# --- the audit ---------------------------------------------------------------------


def test_a_clean_ledger_audits_clean(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(0), "present"), 201)

    audit = admin.audit()

    assert audit == {"consistent": True, "counts": {}, "issues": []}


def test_the_audit_finds_every_kind_of_break(api, db):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    for offset in (-3, -2, -1):
        ok(admin.mark(worker, day(offset), "present"), 201)
    ok(admin.mark(worker, day(0), "absent"), 201)

    rows = {r.date: r for r in db.scalars(select(Attendance)).all()}
    rows[day(-3)].transaction.amount = Decimal("1.00")  # wrong amount
    rows[day(-2)].transaction.date = day(-9)  # wrong date
    stray = Transaction(worker_id=worker, type=TransactionType.WAGE, amount=Decimal("5"), date=day(0))
    db.add(stray)
    db.flush()
    rows[day(0)].transaction_id = stray.id  # wage on an absent day
    db.commit()
    _legacy_day(db, worker, day(-6), "present")  # unpriced

    audit = admin.audit()

    assert audit["consistent"] is False
    assert audit["counts"] == {
        "wrong_amount": 1,
        "wrong_date": 1,
        "stray_wage": 1,
        "unpriced": 1,
    }


# --- the reconcile ------------------------------------------------------------------


def test_reconcile_prices_legacy_days_and_reports_before_writing(api, db):
    admin = api.admin()
    worker = admin.worker("Legacy", daily_wage="600")
    for offset, status in [(-4, "present"), (-3, "half_day"), (-2, "absent"), (-1, "rain_out")]:
        _legacy_day(db, worker, day(offset), status)

    dry = admin.reconcile(apply=False)

    assert dry["applied"] is False
    assert (dry["days_priced"], dry["wages_created"]) == (4, 2)
    assert Decimal(dry["net_change"]) == Decimal("900.00")
    assert dry["by_worker"][0]["worker_name"] == "Legacy"
    assert api.wages(worker) == Decimal("0.00")  # a dry run wrote nothing

    applied = admin.reconcile(apply=True)

    assert applied["applied"] is True
    assert Decimal(applied["net_change"]) == Decimal("900.00")
    assert applied["remaining_issues"] == []
    assert api.wages(worker) == Decimal("900.00")
    api.assert_ledger_matches_register(worker)
    assert admin.audit()["consistent"] is True


def test_reconcile_adopts_hand_typed_wages_rather_than_doubling_them(api, db):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _legacy_day(db, worker, day(-2), "present")
    _legacy_day(db, worker, day(-1), "present")
    typed = _hand_typed_wage(db, worker, day(-2), "600.00")

    report = admin.reconcile(apply=True)

    assert (report["wages_adopted"], report["wages_created"]) == (1, 1)
    assert Decimal(report["net_change"]) == Decimal("600.00")  # only the untyped day adds
    assert api.attendance(worker, day(-2)).transaction_id == typed
    assert api.wages(worker) == Decimal("1200.00")
    api.assert_ledger_matches_register(worker)


def test_reconcile_does_not_adopt_a_wage_that_does_not_match_the_day(api, db):
    """A bonus typed on the same day is not that day's wage."""
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _legacy_day(db, worker, day(-1), "present")
    _hand_typed_wage(db, worker, day(-1), "250.00")

    report = admin.reconcile(apply=True)

    assert (report["wages_adopted"], report["wages_created"]) == (0, 1)
    assert api.wages(worker) == Decimal("850.00")


def test_reconcile_leaves_workers_without_a_rate_and_lists_them(api, db):
    admin = api.admin()
    worker = admin.worker(daily_wage=None)
    _legacy_day(db, worker, day(-1), "present")

    report = admin.reconcile(apply=True)

    assert report["still_unpriced"] == 1
    assert api.wages(worker) == Decimal("0.00")


def test_reconcile_is_idempotent(api, db):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _legacy_day(db, worker, day(-1), "present")

    admin.reconcile(apply=True)
    second = admin.reconcile(apply=True)

    assert (second["days_priced"], second["wages_created"]) == (0, 0)
    assert Decimal(second["net_change"]) == Decimal("0.00")
    assert api.wages(worker) == Decimal("600.00")


def test_reconcile_never_touches_another_contractors_crew(api, db):
    mine, theirs = api.admin("Mine"), api.admin("Theirs")
    my_worker = mine.worker(daily_wage="600")
    their_worker = theirs.worker(daily_wage="600")
    _legacy_day(db, my_worker, day(-1), "present")
    _legacy_day(db, their_worker, day(-1), "present")

    mine.reconcile(apply=True)

    assert api.wages(my_worker) == Decimal("600.00")
    assert api.wages(their_worker) == Decimal("0.00")


# --- replays from the offline queue -----------------------------------------------


def test_a_replayed_advance_with_the_same_key_is_written_once(api):
    admin = api.admin()
    worker = admin.worker()

    first = ok(admin.advance(worker, "500", day(0), client_ref="queue-1"), 201)
    replay = ok(admin.advance(worker, "500", day(0), client_ref="queue-1"), 201)

    assert replay["id"] == first["id"]
    assert admin.balance(worker)["total_advances"] == Decimal("500.00")


def test_two_identical_advances_with_different_keys_are_two_advances(api):
    """Two Rs.500 advances on one day are a legitimate thing to record; only
    the KEY says whether it is a repeat."""
    admin = api.admin()
    worker = admin.worker()

    ok(admin.advance(worker, "500", day(0), client_ref="queue-1"), 201)
    ok(admin.advance(worker, "500", day(0), client_ref="queue-2"), 201)

    assert admin.balance(worker)["total_advances"] == Decimal("1000.00")


def test_a_key_from_another_contractor_never_returns_their_row(api):
    mine, theirs = api.admin("Mine"), api.admin("Theirs")
    their_worker = theirs.worker()
    my_worker = mine.worker()
    ok(theirs.advance(their_worker, "999", day(0), client_ref="shared-key"), 201)

    response = mine.advance(my_worker, "10", day(0), client_ref="shared-key")

    # The key is taken, so the write cannot land - but nothing of theirs leaks.
    assert response.status_code == 409
    assert "999" not in response.text


def test_a_replayed_attendance_mark_is_a_409_and_changes_nothing(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(-1), "present"), 201)

    replay = admin.mark(worker, day(-1), "present")

    assert replay.status_code == 409
    assert api.wages(worker) == Decimal("600.00")
