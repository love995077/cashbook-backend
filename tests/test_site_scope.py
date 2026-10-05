"""Site-first: a site's labour, expenses and reports are that site's alone.

The rule under test (see `Transaction.site_id`): money follows the site stamped
on the row, never where the worker is posted today. So a worker moved to
another job, or deactivated, keeps every rupee they earned here on this
site's books - while dropping out of its crew count and its "still owed".
"""

import csv
import io
from decimal import Decimal

from sqlalchemy import select

from app.models.transaction import Transaction
from tests.helpers import day, ok


def _two_sites(api):
    admin = api.admin()
    villa = admin.site("Udaipur Villa")
    mall = admin.site("City Mall")
    return admin, villa, mall


def _expense(admin, amount, on, site_id=None, direction="out"):
    body = {"direction": direction, "amount": str(amount), "date": on.isoformat()}
    if site_id is not None:
        body["site_id"] = site_id
    return admin.post("/contractor/cash-entries", json=body)


def _summary(admin, site_id=None):
    params = {"site_id": site_id} if site_id else {}
    raw = ok(admin.get("/contractor/summary", params=params))
    return {k: (Decimal(v) if isinstance(v, str) else v) for k, v in raw.items()}


# --- every money row knows its site ---------------------------------------------


def test_a_days_wage_is_filed_under_the_site_the_day_was_worked(api):
    admin, villa, mall = _two_sites(api)
    worker = admin.worker(daily_wage="600", site_id=villa)

    ok(admin.mark(worker, day(-1), "present"), 201)
    ok(admin.mark(worker, day(0), "present", site_id=mall), 201)

    assert api.attendance(worker, day(-1)).transaction.site_id == villa
    assert api.attendance(worker, day(0)).transaction.site_id == mall
    api.assert_ledger_matches_register(worker)


def test_advances_follow_the_named_site_or_the_workers_posting(api):
    admin, villa, mall = _two_sites(api)
    worker = admin.worker(site_id=villa)

    posted = ok(admin.advance(worker, "100", day(0)), 201)
    named = ok(admin.advance(worker, "50", day(0), site_id=mall), 201)

    assert (posted["site_id"], named["site_id"]) == (villa, mall)


def test_money_cannot_be_filed_under_someone_elses_site(api):
    mine, theirs = api.admin("Mine"), api.admin("Theirs")
    their_site = theirs.site("Theirs")
    worker = mine.worker()

    assert mine.advance(worker, "100", day(0), site_id=their_site).status_code == 404
    assert _expense(mine, "100", day(0), site_id=their_site).status_code == 404
    assert mine.mark(worker, day(0), "present", site_id=their_site).status_code == 404


def test_piece_work_wages_are_filed_under_the_logs_site(api):
    admin, villa, _ = _two_sites(api)
    tiler = admin.worker("Tiler", daily_wage=None, wage_type="piece_rate", site_id=villa)

    ok(admin.mark_bulk([{"worker_id": tiler, "quantity": "10", "rate": "25"}], day(0), site_id=villa), 201)

    wage = api.fresh().scalar(select(Transaction).where(Transaction.worker_id == tiler))
    assert wage.site_id == villa


# --- one site's figures -------------------------------------------------------------


def test_a_sites_summary_is_that_sites_money_alone(api):
    admin, villa, mall = _two_sites(api)
    a = admin.worker("A", daily_wage="600", site_id=villa)
    b = admin.worker("B", daily_wage="500", site_id=mall)
    ok(admin.mark(a, day(0), "present"), 201)
    ok(admin.mark(b, day(0), "present"), 201)
    ok(admin.advance(a, "200", day(0)), 201)
    ok(_expense(admin, "1000", day(0), site_id=villa), 201)
    ok(_expense(admin, "3000", day(0), site_id=mall), 201)
    ok(_expense(admin, "5000", day(0), site_id=villa, direction="in"), 201)

    villa_sum, mall_sum, all_sum = _summary(admin, villa), _summary(admin, mall), _summary(admin)

    assert (villa_sum["wages_earned"], villa_sum["wages_paid"]) == (Decimal("600.00"), Decimal("200.00"))
    assert (villa_sum["expenses_paid"], villa_sum["cash_in"]) == (Decimal("1000.00"), Decimal("5000.00"))
    assert villa_sum["outstanding_payable"] == Decimal("400.00")
    assert villa_sum["worker_count"] == 1
    assert (mall_sum["wages_earned"], mall_sum["expenses_paid"]) == (Decimal("500.00"), Decimal("3000.00"))
    # The business is the sum of its sites (nothing here is unattributed).
    for key in ("wages_earned", "wages_paid", "expenses_paid", "cash_in", "outstanding_payable"):
        assert all_sum[key] == villa_sum[key] + mall_sum[key], key


