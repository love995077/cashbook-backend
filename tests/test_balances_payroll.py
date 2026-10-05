"""Advances, payments, negative balances, settlement and installment plans.

balance = wages - advances - payments, for every worker, on every screen.
"""

from decimal import Decimal

from sqlalchemy import func, select

from app.models.deduction import AutoDeduction, DeductionInstallment
from tests.helpers import day, ok


def _week(admin, worker, statuses):
    for offset, status in zip(range(-len(statuses) + 1, 1), statuses):
        ok(admin.mark(worker, day(offset), status), 201)


# --- the balance ---------------------------------------------------------------


def test_balance_is_wages_less_advances_less_payments(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _week(admin, worker, ["present", "present", "half_day", "absent", "present"])  # 2100
    ok(admin.advance(worker, "1000", day(-3)), 201)
    ok(admin.payment(worker, "500", day(0)), 201)

    balance = admin.balance(worker)

    assert balance == {
        "total_wages": Decimal("2100.00"),
        "total_advances": Decimal("1000.00"),
        "total_payments": Decimal("500.00"),
        "balance": Decimal("600.00"),
    }
    api.assert_ledger_matches_register(worker)


def test_an_advance_larger_than_earnings_gives_a_negative_balance(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(0), "present"), 201)
    ok(admin.advance(worker, "5000", day(0)), 201)

    assert admin.balance(worker)["balance"] == Decimal("-4400.00")
    # Nobody is owed anything; the worker is ahead.
    assert Decimal(admin.summary()["outstanding_payable"]) == Decimal("0.00")
    # And it cannot be "settled" into a payment.
    response = admin.settle(worker)
    assert response.status_code == 409
    assert "more than they have earned" in response.json()["detail"]


def test_one_workers_overpayment_does_not_reduce_what_another_is_owed(api):
    admin = api.admin()
    ahead = admin.worker("Ahead", daily_wage="600")
    owed = admin.worker("Owed", daily_wage="600")
    ok(admin.advance(ahead, "3000", day(0)), 201)
    ok(admin.mark(owed, day(0), "present"), 201)

    assert Decimal(admin.summary()["outstanding_payable"]) == Decimal("600.00")


