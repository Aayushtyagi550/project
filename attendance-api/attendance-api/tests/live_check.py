"""
Checks the analytics pipelines against a REAL MongoDB (6.0+; $setWindowFields / $densify need it).

    docker run -d -p 27017:27017 mongo:7
    MONGO_URI=mongodb://localhost:27017 python tests/live_check.py

It seeds a small, awkward dataset straight into a throw-away database (default: attendance_livecheck, DROPPED at the
start - override with LIVE_CHECK_DB), runs the app in-process and compares every analytics endpoint with values that
were worked out by hand from the business rules R1-R10. Dataset (July 2026; 2026-07-06 is a Monday):

  Eng   EMP0001 (full record set, weekend record, LEAVE, half day, a weekday with no record at all)
        EMP0002 (joins 2026-07-13 = mid-month joiner), EMP0008 (overnight shift 22:00-06:00)
  Sales EMP0003, EMP0004, EMP0005 (EMP0005 has a legacy document without late/overtime/half_day/history)
  Ops   EMP0006 (NO logs at all)          Fut  EMP0007 (joins 2026-08-01, after the month)
  EMP9999 does not exist but has a log (must be ignored)
"""
import datetime as dt
import os
import sys

os.environ["MONGO_DB"] = os.environ.get("LIVE_CHECK_DB", "attendance_livecheck")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402

IST = m.IST


def at(day, hhmm, plus_days=0):
    h, mi = map(int, hhmm.split(":"))
    d = dt.datetime.fromisoformat(day).replace(hour=h, minute=mi, tzinfo=IST) + dt.timedelta(days=plus_days)
    return d.astimezone(dt.timezone.utc)


def emp(code, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Name {code[-1]}", "email": f"{code}@x.io", "department": dept, "shift_start": start,
            "shift_end": end, "joined_on": joined, "created_at": at(joined, "10:00")}


