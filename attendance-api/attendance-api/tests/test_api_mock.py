"""Endpoint tests that run WITHOUT a MongoDB server (mongomock). They cover CRUD, status codes, races and the
history trail. Aggregation pipelines ($setWindowFields, $densify, ...) are not supported by mongomock and are
therefore NOT covered here - run them against a real MongoDB (see README)."""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as m

IST = m.IST


def ms(y, mo, d, h, mi=0, s=0, frac=0):
    return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + frac


@pytest.fixture()
def api(monkeypatch):
    mock_client = mongomock.MongoClient(tz_aware=True)
    monkeypatch.setattr(m, "client", mock_client)
    monkeypatch.setattr(m, "db", mock_client["t"])
    m.ensure_indexes()
    return TestClient(m.app)


def emp(api, code="EMP0001", **kw):
    body = {"emp_code": code, "name": "A", "email": "a@x.io", "department": "Eng", "joined_on": "2026-01-05", **kw}
    return api.post("/employees", json=body)


def test_employee_create_and_validation(api):
    r = emp(api)
    assert r.status_code == 201
    b = r.json()
    assert "_id" not in b and b["shift_start"] == "09:30" and isinstance(b["created_at"], int)
    assert emp(api).status_code == 409
    for bad in ({"emp_code": "E1"}, {"emp_code": "EMP12345678"}, {"email": "nope"}, {"shift_start": "9:30"},
                {"shift_start": "10:00", "shift_end": "10:00"}, {"joined_on": "2026-02-30"}, {"joined_on": "06-07-2026"},
                {"department": ""}, {"name": ""}, {"shift_end": "24:00"}):
        assert emp(api, code=bad.get("emp_code", "EMP0002"), **{k: v for k, v in bad.items() if k != "emp_code"}).status_code == 422, bad
    assert api.post("/employees", json={"emp_code": "EMP0003"}).status_code == 422


def test_employee_list_pagination_and_total(api):
    for i in range(1, 6):
        emp(api, f"EMP000{i}", department="A" if i % 2 else "B")
    r = api.get("/employees", params={"page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0004"] and r["total"] == 5
    r = api.get("/employees", params={"department": "A"}).json()
    assert r["total"] == 3 and [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0003", "EMP0005"]
    for p in ({"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": -1}):
        assert api.get("/employees", params=p).status_code == 422


def test_punch_in_rules(api):
    emp(api)
    t = ms(2026, 7, 6, 10, 15, 59, frac=900)  # fractional ms are truncated
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t})
    assert r.status_code == 201
    b = r.json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 45 and b["punch_in"] == t - 900
    assert b["punch_out"] is None and b["work_hours"] is None and b["history"] == [] and "id" not in b
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t}).status_code == 409
    assert api.post("/attendance/punch-in", json={"emp_code": "NOPE", "punched_at": t}).status_code == 404
    for bad in ({"punched_at": t // 1000}, {"punched_at": float(t)}, {"punched_at": str(t)}, {"punched_at": 0},
                {"punched_at": t, "status": "ABSENT"}, {"punched_at": t, "status": "BOGUS"}, {"punched_at": True}):
        assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", **bad}).status_code == 422, bad


def test_punch_in_late_boundary(api):
    emp(api)
    for day, (h, mi, s), late in ((6, (9, 40, 0), 0), (7, (9, 40, 1), 10), (8, (9, 40, 59), 10)):
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, day, h, mi, s)})
        assert r.json()["late_minutes"] == late


def test_punch_in_race_one_winner(api):
    emp(api)
    t = ms(2026, 7, 6, 9, 30)
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t}).status_code, range(16)))
    assert sorted(codes) == [201] + [409] * 15