def test_wages_paid_in_the_cash_box_are_advances_and_payments_not_wages(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _week(admin, worker, ["present", "present"])
    ok(admin.advance(worker, "200", day(0)), 201)

    summary = admin.summary()

    assert Decimal(summary["wages_earned"]) == Decimal("1200.00")
    assert Decimal(summary["wages_paid"]) == Decimal("200.00")
    assert Decimal(summary["outstanding_payable"]) == Decimal("1000.00")


def test_a_wage_cannot_carry_a_payment_mode_and_money_cannot_be_dated_ahead(api):
    admin = api.admin()
    worker = admin.worker()

    assert admin.transaction(worker, "wage", "100", day(0), payment_mode="upi").status_code == 422
    assert admin.advance(worker, "100", day(1)).status_code == 422
    assert admin.advance(worker, "0", day(0)).status_code == 422
    assert admin.advance(worker, "-5", day(0)).status_code == 422


# --- settlement ------------------------------------------------------------------


def test_settling_pays_exactly_the_balance_and_zeroes_it(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _week(admin, worker, ["present", "present", "half_day"])  # 1500
    ok(admin.advance(worker, "400", day(-1)), 201)

    preview = admin.preview(worker)
    settled = ok(admin.settle(worker, payment_mode="upi"), 201)

    assert Decimal(preview["net_payable"]) == Decimal("1100.00")
    assert Decimal(preview["days_worked"]) == Decimal("2.5")
    assert Decimal(settled["transaction"]["amount"]) == Decimal("1100.00")
    assert Decimal(settled["balance_after"]) == Decimal("0.00")
    assert admin.settle(worker).status_code == 409  # nothing left


def test_a_settlement_cannot_end_in_the_future(api):
    admin = api.admin()
    worker = admin.worker()
    ok(admin.mark(worker, day(0), "present"), 201)

    assert admin.settle(worker, end_date=day(1)).status_code == 422


def test_a_narrow_window_cannot_pay_the_same_wages_twice(api):
    """Settle everything, then re-run a narrower window: its wages show up but
    the earlier payment sits outside it. The lifetime cap refuses a second
    payment."""
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _week(admin, worker, ["present", "present", "present"])
    ok(admin.settle(worker), 201)

    preview = admin.preview(worker, start_date=day(-2), end_date=day(-1))

    assert Decimal(preview["window_balance"]) == Decimal("1200.00")
    assert Decimal(preview["net_payable"]) == Decimal("0.00")
    assert admin.settle(worker, start_date=day(-2), end_date=day(-1)).status_code == 409


def test_a_settlement_retry_with_the_same_key_returns_the_first_one(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(0), "present"), 201)

    first = ok(admin.settle(worker, client_ref="settle-abc"), 201)
    retry = admin.settle(worker, client_ref="settle-abc")

    assert retry.status_code in (200, 201)
    assert retry.json()["transaction"]["id"] == first["transaction"]["id"]
    assert admin.balance(worker)["total_payments"] == Decimal("600.00")


# --- installment plans -----------------------------------------------------------


def _advance_with_plan(admin, worker, amount="1000", daily="100"):
    return ok(admin.advance(worker, amount, day(-10), daily_deduction=daily), 201)


def _recovered(api, worker):
    total = api.fresh().scalar(
        select(func.sum(DeductionInstallment.amount)).where(
            DeductionInstallment.worker_id == worker
        )
    )
    return Decimal(total or 0).quantize(Decimal("0.01"))


def _plan(api, worker):
    return api.fresh().scalar(select(AutoDeduction).where(AutoDeduction.worker_id == worker))


def test_every_marking_path_takes_the_days_installment(api):
    """The single mark (the offline queue's route), the grid and the scan
    used to disagree - only the grid took an installment."""
    admin = api.admin()
    single, grid, scanned = (admin.worker(n, daily_wage="600") for n in ("S", "G", "Q"))
    for worker in (single, grid, scanned):
        _advance_with_plan(admin, worker)

    ok(admin.mark(single, day(0), "present"), 201)
    ok(admin.mark_bulk([{"worker_id": grid, "status": "present"}], day(0)), 201)
    ok(admin.scan(scanned))

    for worker in (single, grid, scanned):
        assert _recovered(api, worker) == Decimal("100.00")
        assert _plan(api, worker).balance_remaining == Decimal("900.00")


def test_a_half_day_takes_half_an_installment_and_an_absence_takes_none(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _advance_with_plan(admin, worker)

    ok(admin.mark(worker, day(-1), "half_day"), 201)
    ok(admin.mark(worker, day(0), "absent"), 201)

    assert _recovered(api, worker) == Decimal("50.00")


def test_a_rain_out_over_a_present_day_gives_the_installment_back(api):
    """Regression: the rain-out changed the status and kept the deduction the
    present mark had taken."""
    admin = api.admin()
    site = admin.site()
    worker = admin.worker(daily_wage="600", site_id=site)
    _advance_with_plan(admin, worker)
    ok(admin.mark(worker, day(0), "present"), 201)
    assert _recovered(api, worker) == Decimal("100.00")

    ok(admin.rain_out(site, day(0)))

    assert _recovered(api, worker) == Decimal("0.00")
    assert _plan(api, worker).balance_remaining == Decimal("1000.00")


def test_an_overwriting_grid_gives_the_installment_back(api):
    """Regression: the grid's overwrite path changed the status without
    re-settling the installment."""
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _advance_with_plan(admin, worker)
    ok(admin.mark_bulk([{"worker_id": worker, "status": "present"}], day(0)), 201)

    ok(admin.mark_bulk([{"worker_id": worker, "status": "absent"}], day(0), overwrite=True), 201)

    assert _recovered(api, worker) == Decimal("0.00")


def test_corrections_and_unmarks_move_the_installment_with_the_day(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _advance_with_plan(admin, worker)
    ok(admin.mark(worker, day(-1), "present"), 201)
    attendance_id = admin.attendance_id(worker, day(-1))

    ok(admin.correct(attendance_id, status="half_day"))
    assert _recovered(api, worker) == Decimal("50.00")
    ok(admin.correct(attendance_id, status="present"))
    assert _recovered(api, worker) == Decimal("100.00")
    ok(admin.unmark(attendance_id), 204)
    assert _recovered(api, worker) == Decimal("0.00")
    assert _plan(api, worker).balance_remaining == Decimal("1000.00")


def test_a_resubmitted_day_does_not_take_a_second_installment(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _advance_with_plan(admin, worker)
    mark = [{"worker_id": worker, "status": "present"}]

    for _ in range(3):
        ok(admin.mark_bulk(mark, day(0)), 201)

    assert _recovered(api, worker) == Decimal("100.00")


def test_the_last_installment_is_capped_at_what_is_left(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _advance_with_plan(admin, worker, amount="250", daily="100")

    for offset in (-2, -1, 0):
        ok(admin.mark(worker, day(offset), "present"), 201)

    assert _recovered(api, worker) == Decimal("250.00")
    plan = _plan(api, worker)
    assert plan.balance_remaining == Decimal("0.00")
    assert plan.is_active is False


def test_payroll_holds_back_only_the_installments_recovered(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    _advance_with_plan(admin, worker, amount="1000", daily="100")
    for offset in (-1, 0):
        ok(admin.mark(worker, day(offset), "present"), 201)

    preview = admin.preview(worker)

    # Ledger: 1200 earned - 1000 advanced = 200. Payroll: the advance is not
    # due in full, only the 200 recovered so far -> 1200 - 200 = 1000.
    assert Decimal(preview["window_balance"]) == Decimal("200.00")
    assert Decimal(preview["installments_recovered"]) == Decimal("200.00")
    assert Decimal(preview["net_payable"]) == Decimal("1000.00")
    assert Decimal(preview["emi_outstanding"]) == Decimal("800.00")