def log(code, day, status, pin=None, pout=None, wh=None, late=0, ot=0, half=False, out_next_day=False, legacy=False):
    doc = {"emp_code": code, "date": day, "status": status,
           "punch_in": at(day, pin) if pin else None, "punch_out": at(day, pout, 1 if out_next_day else 0) if pout else None,
           "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}
    if legacy:
        for k in ("late_minutes", "overtime_minutes", "half_day", "history"):
            del doc[k]
    return doc


EMPLOYEES = [
    emp("EMP0001", "Eng", "2026-01-05"), emp("EMP0002", "Eng", "2026-07-13"),
    emp("EMP0003", "Sales", "2026-01-05"), emp("EMP0004", "Sales", "2026-01-05"), emp("EMP0005", "Sales", "2026-01-05"),
    emp("EMP0006", "Ops", "2026-01-05"), emp("EMP0007", "Fut", "2026-08-01"),
    emp("EMP0008", "Eng", "2026-01-05", "22:00", "06:00"),
]
LOGS = [
    log("EMP0001", "2026-07-06", "PRESENT", "10:05", "19:10", 9.08, 35, 40),
    log("EMP0001", "2026-07-07", "WFH", "09:30", "13:00", 3.5, 0, 0, True),
    log("EMP0001", "2026-07-08", "LEAVE"),
    log("EMP0001", "2026-07-11", "PRESENT", "09:50", "14:50", 5.0, 20, 0),  # Saturday
    log("EMP0002", "2026-07-13", "PRESENT", "09:30", "18:30", 9.0),
    log("EMP0002", "2026-07-14", "ON_DUTY", "09:45", "17:45", 8.0, 15),
    log("EMP0003", "2026-07-06", "PRESENT", "10:00", "18:00", 8.0, 30),
    log("EMP0003", "2026-07-07", "PRESENT", "10:00", "19:00", 9.0, 30),
    log("EMP0003", "2026-07-08", "ABSENT"),
    log("EMP0004", "2026-07-06", "PRESENT", "10:00", "17:30", 7.5, 30),
    log("EMP0004", "2026-07-07", "PRESENT", "09:55", "18:25", 8.5, 25),
    log("EMP0005", "2026-07-06", "PRESENT", "09:50", "17:50", 8.0, 20),
    log("EMP0005", "2026-07-08", "PRESENT", "09:30", "17:30", 8.0, legacy=True),
    log("EMP0008", "2026-07-06", "PRESENT", "22:15", "06:15", 8.0, 15, 0, out_next_day=True),
    log("EMP9999", "2026-07-06", "PRESENT", "10:00", "18:00", 8.0, 500),  # orphan: no such employee
]

failures = []


def check(name, got, expected):
    ok = got == expected
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        failures.append(name)
        print("   expected:", expected)
        print("   got     :", got)


def main():
    m.client.drop_database(m.MONGO_DB)
    with TestClient(m.app) as api:  # startup creates the indexes
        m.db.employees.insert_many(EMPLOYEES)
        m.db.attendance_logs.insert_many(LOGS)
        get = lambda url, **p: api.get(url, params=p)

        # ---- employee monthly -------------------------------------------------------------------
        def monthly(code, **kw):
            base = dict(emp_code=code, month="2026-07", leave_days=0, late_count=0, total_late_minutes=0,
                        total_overtime_minutes=0)
            return {**base, **kw}
        check("monthly EMP0001", get("/analytics/employees/EMP0001/monthly", month="2026-07").json(),
              monthly("EMP0001", working_days=23, present_days=1.5, leave_days=1, late_count=2, total_late_minutes=55,
                      total_overtime_minutes=40, attendance_pct=6.52))
        check("monthly EMP0002 (mid-month joiner)", get("/analytics/employees/EMP0002/monthly", month="2026-07").json(),
              monthly("EMP0002", working_days=15, present_days=2.0, late_count=1, total_late_minutes=15, attendance_pct=13.33))
        check("monthly EMP0006 (no logs)", get("/analytics/employees/EMP0006/monthly", month="2026-07").json(),
              monthly("EMP0006", working_days=23, present_days=0.0, attendance_pct=0.0))
        check("monthly EMP0007 (joins later -> pct null)", get("/analytics/employees/EMP0007/monthly", month="2026-07").json(),
              monthly("EMP0007", working_days=0, present_days=0.0, attendance_pct=None))
        check("monthly EMP0008 (overnight)", get("/analytics/employees/EMP0008/monthly", month="2026-07").json(),
              monthly("EMP0008", working_days=23, present_days=1.0, late_count=1, total_late_minutes=15, attendance_pct=4.35))

        # ---- department summary -----------------------------------------------------------------
        zero = dict(late_count=0, total_late_minutes=0, leave_count=0, on_duty_count=0)
        check("summary July", get("/analytics/departments/summary", month="2026-07").json(), {"month": "2026-07", "items": [
            dict(department="Eng", headcount=3, present_days=4.5, avg_work_hours=7.1, late_count=4, total_late_minutes=85,
                 leave_count=1, on_duty_count=1),
            dict(department="Ops", headcount=1, present_days=0.0, avg_work_hours=None, **zero),
            dict(department="Sales", headcount=3, present_days=6.0, avg_work_hours=8.17, late_count=5,
                 total_late_minutes=135, leave_count=0, on_duty_count=0)]})
        check("summary June (EMP0002 not yet joined, no logs)", [(i["department"], i["headcount"]) for i in
              get("/analytics/departments/summary", month="2026-06").json()["items"]],
              [("Eng", 2), ("Ops", 1), ("Sales", 3)])
        check("summary department filter", [i["department"] for i in
              get("/analytics/departments/summary", month="2026-07", department="Sales").json()["items"]], ["Sales"])

        # ---- late leaderboard -------------------------------------------------------------------
        def board(**p):
            return [(r["rank"], r["emp_code"], r["total_late_minutes"], r["late_count"])
                    for r in get("/analytics/leaderboard/late", month="2026-07", **p).json()["items"]]
        full = [(1, "EMP0003", 60, 2), (2, "EMP0001", 55, 2), (2, "EMP0004", 55, 2), (4, "EMP0005", 20, 1),
                (5, "EMP0002", 15, 1), (5, "EMP0008", 15, 1)]
        check("leaderboard limit=1", board(limit=1), full[:1])
        check("leaderboard limit=2 keeps the whole tie at rank 2", board(limit=2), full[:3])
        check("leaderboard limit=3 (rank 3 does not exist)", board(limit=3), full[:3])
        check("leaderboard limit=4", board(limit=4), full[:4])
        check("leaderboard limit=5 keeps the tie at rank 5", board(limit=5), full)
        check("leaderboard Sales only: ranked inside the department", board(department="Sales"),
              [(1, "EMP0003", 60, 2), (2, "EMP0004", 55, 2), (3, "EMP0005", 20, 1)])
        check("leaderboard Eng only", board(department="Eng"),
              [(1, "EMP0001", 55, 2), (2, "EMP0002", 15, 1), (2, "EMP0008", 15, 1)])
        check("leaderboard empty month", get("/analytics/leaderboard/late", month="2026-06").json(),
              {"month": "2026-06", "items": []})
        row = get("/analytics/leaderboard/late", month="2026-07", limit=1).json()["items"][0]
        check("leaderboard row shape", row, dict(rank=1, emp_code="EMP0003", name="Name 3", department="Sales",
                                                  total_late_minutes=60, late_count=2))

        # ---- department trend -------------------------------------------------------------------
        def trend(dept, a, b):
            r = get(f"/analytics/departments/{dept}/trend", **{"from": a, "to": b})
            return r.status_code, [(i["date"], i["is_working_day"], i["headcount"], i["present_count"], i["late_count"],
                                    i["attendance_rate"], i["moving_avg_7d"]) for i in r.json().get("items", [])]
        check("trend Eng 07-06..07-14", trend("Eng", "2026-07-06", "2026-07-14"), (200, [
            ("2026-07-06", True, 2, 2.0, 2, 1.0, 1.0), ("2026-07-07", True, 2, 0.5, 0, 0.25, 0.625),
            ("2026-07-08", True, 2, 0.0, 0, 0.0, 0.4167), ("2026-07-09", True, 2, 0.0, 0, 0.0, 0.3125),
            ("2026-07-10", True, 2, 0.0, 0, 0.0, 0.25), ("2026-07-11", False, 2, 1.0, 1, None, 0.25),
            ("2026-07-12", False, 2, 0.0, 0, None, 0.25), ("2026-07-13", True, 3, 1.0, 0, 0.3333, 0.1167),
            ("2026-07-14", True, 3, 1.0, 1, 0.3333, 0.1333)]))
        check("trend Ops, no logs at all (rows still generated)", trend("Ops", "2026-07-06", "2026-07-08"), (200, [
            ("2026-07-06", True, 1, 0.0, 0, 0.0, 0.0), ("2026-07-07", True, 1, 0.0, 0, 0.0, 0.0),
            ("2026-07-08", True, 1, 0.0, 0, 0.0, 0.0)]))
        status, rows = trend("Fut", "2026-07-01", "2026-07-10")
        check("trend Fut: nobody joined yet -> 10 rows, rate/avg null", (status, len(rows), {r[5] for r in rows},
              {r[6] for r in rows}, {r[2] for r in rows}), (200, 10, {None}, {None}, {0}))
        check("trend 404 unknown department", trend("Nope", "2026-07-01", "2026-07-02")[0], 404)
        check("trend 422 to < from", trend("Eng", "2026-07-02", "2026-07-01")[0], 422)
        check("trend 92 days ok / 93 days 422", (trend("Eng", "2026-04-01", "2026-07-01")[0],
              trend("Eng", "2026-04-01", "2026-07-02")[0]), (200, 422))
        check("trend 92 days -> exactly 92 rows", len(trend("Eng", "2026-04-01", "2026-07-01")[1]), 92)

        # ---- explain: IXSCAN and no COLLSCAN ----------------------------------------------------
        for name, params in {
            "attendance_list": {}, "attendance_list ": {"emp_code": "EMP0001", "date_from": "2026-07-01", "status": "PRESENT"},
            "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"}, "department_summary": {"month": "2026-07"},
            "late_leaderboard": {"month": "2026-07", "limit": 3},
            "department_trend": {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"},
        }.items():
            r = api.get(f"/admin/explain/{name.strip()}", params=params)
            text = r.text
            check(f"explain {name.strip()} {params or ''}", (r.status_code, "IXSCAN" in text, "COLLSCAN" in text),
                  (200, True, False))
        check("explain 422 on missing params", api.get("/admin/explain/employee_monthly").status_code, 422)

    m.client.drop_database(m.MONGO_DB)
    print("\n%s" % ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