def test_overnight_punch_flow(api):
    emp(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)}).json()
    assert r["date"] == "2026-07-06" and r["late_minutes"] == 0
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)})
    assert r.status_code == 200
    b = r.json()
    assert b["work_hours"] == 8.75 and b["overtime_minutes"] == 40 and b["half_day"] is False and b["date"] == "2026-07-06"
    # punch-in after midnight belongs to the previous day's shift
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 8, 1, 0)}).json()
    assert r["date"] == "2026-07-07" and r["late_minutes"] == 180


def test_punch_out_errors_and_race(api):
    emp(api)
    emp(api, "EMP0002")
    assert api.post("/attendance/punch-out", json={"emp_code": "NOPE"}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ms(2026, 7, 6, 18)}).status_code == 404
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 31)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 29)}).status_code == 404
    t = ms(2026, 7, 6, 19, 10)
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(lambda _: api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t}), range(12)))
    assert sorted(r.status_code for r in res) == [200] + [409] * 11
    ok = next(r for r in res if r.status_code == 200).json()
    assert ok["work_hours"] == 9.67 and ok["overtime_minutes"] == 40 and ok["history"] == []


def test_half_day_on_punch_out(api):
    emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30), "status": "WFH"})
    b = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 13, 0)}).json()
    assert b["work_hours"] == 3.5 and b["half_day"] is True and b["status"] == "WFH"


def test_list_attendance(api):
    emp(api); emp(api, "EMP0002")
    for d in (6, 7, 8):
        for code in ("EMP0002", "EMP0001"):
            api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2026, 7, d, 9, 30)})
    r = api.get("/attendance", params={"page_size": 4}).json()
    assert r["total"] == 6
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002")]
    r = api.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
    assert r["total"] == 1 and r["items"][0]["date"] == "2026-07-07"
    assert api.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-07"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "2026-7-7"}).status_code == 422
    assert api.get("/attendance", params={"status": "NOPE"}).status_code == 422
    assert api.get("/attendance", params={"status": "ABSENT"}).json()["total"] == 0


def test_legacy_documents_are_read_with_defaults(api):
    emp(api)
    m.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": ms_to_dt(1783312500000), "punch_out": ms_to_dt(1783312500000 + 32400000), "work_hours": 8.98, "late_minutes": 0})
    b = api.get("/attendance").json()["items"][0]
    assert b["history"] == [] and b["half_day"] is False and b["overtime_minutes"] == 0
    # ...and a regularization works on it (history field missing)
    r = api.patch("/attendance/EMP0001/2026-07-14", json={"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr"})
    assert r.status_code == 200 and len(r.json()["history"]) == 1


def ms_to_dt(x):
    return m.ms_to_dt(x)


