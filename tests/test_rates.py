"""A day keeps the rate it was worked at - unless somebody says otherwise.

Three scenarios the first version of the wage link got wrong or could not
express:

  * a raise in October repriced September the next time any September day was
    resubmitted, corrected or rained out (the rate was read off the worker);
  * a worker booked with a name only, marked present all week, and given a
    rate on Saturday was owed nothing for the week (the zero-wage trap);
  * "agreed 600, typed 60" was stuck at 60 forever.
"""

from decimal import Decimal

from tests.helpers import day, ok


# --- a raise is not a rewrite of history ----------------------------------------


def test_a_raise_does_not_reprice_days_already_worked(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="500")
    ok(admin.mark(worker, day(-2), "present"), 201)

    ok(admin.replace_worker(worker, daily_wage="650"))
    ok(admin.mark(worker, day(-1), "present"), 201)

    assert api.attendance(worker, day(-2)).transaction.amount == Decimal("500.00")
    assert api.attendance(worker, day(-1)).transaction.amount == Decimal("650.00")
    assert api.wages(worker) == Decimal("1150.00")
    api.assert_ledger_matches_register(worker)


def test_resubmitting_an_old_grid_after_a_raise_keeps_the_old_rate(api):
    """The regression: a resubmission re-syncs unchanged rows, and used to
    re-read the worker's CURRENT rate while doing it."""
    admin = api.admin()
    worker = admin.worker(daily_wage="500")
    mark = [{"worker_id": worker, "status": "present"}]
    ok(admin.mark_bulk(mark, day(-5)), 201)
    ok(admin.replace_worker(worker, daily_wage="700"))

    ok(admin.mark_bulk(mark, day(-5)), 201)

    assert api.wages(worker) == Decimal("500.00")
    api.assert_ledger_matches_register(worker)


def test_correcting_an_old_day_after_a_raise_halves_the_old_rate(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="500")
    ok(admin.mark(worker, day(-5), "present"), 201)
    ok(admin.replace_worker(worker, daily_wage="700"))

    ok(admin.correct(admin.attendance_id(worker, day(-5)), status="half_day"))

    assert api.wages(worker) == Decimal("250.00")  # half of 500, not of 700
    api.assert_ledger_matches_register(worker)


def test_an_absent_day_corrected_to_present_later_earns_its_own_days_rate(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="500")
    ok(admin.mark(worker, day(-5), "absent"), 201)
    ok(admin.replace_worker(worker, daily_wage="700"))

    ok(admin.correct(admin.attendance_id(worker, day(-5)), status="present"))

    assert api.wages(worker) == Decimal("500.00")


def test_a_rain_out_after_a_raise_takes_back_the_original_amount(api):
    admin = api.admin()
    site = admin.site()
    worker = admin.worker(daily_wage="500", site_id=site)
    ok(admin.mark(worker, day(0), "present"), 201)
    ok(admin.replace_worker(worker, daily_wage="900"))

    ok(admin.rain_out(site, day(0)))

    assert api.wages(worker) == Decimal("0.00")
    api.assert_ledger_matches_register(worker)


# --- the zero-wage trap ------------------------------------------------------------


def test_days_marked_without_a_rate_are_priced_when_one_is_set(api):
    admin = api.admin()
    worker = admin.worker(daily_wage=None)
    for offset, status in [(-4, "present"), (-3, "half_day"), (-2, "absent"), (-1, "present")]:
        ok(admin.mark(worker, day(offset), status), 201)
    assert api.wages(worker) == Decimal("0.00")
    assert api.attendance(worker, day(-4)).wage_rate is None

    saved = ok(admin.replace_worker(worker, daily_wage="600"))

    # Three earning days, priced; the absence is priced too but earns nothing.
    assert saved["days_priced"] == 4
    assert api.wages(worker) == Decimal("1500.00")  # 600 + 300 + 0 + 600
    api.assert_ledger_matches_register(worker)


def test_a_rate_of_zero_is_read_as_not_known_yet(api):
    """The form treats the field as optional; a zero typed into it must not
    lock the days marked meanwhile at zero forever."""
    admin = api.admin()
    worker = admin.worker(daily_wage="0")
    ok(admin.mark(worker, day(-1), "present"), 201)
    assert api.attendance(worker, day(-1)).wage_rate is None

    ok(admin.replace_worker(worker, daily_wage="550"))

    assert api.wages(worker) == Decimal("550.00")


