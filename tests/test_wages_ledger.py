"""Attendance earns wages: every status, every correction, every write path.

The invariant under test (app/services/wages.py): each attendance row's linked
WAGE is exactly round_half_up(wage_rate x weight(status)) on the row's date,
and a row that earns nothing has none. `assert_ledger_matches_register`
checks it two independent ways after every step.
"""

from decimal import Decimal

import pytest

from tests.helpers import day, expected_day_wage, money, ok

STATUSES = ["present", "half_day", "absent", "rain_out"]


# --- one day, one status --------------------------------------------------------


@pytest.mark.parametrize(
    "status, earned",
    [
        ("present", "600.00"),
        ("half_day", "300.00"),
        ("absent", "0.00"),
        ("rain_out", "0.00"),
    ],
)
def test_each_status_earns_exactly_its_share(api, status, earned):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")

    ok(admin.mark(worker, day(0), status), 201)

    assert api.wages(worker) == money(earned)
    row = api.attendance(worker, day(0))
    assert row.wage_rate == Decimal("600.00")
    assert (row.transaction_id is not None) == (earned != "0.00")
    api.assert_ledger_matches_register(worker)


def test_marking_present_is_atomic_with_the_wage(api):
    """The day and the money land together: the wage is linked, dated the day,
    labelled as a day wage, carries no payment mode, and shows in the balance
    the moment the mark returns."""
    admin = api.admin()
    worker = admin.worker(daily_wage="750.50")

    mark = ok(admin.mark(worker, day(-2), "present"), 201)

    row = api.attendance(worker, day(-2))
    wage = row.transaction
    assert wage is not None
    assert wage.amount == Decimal("750.50")
    assert wage.date == day(-2)
    assert wage.type.value == "wage"
    assert wage.payment_mode is None
    assert wage.note == "Day wage"
    assert Decimal(mark["wage_rate"]) == Decimal("750.50")
    assert admin.balance(worker)["total_wages"] == Decimal("750.50")
    assert admin.balance(worker)["balance"] == Decimal("750.50")


@pytest.mark.parametrize(
    "rate, half",
    [
        ("601.01", "300.51"),  # 300.505 rounds half UP, not to even
        ("600.03", "300.02"),  # 300.015 -> 300.02
        ("0.01", "0.01"),  # 0.005 -> 0.01: the smallest rate still earns
        ("99999999.99", "50000000.00"),  # the column's ceiling, halved
    ],
)
def test_half_day_rounds_half_up_to_the_paisa(api, rate, half):
    admin = api.admin()
    worker = admin.worker(daily_wage=rate)

    ok(admin.mark(worker, day(0), "half_day"), 201)

    assert api.wages(worker) == Decimal(half)
    assert api.wages(worker) == expected_day_wage("half_day", Decimal(rate))
    api.assert_ledger_matches_register(worker)


# --- correcting a day: every transition -----------------------------------------


@pytest.mark.parametrize("before", STATUSES)
@pytest.mark.parametrize("after", STATUSES)
def test_every_correction_moves_the_wage_with_the_day(api, before, after):
    admin = api.admin()
    worker = admin.worker(daily_wage="640")
    ok(admin.mark(worker, day(-1), before), 201)
    attendance_id = admin.attendance_id(worker, day(-1))

    ok(admin.correct(attendance_id, status=after))

    assert api.wages(worker) == expected_day_wage(after, Decimal("640"))
    api.assert_ledger_matches_register(worker)


def test_correcting_back_and_forth_never_leaves_a_second_wage(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="500")
    ok(admin.mark(worker, day(0), "present"), 201)
    attendance_id = admin.attendance_id(worker, day(0))

    for status in ["absent", "present", "half_day", "rain_out", "present", "present"]:
        ok(admin.correct(attendance_id, status=status))
        api.assert_ledger_matches_register(worker)

    assert api.wages(worker) == Decimal("500.00")
    assert len([e for e in admin.ledger(worker)["transactions"] if e["type"] == "wage"]) == 1


def test_moving_a_day_moves_its_wage(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(-3), "present"), 201)
    attendance_id = admin.attendance_id(worker, day(-3))

    ok(admin.correct(attendance_id, date=day(-5)))

    row = api.attendance(worker, day(-5))
    assert row.transaction.date == day(-5)
    api.assert_ledger_matches_register(worker)


