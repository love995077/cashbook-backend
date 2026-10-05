"""Removing a worker, and what "today" means for an Indian site on a UTC server."""

from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import TODAY, day, ok

IST = ZoneInfo("Asia/Kolkata")


# --- deactivation ----------------------------------------------------------------


def test_deactivating_keeps_the_ledger_and_the_site(api):
    admin = api.admin()
    site = admin.site()
    worker = admin.worker(daily_wage="600", site_id=site)
    ok(admin.mark(worker, day(-1), "present"), 201)
    ok(admin.advance(worker, "100", day(-1)), 201)

    ok(admin.deactivate(worker), 204)

    row = api.worker_row(worker)
    assert row.is_active is False
    assert row.deactivated_on == day(1)  # last day worked: today
    assert row.site_id == site  # the posting is kept for the site's history
    assert admin.balance(worker)["balance"] == Decimal("500.00")
    api.assert_ledger_matches_register(worker)


def test_a_deactivated_worker_drops_out_of_the_crew_and_the_site_counts(api):
    admin = api.admin()
    site = admin.site()
    stays = admin.worker("Stays", site_id=site)
    leaves = admin.worker("Leaves", site_id=site)

    ok(admin.deactivate(leaves), 204)

    crew = [w["id"] for w in ok(admin.get("/contractor/workers"))]
    everyone = [w["id"] for w in ok(admin.get("/contractor/workers", params={"include_inactive": True}))]
    sites = ok(admin.get("/contractor/sites"))
    assert crew == [stays]
    assert sorted(everyone) == sorted([stays, leaves])
    assert sites[0]["worker_count"] == 1


def test_days_after_the_last_day_cannot_be_marked_by_any_route(api):
    admin = api.admin()
    site = admin.site()
    worker = admin.worker(daily_wage="600", site_id=site)
    ok(admin.deactivate(worker, last_day=day(-2)), 204)

    responses = [
        admin.mark(worker, day(-1), "present"),
        admin.mark_bulk([{"worker_id": worker, "status": "present"}], day(0)),
        admin.scan(worker),
        admin.post(
            "/contractor/piece-rate-logs",
            json={
                "worker_id": worker,
                "work_description": "Tiling",
                "quantity": "10",
                "rate": "10",
                "date": day(0).isoformat(),
            },
        ),
    ]

    # 422, not 409: the offline queue drops a 409 as "already recorded".
    assert [r.status_code for r in responses] == [422, 422, 422, 422]
    assert "deactivated" in responses[0].json()["detail"]
    assert api.wages(worker) == Decimal("0.00")


def test_days_up_to_the_last_day_can_still_be_marked(api):
    """A worker who left on the 13th worked the 12th - from a paper register,
    or a phone that was offline until today."""
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.deactivate(worker, last_day=day(-2)), 204)

    ok(admin.mark(worker, day(-2), "present"), 201)
    ok(admin.mark(worker, day(-3), "half_day"), 201)

    assert api.wages(worker) == Decimal("900.00")
    api.assert_ledger_matches_register(worker)


def test_history_of_a_deactivated_worker_can_still_be_corrected(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(-1), "present"), 201)
    ok(admin.deactivate(worker), 204)

    ok(admin.correct(admin.attendance_id(worker, day(-1)), status="half_day"))

    assert api.wages(worker) == Decimal("300.00")


def test_a_worker_deactivated_before_the_date_was_recorded_gets_no_new_days(api, db):
    admin = api.admin()
    worker = admin.worker()
    row = api.worker_row(worker)
    row.is_active = False  # the way the old route left them: no date
    db.commit()

    assert admin.mark(worker, day(-30), "present").status_code == 422


def test_reactivating_clears_the_date_and_the_worker_can_be_marked_again(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.deactivate(worker, last_day=day(-5)), 204)

    ok(admin.patch(f"/contractor/workers/{worker}", json={"is_active": True}))
    ok(admin.mark(worker, day(0), "present"), 201)

    assert api.worker_row(worker).deactivated_on is None
    assert api.wages(worker) == Decimal("600.00")


def test_deactivating_twice_keeps_the_day_they_actually_left(api):
    admin = api.admin()
    worker = admin.worker()
    ok(admin.deactivate(worker, last_day=day(-5)), 204)

    ok(admin.deactivate(worker), 204)

    assert api.worker_row(worker).deactivated_on == day(-4)


def test_a_deactivated_worker_cannot_sign_in(api, client):
    admin = api.admin()
    phone = "9876500001"
    worker = ok(
        admin.post(
            "/contractor/workers",
            json={"name": "Has Phone", "phone": phone, "password": "pin123", "daily_wage": "600"},
        ),
        201,
    )["id"]
    ok(admin.deactivate(worker), 204)

    response = client.post("/auth/login", json={"phone": phone, "password": "pin123"})

    assert response.status_code == 403


def test_a_deactivated_worker_can_still_be_paid_what_they_are_owed(api):
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, day(-1), "present"), 201)
    ok(admin.deactivate(worker), 204)

    settled = ok(admin.settle(worker), 201)

    assert Decimal(settled["balance_after"]) == Decimal("0.00")


def test_purging_a_worker_with_history_is_refused(api):
    admin = api.admin()
    worker = admin.worker()
    ok(admin.mark(worker, day(0), "present"), 201)

    assert admin.delete(f"/contractor/workers/{worker}", params={"purge": True}).status_code == 409
    assert api.worker_row(worker) is not None


# --- the business day, not the server's ----------------------------------------


@pytest.mark.parametrize(
    "ist_time",
    [time(0, 0), time(0, 30), time(3, 15), time(5, 29), time(23, 59)],
)
def test_today_in_india_can_be_marked_at_any_hour(api, frozen_now, ist_time):
    """00:00-05:29 IST is still yesterday in UTC. A server reading its own
    clock refused the morning's attendance as 'marked before it happens'."""
    moment = datetime.combine(TODAY, ist_time, tzinfo=IST)
    frozen_now(moment)
    admin = api.admin()
    worker = admin.worker(daily_wage="600")

    ok(admin.mark(worker, TODAY, "present"), 201)
    ok(admin.mark_bulk([{"worker_id": worker, "status": "present"}], TODAY), 201)
    assert admin.mark(worker, day(1), "present").status_code == 422


def test_the_default_day_is_the_business_day(api, frozen_now):
    """At 01:00 IST the UTC date is yesterday; a grid sent without a date, a
    scan and an approved advance must all land on the Indian date."""
    frozen_now(datetime.combine(TODAY, time(1, 0), tzinfo=IST))
    admin = api.admin()
    worker = admin.worker(daily_wage="600")

    grid = ok(admin.post(
        "/contractor/attendance/bulk",
        json={"marks": [{"worker_id": worker, "status": "present"}]},
    ), 201)

    assert grid["date"] == TODAY.isoformat()


def test_settling_up_to_today_works_just_after_midnight(api, frozen_now):
    frozen_now(datetime.combine(TODAY, time(0, 15), tzinfo=IST))
    admin = api.admin()
    worker = admin.worker(daily_wage="600")
    ok(admin.mark(worker, TODAY, "present"), 201)

    settled = ok(admin.settle(worker, end_date=TODAY), 201)

    assert settled["transaction"]["date"] == TODAY.isoformat()