def test_setting_a_rate_with_patch_prices_days_and_says_so(api):
    admin = api.admin()
    worker = admin.worker(daily_wage=None)
    ok(admin.mark(worker, day(0), "present"), 201)

    saved = ok(admin.patch(f"/contractor/workers/{worker}", json={"daily_wage": "480"}))

    assert saved["days_priced"] == 1
    assert api.wages(worker) == Decimal("480.00")


def test_pricing_adopts_a_wage_typed_by_hand_instead_of_doubling_it(api):
    """A contractor working around the old bug typed the day's wage in by
    hand. Pricing the day must link that wage, not add a second one."""
    admin = api.admin()
    worker = admin.worker(daily_wage=None)
    ok(admin.mark(worker, day(-1), "present"), 201)
    ok(admin.transaction(worker, "wage", "600", day(-1)), 201)

    ok(admin.replace_worker(worker, daily_wage="600"))

    assert api.wages(worker) == Decimal("600.00")
    row = api.attendance(worker, day(-1))
    assert row.transaction_id is not None
    api.assert_ledger_matches_register(worker)


def test_an_unrelated_edit_does_not_touch_priced_days(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(0), "present"), 201)

    saved = ok(admin.replace_worker(worker, name="Suresh Kumar"))

    assert saved["days_priced"] == 0
    assert api.wages(worker) == Decimal("600.00")


# --- deliberately re-pricing -------------------------------------------------------


def test_reprice_existing_moves_marked_days_onto_the_corrected_rate(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="60")  # agreed 600, typed 60
    for offset in (-3, -2, -1):
        ok(admin.mark(worker, day(offset), "present"), 201)
    ok(admin.mark(worker, day(0), "half_day"), 201)

    saved = ok(admin.replace_worker(worker, daily_wage="600", reprice_existing=True))

    assert saved["days_repriced"] == 4
    assert api.wages(worker) == Decimal("2100.00")  # 3 x 600 + 300
    api.assert_ledger_matches_register(worker)


def test_reprice_from_leaves_earlier_days_alone(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="500")
    for offset in (-4, -3, -2, -1):
        ok(admin.mark(worker, day(offset), "present"), 201)

    saved = ok(
        admin.replace_worker(
            worker, daily_wage="600", reprice_existing=True, reprice_from=day(-2)
        )
    )

    assert saved["days_repriced"] == 2
    assert api.wages(worker) == Decimal("2200.00")  # 2 x 500 + 2 x 600


def test_repricing_onto_no_rate_is_refused(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="500")
    ok(admin.mark(worker, day(0), "present"), 201)

    response = admin.replace_worker(worker, daily_wage=None, reprice_existing=True)

    assert response.status_code == 422
    assert api.wages(worker) == Decimal("500.00")


# --- switching between day rate and piece rate --------------------------------


def test_moving_a_worker_onto_piece_rate_keeps_the_day_wages_they_earned(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(-2), "present"), 201)

    ok(admin.replace_worker(worker, wage_type="piece_rate"))
    # Touch the old day again - a resubmitted grid re-syncs it.
    ok(admin.mark_bulk([{"worker_id": worker, "status": "present"}], day(-2)), 201)
    ok(admin.mark(worker, day(-1), "present"), 201)

    assert api.attendance(worker, day(-2)).transaction.amount == Decimal("600.00")
    assert api.attendance(worker, day(-1)).wage_rate == Decimal("0.00")
    assert api.wages(worker) == Decimal("600.00")
    api.assert_ledger_matches_register(worker)


def test_moving_a_piece_rate_worker_onto_a_day_rate_does_not_pay_old_piece_days(api):
    admin = api.admin()
    worker = admin.worker(daily_wage=None, wage_type="piece_rate")
    ok(admin.mark_bulk([{"worker_id": worker, "quantity": "10", "rate": "30"}], day(-2)), 201)

    ok(admin.replace_worker(worker, wage_type="daily", daily_wage="600"))
    ok(admin.mark(worker, day(-1), "present"), 201)

    assert api.wages(worker) == Decimal("900.00")  # 300 piece + 600 day, nothing doubled
    api.assert_ledger_matches_register(worker)