def test_moving_a_day_onto_a_marked_day_is_refused_and_changes_nothing(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(-1), "present"), 201)
    ok(admin.mark(worker, day(-2), "half_day"), 201)

    response = admin.correct(admin.attendance_id(worker, day(-1)), date=day(-2))

    assert response.status_code == 409
    assert api.wages(worker) == Decimal("900.00")
    api.assert_ledger_matches_register(worker)


def test_a_correction_cannot_move_a_day_into_the_future(api):
    admin = api.admin()
    worker = admin.worker()
    ok(admin.mark(worker, day(0), "present"), 201)

    response = admin.correct(admin.attendance_id(worker, day(0)), date=day(1))

    assert response.status_code == 422
    api.assert_ledger_matches_register(worker)


def test_null_fields_in_a_correction_are_ignored_not_written(api):
    """An explicit null status is a malformed request, not an instruction to
    blank a NOT NULL column (which used to surface as a misleading 409)."""
    admin = api.admin()
    worker = admin.worker()
    ok(admin.mark(worker, day(0), "present"), 201)

    ok(admin.correct(admin.attendance_id(worker, day(0)), status=None, note="checked"))

    row = api.attendance(worker, day(0))
    assert row.status.value == "present"
    assert row.note == "checked"


# --- unmarking ------------------------------------------------------------------


@pytest.mark.parametrize("status", STATUSES)
def test_unmarking_takes_the_wage_away(api, status):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(0), status), 201)

    ok(admin.unmark(admin.attendance_id(worker, day(0))), 204)

    assert api.attendance(worker, day(0)) is None
    assert api.wages(worker) == Decimal("0.00")
    api.assert_ledger_matches_register(worker)


def test_an_attendance_wage_cannot_be_deleted_as_a_ledger_entry(api):
    """Deleting the wage alone would leave the day earning nothing, and the
    next touch of the day would silently write it back."""
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(0), "present"), 201)
    wage_id = api.attendance(worker, day(0)).transaction_id

    response = admin.delete(f"/contractor/transactions/{wage_id}")

    assert response.status_code == 409
    assert api.wages(worker) == Decimal("600.00")


# --- every write path, not just the single mark ---------------------------------


def test_the_crew_grid_earns_and_a_resubmission_does_not_pay_twice(api):
    admin = api.admin()
    site = admin.site()
    a = admin.worker("A", daily_wage="600", site_id=site)
    b = admin.worker("B", daily_wage="450", site_id=site)
    marks = [{"worker_id": a, "status": "present"}, {"worker_id": b, "status": "half_day"}]

    first = ok(admin.mark_bulk(marks, day(0), site_id=site), 201)
    second = ok(admin.mark_bulk(marks, day(0), site_id=site), 201)

    assert (first["created"], second["created"], second["unchanged"]) == (2, 0, 2)
    assert api.wages(a) == Decimal("600.00")
    assert api.wages(b) == Decimal("225.00")
    assert api.attendance(a, day(0)).site_id == site
    api.assert_ledger_matches_register(a, b)


def test_an_overwriting_grid_moves_the_wage(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark_bulk([{"worker_id": worker, "status": "present"}], day(0)), 201)

    result = ok(
        admin.mark_bulk([{"worker_id": worker, "status": "absent"}], day(0), overwrite=True),
        201,
    )

    assert result["updated"] == 1
    assert api.wages(worker) == Decimal("0.00")
    api.assert_ledger_matches_register(worker)


def test_a_qr_scan_earns_and_a_rescan_is_a_no_op(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")

    ok(admin.scan(worker))
    ok(admin.scan(worker))
    ok(admin.scan(worker, status="half_day"))

    assert api.attendance(worker, day(0)).status.value == "half_day"
    assert api.wages(worker) == Decimal("300.00")
    api.assert_ledger_matches_register(worker)


def test_a_rain_out_over_a_present_day_takes_the_wage_back(api):
    admin = api.admin()
    site = admin.site()
    worker = admin.worker(daily_wage="600", site_id=site)
    ok(admin.mark(worker, day(0), "present"), 201)

    result = ok(admin.rain_out(site, day(0)))

    assert result["updated"] == 1
    assert api.attendance(worker, day(0)).status.value == "rain_out"
    assert api.wages(worker) == Decimal("0.00")
    api.assert_ledger_matches_register(worker)


def test_a_rain_out_without_overwrite_leaves_marked_days_and_their_wages(api):
    admin = api.admin()
    site = admin.site()
    marked = admin.worker("Marked", daily_wage="600", site_id=site)
    unmarked = admin.worker("Unmarked", daily_wage="600", site_id=site)
    ok(admin.mark(marked, day(0), "present"), 201)

    result = ok(admin.rain_out(site, day(0), overwrite=False))

    assert (result["created"], result["unchanged"]) == (1, 1)
    assert api.wages(marked) == Decimal("600.00")
    assert api.attendance(unmarked, day(0)).status.value == "rain_out"
    api.assert_ledger_matches_register(marked, unmarked)


def test_marking_an_already_marked_day_is_a_409_and_writes_nothing(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(0), "present"), 201)

    response = admin.mark(worker, day(0), "absent")

    assert response.status_code == 409
    assert api.attendance(worker, day(0)).status.value == "present"
    assert api.wages(worker) == Decimal("600.00")


