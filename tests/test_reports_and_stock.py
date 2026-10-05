"""Downloads that used to crash, and stock that must not split by case."""

from decimal import Decimal

from sqlalchemy import select

from app.models.inventory import SiteInventory
from tests.helpers import day, ok


def _crew_with_a_rain_out(api):
    admin = api.admin()
    site = admin.site()
    worker = admin.worker(daily_wage="600", site_id=site)
    ok(admin.mark(worker, day(-1), "present"), 201)
    ok(admin.rain_out(site, day(0)))
    return admin, worker


def test_the_business_report_survives_a_rained_out_day(api):
    """Regression: the report's day-weight table had no rain_out key, so the
    first rained-out day in the period turned the PDF into a 500."""
    admin, _ = _crew_with_a_rain_out(api)

    response = admin.get("/api/reports/business/pdf")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_the_worker_statement_survives_a_rained_out_day(api):
    """Regression: same missing key, in the statement's label table."""
    admin, worker = _crew_with_a_rain_out(api)

    response = admin.get(f"/api/reports/worker/{worker}/pdf")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_csv_exports_keep_their_byte_order_mark(api):
    """The app refuses a 'CSV' that does not start with the UTF-8 BOM - that is
    how a JSON error body is kept out of the share sheet."""
    admin, _ = _crew_with_a_rain_out(api)

    for path in (
        "/api/reports/transactions/csv",
        "/api/reports/attendance/csv",
        "/api/reports/cash-entries/csv",
    ):
        response = admin.get(path)
        assert response.status_code == 200, path
        assert response.content.startswith(b"\xef\xbb\xbf"), path

    monthly = admin.get("/api/reports/monthly-report", params={"month": day(0).strftime("%Y-%m")})
    assert monthly.status_code == 200
    assert monthly.content.startswith(b"\xef\xbb\xbf")


def _receive(admin, site, item, quantity, unit="bag"):
    return admin.post(
        f"/contractor/sites/{site}/material-receipts",
        data={"item_name": item, "quantity_received": str(quantity), "unit": unit},
    )


def test_stock_is_one_row_whatever_the_case_it_is_typed_in(api, db):
    """MySQL's collation made 'Cement' and 'cement' one item. PostgreSQL does
    not, and two rows would each hold part of the count."""
    admin = api.admin()
    site = admin.site()

    ok(_receive(admin, site, "Cement", 10), 201)
    second = ok(_receive(admin, site, "cement", 5), 201)
    ok(_receive(admin, site, "  CEMENT ", 2.5), 201)

    rows = db.scalars(select(SiteInventory).where(SiteInventory.site_id == site)).all()
    assert len(rows) == 1
    assert rows[0].quantity_in_stock == Decimal("17.500")
    assert Decimal(second["quantity_in_stock"]) == Decimal("15.000")


def test_a_delivery_in_a_different_unit_is_refused_not_converted(api):
    admin = api.admin()
    site = admin.site()
    ok(_receive(admin, site, "Sand", 2, unit="brass"), 201)

    response = _receive(admin, site, "sand", 100, unit="kg")

    assert response.status_code == 409