def test_regularize_flow(api):
    emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    url = "/attendance/EMP0001/2026-07-07"
    body = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    # correct punch-in and add a punch-out in one go
    r = api.patch(url, json={**body, "punch_in": ms(2026, 7, 7, 9, 28), "punch_out": ms(2026, 7, 7, 18, 30), "work_hours": 99})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["late_minutes"] == 0 and b["work_hours"] == 9.03 and b["overtime_minutes"] == 0 and len(b["history"]) == 1
    ch = b["history"][0]["changes"]
    assert set(ch) == {"punch_in", "punch_out", "late_minutes", "work_hours"}  # only what actually changed
    assert ch["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    assert ch["late_minutes"] == {"from": 35, "to": 0} and ch["work_hours"] == {"from": None, "to": 9.03}
    assert b["history"][0]["by"] == "hr.admin" and isinstance(b["history"][0]["at"], int)
    # no-op
    assert api.patch(url, json={**body, "punch_in": ms(2026, 7, 7, 9, 28)}).status_code == 422
    assert api.patch(url, json=body).status_code == 422
    # second correction appends, never overwrites
    r = api.patch(url, json={**body, "punch_out": ms(2026, 7, 7, 19, 10)}).json()
    assert len(r["history"]) == 2 and r["overtime_minutes"] == 40 and r["history"][0] == b["history"][0]
    # status -> ABSENT clears punches and derived fields
    r = api.patch(url, json={**body, "status": "ABSENT"}).json()
    assert r["punch_in"] is None and r["punch_out"] is None and r["work_hours"] is None
    assert r["late_minutes"] == 0 and r["overtime_minutes"] == 0 and len(r["history"]) == 3
    # ABSENT + punch times is a contradiction; PRESENT again needs a punch_in
    assert api.patch(url, json={**body, "status": "LEAVE", "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422
    assert api.patch(url, json={**body, "status": "PRESENT"}).status_code == 422
    assert api.patch(url, json={**body, "status": "ON_DUTY", "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 200


def test_regularize_validation(api):
    emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    url = "/attendance/EMP0001/2026-07-07"
    body = {"reason": "biometric glitch", "regularized_by": "hr"}
    assert api.patch(url, json={**body, "punch_in": ms(2026, 7, 8, 9, 0)}).status_code == 422   # off the date
    assert api.patch(url, json={**body, "punch_out": ms(2026, 7, 7, 10, 5)}).status_code == 422   # not after
    assert api.patch(url, json={**body, "punch_out": ms(2026, 7, 8, 10, 6)}).status_code == 422   # > 24h
    assert api.patch(url, json={**body, "punch_in": ms(2026, 7, 7, 23, 0)}).status_code == 200
    assert api.patch(url, json={"reason": "abc", "regularized_by": "hr", "status": "LEAVE"}).status_code == 422
    assert api.patch(url, json={**body, "status": "LEAVE", "punch_in": None}).status_code == 422  # explicit null
    assert api.patch(url, json={**body, "punch_in": 1783312500}).status_code == 422                # seconds
    assert api.patch("/attendance/NOPE/2026-07-07", json={**body, "status": "LEAVE"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-07-09", json={**body, "status": "LEAVE"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-13-09", json={**body, "status": "LEAVE"}).status_code == 422


def test_regularize_overnight_date_rule(api):
    emp(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)})
    body = {"reason": "fix the time", "regularized_by": "hr"}
    # 03:00 on the 7th still belongs to the shift of the 6th; 07:00 on the 7th belongs to the 7th
    assert api.patch("/attendance/EMP0005/2026-07-06", json={**body, "punch_in": ms(2026, 7, 7, 3, 0)}).status_code == 200
    assert api.patch("/attendance/EMP0005/2026-07-06", json={**body, "punch_in": ms(2026, 7, 7, 7, 0)}).status_code == 422


def test_concurrent_regularizations_never_lose_history(api):
    emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    url = "/attendance/EMP0001/2026-07-07"

    def patch(i):
        return api.patch(url, json={"reason": f"reason {i}", "regularized_by": "hr", "punch_in": ms(2026, 7, 7, 9, 0, i)})

    with ThreadPoolExecutor(10) as ex:
        res = list(ex.map(patch, range(1, 11)))
    wins = [r for r in res if r.status_code == 200]
    assert wins and all(r.status_code in (200, 409) for r in res)
    final = api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert len(final["history"]) == len(wins)


def test_health_and_unknown_explain_endpoint(api):
    assert api.get("/health").status_code in (200, 503)
    assert api.get("/admin/explain/nope").status_code == 422
    assert api.get("/admin/explain/employee_monthly").status_code == 422  # missing emp_code / month
    assert api.get("/admin/explain/department_trend", params={"department": "X"}).status_code == 422


def test_analytics_validation_paths(api):
    emp(api)
    assert api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422
    assert api.get("/analytics/employees/EMP0001/monthly").status_code == 422
    assert api.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code == 404
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 0}).status_code == 422
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422
    assert api.get("/analytics/departments/summary", params={"month": "July"}).status_code == 422
    t = "/analytics/departments/Eng/trend"
    assert api.get(t, params={"from": "2026-07-10", "to": "2026-07-01"}).status_code == 422
    assert api.get(t, params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422   # 93 days
    assert api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-05"}).status_code == 404
    assert api.get(t, params={"from": "2026-07-01"}).status_code == 422