def test_a_single_mark_is_stamped_with_the_workers_site(api):
    """The offline queue replays single marks, and an older build sends no
    site - the row must still say where the day was worked."""
    admin = api.admin()
    site = admin.site()
    worker = admin.worker(site_id=site)

    ok(admin.mark(worker, day(0), "present"), 201)

    assert api.attendance(worker, day(0)).site_id == site


# --- piece rate -------------------------------------------------------------------


def test_a_piece_rate_worker_is_paid_by_the_log_not_the_day(api):
    admin = api.admin()
    tiler = admin.worker("Tiler", daily_wage=None, wage_type="piece_rate")

    ok(
        admin.mark_bulk(
            [{"worker_id": tiler, "quantity": "120.5", "rate": "18", "work_description": "Tiling"}],
            day(0),
        ),
        201,
    )

    row = api.attendance(tiler, day(0))
    assert row.status.value == "present"
    assert row.wage_rate == Decimal("0.00")  # not paid by the day
    assert row.transaction_id is None
    assert api.wages(tiler) == Decimal("2169.00")  # 120.5 x 18, from the log alone
    api.assert_ledger_matches_register(tiler)


def test_a_piece_rate_resubmission_does_not_pay_for_the_tiles_twice(api):
    admin = api.admin()
    tiler = admin.worker("Tiler", daily_wage=None, wage_type="piece_rate")
    mark = [{"worker_id": tiler, "quantity": "10", "rate": "20"}]

    ok(admin.mark_bulk(mark, day(0)), 201)
    ok(admin.mark_bulk(mark, day(0)), 201)

    assert api.wages(tiler) == Decimal("200.00")


def test_unmarking_a_day_that_paid_piece_work_is_refused(api):
    admin = api.admin()
    tiler = admin.worker("Tiler", daily_wage=None, wage_type="piece_rate")
    ok(admin.mark_bulk([{"worker_id": tiler, "quantity": "10", "rate": "20"}], day(0)), 201)

    response = admin.unmark(admin.attendance_id(tiler, day(0)))

    assert response.status_code == 409
    assert api.wages(tiler) == Decimal("200.00")


# --- the whole picture ------------------------------------------------------------


def test_a_fortnight_for_a_mixed_crew_adds_up_to_the_paisa(api):
    """Two weeks, three workers, every status, corrections, an unmark and a
    rain-out - then the ledger is compared with the register line by line and
    in total."""
    admin = api.admin()
    site = admin.site()
    mason = admin.worker("Mason", daily_wage="850", site_id=site)
    helper = admin.worker("Helper", daily_wage="512.25", site_id=site)
    chowkidar = admin.worker("Chowkidar", daily_wage="400", site_id=site)
    crew = [mason, helper, chowkidar]

    pattern = ["present", "present", "half_day", "absent", "present", "rain_out", "present"]
    for offset in range(-13, 1):
        status = pattern[(offset + 13) % len(pattern)]
        if status == "rain_out":
            ok(admin.rain_out(site, day(offset)))
            continue
        marks = [{"worker_id": w, "status": status} for w in crew]
        marks[1]["status"] = "half_day" if status == "present" else status
        ok(admin.mark_bulk(marks, day(offset), site_id=site), 201)

    ok(admin.correct(admin.attendance_id(mason, day(-10)), status="absent"))
    ok(admin.correct(admin.attendance_id(helper, day(-3)), status="present"))
    ok(admin.unmark(admin.attendance_id(chowkidar, day(-7))), 204)

    api.assert_ledger_matches_register(*crew)
    for worker in crew:
        assert api.wages(worker) == api.oracle_attendance_wages(worker)
        balance = admin.balance(worker)
        assert balance["total_wages"] == api.wages(worker)
        assert balance["balance"] == balance["total_wages"]