def test_moving_a_worker_does_not_move_their_history(api):
    admin, villa, mall = _two_sites(api)
    worker = admin.worker(daily_wage="600", site_id=villa)
    ok(admin.mark(worker, day(-1), "present"), 201)

    ok(admin.patch(f"/contractor/workers/{worker}", json={"site_id": mall}))
    ok(admin.mark(worker, day(0), "present"), 201)

    assert _summary(admin, villa)["wages_earned"] == Decimal("600.00")
    assert _summary(admin, mall)["wages_earned"] == Decimal("600.00")
    # Who is owed is about who is posted there now.
    assert _summary(admin, villa)["outstanding_payable"] == Decimal("0.00")
    assert _summary(admin, mall)["outstanding_payable"] == Decimal("1200.00")


def test_a_deactivated_worker_leaves_the_crew_but_not_the_sites_books(api):
    admin, villa, _ = _two_sites(api)
    worker = admin.worker("Leaver", daily_wage="600", site_id=villa)
    stays = admin.worker("Stays", daily_wage="600", site_id=villa)
    ok(admin.mark(worker, day(-1), "present"), 201)
    ok(admin.advance(worker, "100", day(-1)), 201)

    ok(admin.deactivate(worker), 204)

    summary = _summary(admin, villa)
    assert summary["wages_earned"] == Decimal("600.00")
    assert summary["wages_paid"] == Decimal("100.00")
    assert summary["worker_count"] == 1  # only the active crew
    # Their history is in the site's exports, by the site on each row.
    rows = list(csv.reader(io.StringIO(
        admin.get("/api/reports/transactions/csv", params={"site_id": villa}).content.decode("utf-8-sig")
    )))
    assert sum(1 for row in rows if "Leaver" in row) == 2
    att = admin.get("/api/reports/attendance/csv", params={"site_id": villa}).content.decode("utf-8-sig")
    assert "Leaver" in att
    pdf = admin.get("/api/reports/business/pdf", params={"site_id": villa})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert stays  # still on the crew


def test_cash_entries_list_one_site(api):
    admin, villa, mall = _two_sites(api)
    ok(_expense(admin, "100", day(0), site_id=villa), 201)
    ok(_expense(admin, "200", day(0), site_id=mall), 201)
    ok(_expense(admin, "300", day(0)), 201)

    villa_rows = ok(admin.get("/contractor/cash-entries", params={"site_id": villa}))
    everything = ok(admin.get("/contractor/cash-entries"))

    assert [Decimal(r["amount"]) for r in villa_rows] == [Decimal("100.00")]
    assert len(everything) == 3


def test_a_site_report_leaves_out_other_sites(api):
    admin, villa, mall = _two_sites(api)
    a = admin.worker("Villa Worker", daily_wage="600", site_id=villa)
    b = admin.worker("Mall Worker", daily_wage="600", site_id=mall)
    ok(admin.mark(a, day(0), "present"), 201)
    ok(admin.mark(b, day(0), "present"), 201)

    text = admin.get("/api/reports/transactions/csv", params={"site_id": villa}).content.decode("utf-8-sig")
    monthly = admin.get(
        "/api/reports/monthly-report", params={"month": day(0).strftime("%Y-%m"), "site_id": villa}
    )

    assert "Villa Worker" in text and "Mall Worker" not in text
    assert monthly.status_code == 200
    assert b"Mall Worker" not in monthly.content


def test_someone_elses_site_is_a_404_on_every_report(api):
    mine, theirs = api.admin("Mine"), api.admin("Theirs")
    their_site = theirs.site("Theirs")

    for path, extra in [
        ("/api/reports/transactions/csv", {}),
        ("/api/reports/attendance/csv", {}),
        ("/api/reports/cash-entries/csv", {}),
        ("/api/reports/business/pdf", {}),
        ("/api/reports/monthly-report", {"month": day(0).strftime("%Y-%m")}),
        ("/contractor/summary", {}),
        ("/contractor/cash-entries", {}),
    ]:
        response = mine.get(path, params={"site_id": their_site, **extra})
        assert response.status_code == 404, path


# --- the crew of a site ---------------------------------------------------------------


