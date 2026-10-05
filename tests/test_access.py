"""Who may touch whose books. Every miss is a 404, never a 403."""

from tests.helpers import day, ok


def test_nobody_can_sign_up_onto_someone_elses_crew(api, client):
    admin = api.admin()

    response = client.post(
        "/auth/register",
        json={
            "name": "Intruder",
            "phone": "9111111111",
            "password": "secret12",
            "role": "worker",
            "contractor_id": admin.id,
        },
    )

    assert response.status_code == 422
    crew = ok(admin.get("/contractor/workers", params={"include_inactive": True}))
    assert crew == []


def test_a_worker_can_still_sign_up_on_their_own(client):
    response = client.post(
        "/auth/register",
        json={"name": "Solo", "phone": "9222222222", "password": "secret12", "role": "worker"},
    )

    assert response.status_code == 201
    assert response.json()["user"]["contractor_id"] is None


def test_another_contractors_worker_is_a_404_on_every_money_route(api):
    mine, theirs = api.admin("Mine"), api.admin("Theirs")
    their_worker = theirs.worker(daily_wage="600")
    ok(theirs.mark(their_worker, day(0), "present"), 201)
    their_day = theirs.attendance_id(their_worker, day(0))

    responses = [
        mine.mark(their_worker, day(-1), "present"),
        mine.advance(their_worker, "100", day(0)),
        mine.get(f"/contractor/workers/{their_worker}/ledger"),
        mine.correct(their_day, status="absent"),
        mine.unmark(their_day),
        mine.settle(their_worker),
        mine.get("/payroll/preview", params={"worker_id": their_worker}),
        mine.put(f"/contractor/workers/{their_worker}", json={"name": "Mine now"}),
        mine.deactivate(their_worker),
    ]

    assert [r.status_code for r in responses] == [404] * len(responses)
    assert api.wages(their_worker) == api.oracle_attendance_wages(their_worker)


def test_the_integrity_report_only_covers_your_own_crew(api, db):
    from app.models.attendance import Attendance
    from app.models.enums import AttendanceStatus

    mine, theirs = api.admin("Mine"), api.admin("Theirs")
    their_worker = theirs.worker(daily_wage="600")
    db.add(Attendance(worker_id=their_worker, date=day(-1), status=AttendanceStatus.PRESENT))
    db.commit()

    assert mine.audit()["issues"] == []
    assert theirs.audit()["counts"] == {"unpriced": 1}


def test_a_worker_cannot_reach_contractor_routes(api, client):
    admin = api.admin()
    ok(
        admin.post(
            "/contractor/workers",
            json={"name": "Has Login", "phone": "9333333333", "password": "pin123"},
        ),
        201,
    )
    token = ok(client.post("/auth/login", json={"phone": "9333333333", "password": "pin123"}))
    headers = {"Authorization": f"Bearer {token['access_token']}"}

    assert client.get("/contractor/integrity/wages", headers=headers).status_code == 403
    assert client.get("/contractor/summary", headers=headers).status_code == 403
    assert client.get("/worker/ledger", headers=headers).status_code == 200