def test_the_crew_list_can_be_one_sites_or_the_unposted(api):
    admin, villa, mall = _two_sites(api)
    a = admin.worker("A", site_id=villa)
    b = admin.worker("B", site_id=mall)
    c = admin.worker("C")

    villa_crew = ok(admin.get("/contractor/workers", params={"site_id": villa}))
    unposted = ok(admin.get("/contractor/workers", params={"site_id": 0}))

    assert [w["id"] for w in villa_crew] == [a]
    assert [w["id"] for w in unposted] == [c]
    assert b


def test_the_site_day_view_shows_a_moved_workers_day_where_it_was_worked(api):
    admin, villa, mall = _two_sites(api)
    worker = admin.worker(site_id=villa)
    ok(admin.mark(worker, day(0), "present"), 201)
    ok(admin.patch(f"/contractor/workers/{worker}", json={"site_id": mall}))

    villa_day = ok(admin.get(f"/contractor/sites/{villa}/attendance", params={"date": day(0).isoformat()}))
    mall_day = ok(admin.get(f"/contractor/sites/{mall}/attendance", params={"date": day(0).isoformat()}))

    assert [row["worker_id"] for row in villa_day] == [worker]
    assert mall_day == []


# --- the Home screen's cards -------------------------------------------------------


def test_the_overview_gives_every_site_its_own_numbers(api):
    admin, villa, mall = _two_sites(api)
    a = admin.worker("A", daily_wage="600", site_id=villa)
    b = admin.worker("B", daily_wage="400", site_id=villa)
    c = admin.worker("C", daily_wage="500", site_id=mall)
    ok(admin.mark_bulk([{"worker_id": a, "status": "present"}, {"worker_id": b, "status": "half_day"}], day(0), site_id=villa), 201)
    ok(admin.advance(a, "100", day(0)), 201)
    ok(_expense(admin, "250", day(0), site_id=villa), 201)
    ok(admin.post(f"/contractor/sites/{villa}/petty-cash", json={"amount": "40", "description": "Tea"}), 201)
    ok(admin.deactivate(c), 204)

    cards = {card["site_id"]: card for card in ok(admin.get("/contractor/sites/overview"))}

    villa_card, mall_card = cards[villa], cards[mall]
    assert (villa_card["crew_active"], villa_card["marked_today"]) == (2, 2)
    assert Decimal(villa_card["worked_today"]) == Decimal("1.5")
    assert Decimal(villa_card["wages_earned"]) == Decimal("800.00")
    assert Decimal(villa_card["wages_paid"]) == Decimal("100.00")
    assert Decimal(villa_card["outstanding_payable"]) == Decimal("700.00")
    assert Decimal(villa_card["expenses_paid"]) == Decimal("250.00")
    assert Decimal(villa_card["petty_cash"]) == Decimal("40.00")
    assert Decimal(villa_card["month_spend"]) == Decimal("390.00")  # 100 + 250 + 40
    assert (mall_card["crew_active"], mall_card["crew_inactive"]) == (0, 1)


def test_the_overview_is_admin_only(api, client):
    admin = api.admin()
    site = admin.site()
    supervisor = ok(
        client.post(
            "/auth/register",
            json={
                "name": "Sup",
                "phone": "9444444444",
                "password": "secret12",
                "role": "supervisor",
                "contractor_id": admin.id,
            },
        ),
        201,
    )
    ok(admin.patch(f"/contractor/sites/{site}", json={"supervisor_id": supervisor["user"]["id"]}))
    headers = {"Authorization": f"Bearer {supervisor['access_token']}"}

    assert client.get("/contractor/sites/overview", headers=headers).status_code == 403
    assert client.get("/contractor/sites", headers=headers).status_code == 200


# --- sites cannot be purged out from under their books -------------------------------


def test_a_site_with_money_filed_under_it_cannot_be_purged(api):
    admin, villa, _ = _two_sites(api)
    worker = admin.worker()
    ok(admin.advance(worker, "100", day(0), site_id=villa), 201)

    response = admin.delete(f"/contractor/sites/{villa}", params={"purge": True})

    assert response.status_code == 409
    assert "ledger entries" in response.json()["detail"]


def test_the_audit_catches_a_wage_filed_under_the_wrong_site(api, db):
    admin, villa, mall = _two_sites(api)
    worker = admin.worker(daily_wage="600", site_id=villa)
    ok(admin.mark(worker, day(0), "present"), 201)
    wage = api.attendance(worker, day(0)).transaction
    wage.site_id = mall
    db.commit()

    assert admin.audit()["counts"] == {"wrong_site": 1}
    report = admin.reconcile(apply=True)
    assert report["wages_corrected"] == 1
    assert admin.audit()["consistent"] is True
