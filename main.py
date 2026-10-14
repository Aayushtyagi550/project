"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real environment variables win)

Layout of this file
  1. configuration & database handle
  2. time helpers      - everything works on integer epoch seconds; IST is a fixed +05:30 offset
  3. business rules    - R1..R5 as small pure functions (unit-testable without a database)
  4. validation models and request helpers
  5. indexes & startup
  6. endpoints         - employees, attendance, regularization
  7. analytics         - pipeline builders + endpoints
  8. admin/explain     - re-uses the very same builders as the endpoints
"""
import calendar
import datetime as dt
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # never overrides variables that are already set in the real environment

log = logging.getLogger("attendance")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo hands back UTC-aware datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

# --------------------------------------------------------------------------- #
# 2. Constants and time helpers
# --------------------------------------------------------------------------- #
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc
EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
NON_PRESENCE_STATUSES = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_SHIFT_SECONDS = 24 * 3600
MAX_TREND_DAYS = 92
MONGO_WEEKDAYS = [2, 3, 4, 5, 6]  # $dayOfWeek: 1 = Sunday ... 7 = Saturday  ->  Mon..Fri

MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def ms_to_dt(ms: int) -> dt.datetime:
    """Epoch milliseconds -> aware UTC datetime (what we store in MongoDB)."""
    return EPOCH + dt.timedelta(milliseconds=ms)


def dt_to_ms(value: Optional[dt.datetime]) -> Optional[int]:
    """Datetime from MongoDB -> epoch milliseconds. Naive values are treated as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // dt.timedelta(milliseconds=1)


def ms_to_seconds(ms: int) -> int:
    """R1: instants are truncated to whole seconds before any calculation."""
    return ms // 1000


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def ist_instant(day: dt.date, hhmm: str, plus_days: int = 0) -> int:
    """Epoch seconds of `hhmm` IST on `day` (+ plus_days)."""
    hour, minute = parse_hhmm(hhmm)
    local = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)
    return int((local + dt.timedelta(days=plus_days)).timestamp())


# --------------------------------------------------------------------------- #
# 3. Business rules R1 - R5 (pure functions)
# --------------------------------------------------------------------------- #
def attendance_date(punch_in_s: int, shift_start: str, shift_end: str) -> dt.date:
    """R1: the IST calendar date of the punch-in; for overnight shifts a punch-in earlier than
    shift_end (IST time of day) belongs to the shift that started the previous day."""
    local = dt.datetime.fromtimestamp(punch_in_s, tz=IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        end_h, end_m = parse_hhmm(shift_end)
        if (local.hour, local.minute, local.second) < (end_h, end_m, 0):
            day -= dt.timedelta(days=1)
    return day


def compute_late_minutes(punch_in_s: int, day: dt.date, shift_start: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift_start; then floor-minutes since shift_start."""
    diff = punch_in_s - ist_instant(day, shift_start)
    return diff // 60 if diff > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out_s: int, day: dt.date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift_end, counted only when at least 30. Overnight shifts end the next day."""
    end_s = ist_instant(day, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    minutes = (punch_out_s - end_s) // 60  # floor, also for negative values
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in_s: int, punch_out_s: int) -> Decimal:
    """R4: seconds / 3600, 2 decimals, ROUND_HALF_UP (exact Decimal arithmetic, no float rounding)."""
    return (Decimal(punch_out_s - punch_in_s) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derive_fields(
    status: str, punch_in_s: Optional[int], punch_out_s: Optional[int], day: dt.date, shift_start: str, shift_end: str
) -> dict:
    """All derived fields for a record, following the table in DATA_MODEL.md."""
    if status not in PRESENCE_STATUSES or punch_in_s is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in_s, day, shift_start)
    if punch_out_s is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in_s, punch_out_s)
    return {
        "work_hours": float(hours),
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out_s, day, shift_start, shift_end),
        "half_day": hours < HALF_DAY_LIMIT,  # R5 uses the ROUNDED work_hours
    }


def round_half_up(value: Any, places: int) -> Decimal:
    """R8 for numbers computed in Python."""
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def count_weekdays(first: dt.date, last: dt.date) -> int:
    """Mon-Fri days in [first, last] (R7); 0 when the range is empty."""
    if last < first:
        return 0
    days = (last - first).days + 1
    weeks, rest = divmod(days, 7)
    total = weeks * 5
    total += sum(1 for i in range(rest) if (first.weekday() + i) % 7 < 5)
    return total


# --------------------------------------------------------------------------- #
# 4. Validation models and request helpers
# --------------------------------------------------------------------------- #
def unprocessable(loc: list, msg: str) -> HTTPException:
    """422 with the same body shape FastAPI uses for its own validation errors."""
    return HTTPException(422, detail=[{"type": "value_error", "loc": loc, "msg": msg}])


def parse_date_str(value: str, loc: list) -> dt.date:
    if not DATE_RE.match(value):
        raise unprocessable(loc, "must be a date in YYYY-MM-DD format")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise unprocessable(loc, "not a valid calendar date")


def parse_month(value: str, loc: list) -> tuple[dt.date, dt.date]:
    """'YYYY-MM' -> (first day, last day)."""
    if not re.match(MONTH_PATTERN, value):
        raise unprocessable(loc, "must be a month in YYYY-MM format")
    year, month = int(value[:4]), int(value[5:])
    if year < 1:
        raise unprocessable(loc, "year out of range")
    return dt.date(year, month, 1), dt.date(year, month, calendar.monthrange(year, month)[1])


def require(value: Any, name: str) -> Any:
    if value is None:
        raise unprocessable(["query", name], f"{name} is required for this endpoint")
    return value


# strict=True: floats such as 1.7e12 and strings are rejected instead of being coerced; ge/le reject seconds.
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, value: str) -> str:
        if not DATE_RE.match(value):
            raise ValueError("must be a date in YYYY-MM-DD format")
        dt.date.fromisoformat(value)  # ValueError -> 422
        return value

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # The contract says "omitted = unchanged"; an explicit null is not a valid value.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null; omit it to leave it unchanged")
        return self


# ---- output shaping (MongoDB document -> API JSON) ------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


HISTORY_TIME_FIELDS = ("punch_in", "punch_out")
CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def history_out(entry: dict) -> dict:
    changes = {}
    for key, change in (entry.get("changes") or {}).items():
        convert = dt_to_ms if key in HISTORY_TIME_FIELDS else (lambda v: v)
        changes[key] = {"from": convert(change.get("from")), "to": convert(change.get("to"))}
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Missing legacy fields are read as defaults (late/overtime 0, half_day false, history [])."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 5. Indexes and startup
# --------------------------------------------------------------------------- #
indexes_ready = False


def _create_index(collection, keys, unique: bool = False) -> None:
    try:
        collection.create_index(keys, unique=unique)
    except OperationFailure:
        if not unique:
            raise
        # The seeded data violates uniqueness. Never refuse to start because of it: fall back to a plain index.
        log.warning("could not build unique index %s on %s; falling back to non-unique", keys, collection.name)
        collection.create_index(keys)


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    global indexes_ready
    emp, logs = db.employees, db.attendance_logs
    # employees
    _create_index(emp, [("emp_code", ASCENDING)], unique=True)  # identity; POST /employees race guard
    _create_index(emp, [("department", ASCENDING), ("emp_code", ASCENDING)])  # GET /employees?department=
    _create_index(emp, [("department", ASCENDING), ("joined_on", ASCENDING)])  # headcount per department / day
    _create_index(emp, [("joined_on", ASCENDING)])  # department summary without a department filter
    # attendance_logs
    _create_index(logs, [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)  # one record per day; punch-in race
    _create_index(logs, [("date", DESCENDING), ("emp_code", ASCENDING)])  # list sort order, date-range scans
    indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # MongoDB not reachable yet: /health reports 503 and retries
        log.warning("index creation deferred: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not available")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 6. Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = ms_to_dt(now_ms())
    try:  # uniqueness is enforced by the unique index, not by a check-then-insert
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {} if department is None else {"department": department}
    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    items = []
    if skip < 2**62:
        cursor = db.employees.find(query).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = [employee_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 6. Attendance: punch-in, punch-out, list, regularize
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    punch_in_s = ms_to_seconds(body.punched_at if body.punched_at is not None else now_ms())
    day = attendance_date(punch_in_s, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ms_to_dt(punch_in_s * 1000),  # stored truncated to whole seconds
        "punch_out": None,
        **derive_fields(body.status, punch_in_s, None, day, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:  # the unique (emp_code, date) index decides the race: exactly one insert wins
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    punch_out_s = ms_to_seconds(body.punched_at if body.punched_at is not None else now_ms())
    out_dt = ms_to_dt(punch_out_s * 1000)

    # Most recent record whose punch_in <= punched_at. (ABSENT/LEAVE have no punch_in and never match.)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_dt}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found for this employee")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_s = ms_to_seconds(dt_to_ms(rec["punch_in"]))
    if punch_out_s <= punch_in_s:
        raise unprocessable(["body", "punched_at"], "punched_at must be after the punch-in")
    if punch_out_s - punch_in_s > MAX_SHIFT_SECONDS:
        raise unprocessable(["body", "punched_at"], "punched_at is more than 24 hours after the punch-in")

    day = dt.date.fromisoformat(rec["date"])
    derived = derive_fields(rec["status"], punch_in_s, punch_out_s, day, emp["shift_start"], emp["shift_end"])
    # Compare-and-set on punch_out: of two simultaneous punch-outs only one matches the filter.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": out_dt, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def build_attendance_find(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str],
    page: int, page_size: int,
) -> dict:
    """The one place the attendance list query is built (used by the endpoint AND by /admin/explain)."""
    first = parse_date_str(date_from, ["query", "date_from"]) if date_from is not None else None
    last = parse_date_str(date_to, ["query", "date_to"]) if date_to is not None else None
    if first and last and first > last:
        raise unprocessable(["query", "date_from"], "date_from must not be after date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if first or last:
        query["date"] = {}
        if first:
            query["date"]["$gte"] = date_from
        if last:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return {"filter": query, "sort": ATTENDANCE_SORT, "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"])
    items = []
    if spec["skip"] < 2**62:
        cursor = db.attendance_logs.find(spec["filter"]).sort(spec["sort"]).skip(spec["skip"]).limit(spec["limit"])
        items = [record_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _bson_change_value(field: str, value: Any) -> Any:
    return ms_to_dt(value) if field in HISTORY_TIME_FIELDS and value is not None else value


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date_str(date, ["path", "date"])
    emp = get_employee(emp_code)
    cur = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if cur is None:
        raise HTTPException(404, "no attendance record for this date")

    old_in, old_out = dt_to_ms(cur.get("punch_in")), dt_to_ms(cur.get("punch_out"))
    new_status = body.status or cur["status"]

    if new_status in NON_PRESENCE_STATUSES:
        # ABSENT / LEAVE clear both punch times, so supplying one is contradictory.
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(["body"], f"{new_status} records have no punch times")
        new_in = new_out = None
    else:
        new_in = body.punch_in // 1000 * 1000 if body.punch_in is not None else old_in
        new_out = body.punch_out // 1000 * 1000 if body.punch_out is not None else old_out
        if new_in is None:
            raise unprocessable(["body", "punch_in"], f"{new_status} requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in // 1000, emp["shift_start"], emp["shift_end"]) != day:
            raise unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if new_out is not None and (body.punch_in is not None or body.punch_out is not None):
            span = new_out // 1000 - new_in // 1000
            if span <= 0 or span > MAX_SHIFT_SECONDS:
                raise unprocessable(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours")

    derived = derive_fields(
        new_status,
        None if new_in is None else new_in // 1000,
        None if new_out is None else new_out // 1000,
        day, emp["shift_start"], emp["shift_end"],
    )
    before = {
        "status": cur["status"], "punch_in": old_in, "punch_out": old_out,
        "work_hours": cur.get("work_hours"),
        "late_minutes": int(cur.get("late_minutes") or 0),
        "overtime_minutes": int(cur.get("overtime_minutes") or 0),
        "half_day": bool(cur.get("half_day") or False),
    }
    after = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}

    if all(before[k] == after[k] for k in ("status", "punch_in", "punch_out")):
        raise unprocessable(["body"], "the request does not change anything")

    changes = {k: {"from": _bson_change_value(k, before[k]), "to": _bson_change_value(k, after[k])}
               for k in CHANGE_FIELDS if before[k] != after[k]}
    entry = {"at": ms_to_dt(now_ms()), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the write only applies if the record is still exactly as we read it
    # (same inputs, same number of history entries). The loser gets 409 and no history entry is lost.
    history_len = len(cur.get("history") or [])
    guard: dict = {
        "emp_code": emp_code, "date": date, "status": cur["status"],
        "punch_in": cur.get("punch_in"), "punch_out": cur.get("punch_out"),
    }
    if history_len == 0:
        guard["$or"] = [{"history": None}, {"history": {"$size": 0}}]
    else:
        guard["history"] = {"$size": history_len}

    updated = db.attendance_logs.find_one_and_update(
        guard,
        {
            "$set": {
                "status": new_status,
                "punch_in": None if new_in is None else ms_to_dt(new_in),
                "punch_out": None if new_out is None else ms_to_dt(new_out),
                **derived,
            },
            "$push": {"history": entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; reload it and try again")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 7. Analytics: pipeline builders
# --------------------------------------------------------------------------- #
def half_up_expr(expr: Any, places: int) -> dict:
    """Half-up rounding inside a pipeline. MongoDB's $round is banker's rounding, which R8 forbids.
    The 1e-10 nudge cancels binary-float error (x * 10^places can land just below a true .5)."""
    factor = 10**places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, 0.5 + 1e-10]}}, factor]}


def log_exprs(p: str) -> dict:
    """Aggregation expressions over ONE attendance log. `p` is '$' for a document of the pipeline or
    '$$l.' for the variable of a $map. They read the STORED derived values; nothing is recomputed from punch times.
    Missing legacy fields: half_day -> false, late/overtime -> 0."""
    status = f"{p}status"
    is_present = {"$in": [status, list(PRESENCE_STATUSES)]}
    on_weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {
        "dateString": f"{p}date", "format": "%Y-%m-%d", "onError": None, "onNull": None}}}, MONGO_WEEKDAYS]}
    half = {"$ifNull": [f"{p}half_day", False]}
    late = {"$ifNull": [f"{p}late_minutes", 0]}
    has_hours = {"$and": [is_present, {"$isNumber": f"{p}work_hours"}]}
    presence_any_day = {"$cond": [is_present, {"$cond": [half, 0.5, 1]}, 0]}
    return {
        "presence_any_day": presence_any_day,  # trend: weekends count
        "present_weight": {"$cond": [on_weekday, presence_any_day, 0]},  # R5-R7: Mon-Fri only, half day = 0.5
        "late_minutes": late,
        "late_flag": {"$cond": [{"$gt": [late, 0]}, 1, 0]},
        "overtime_minutes": {"$ifNull": [f"{p}overtime_minutes", 0]},
        "leave_flag": {"$cond": [{"$eq": [status, "LEAVE"]}, 1, 0]},
        "on_duty_flag": {"$cond": [{"$eq": [status, "ON_DUTY"]}, 1, 0]},
        # work_hours as integer hundredths: exact sums, so the mean can be rounded exactly in Python
        "work_cents": {"$cond": [has_hours, {"$toLong": {"$round": [{"$multiply": [f"{p}work_hours", 100]}, 0]}}, 0]},
        "work_flag": {"$cond": [has_hours, 1, 0]},
    }


def build_employee_monthly_pipeline(emp_code: str, first: dt.date, last: dt.date) -> list:
    x = log_exprs("$")
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": x["present_weight"]},
            "leave_days": {"$sum": x["leave_flag"]},
            "late_count": {"$sum": x["late_flag"]},
            "total_late_minutes": {"$sum": x["late_minutes"]},
            "total_overtime_minutes": {"$sum": x["overtime_minutes"]},
        }},
    ]


def build_department_summary_pipeline(first: dt.date, last: dt.date, department: Optional[str]) -> list:
    """Starts from EMPLOYEES, so people without any log still exist as a document and are counted;
    $lookup keeps them with logs = []. Employees who join after the month are filtered out by the $match (R9)."""
    l = log_exprs("$$l.")

    def over_logs(expr: Any) -> dict:
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}

    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "status": 1, "date": 1, "half_day": 1, "late_minutes": 1,
                              "overtime_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        # one row per employee ...
        {"$project": {
            "_id": 0, "department": 1,
            "present_days": over_logs(l["present_weight"]),
            "late_count": over_logs(l["late_flag"]),
            "total_late_minutes": over_logs(l["late_minutes"]),
            "leave_count": over_logs(l["leave_flag"]),
            "on_duty_count": over_logs(l["on_duty_flag"]),
            "work_cents": over_logs(l["work_cents"]),
            "work_n": over_logs(l["work_flag"]),
        }},
        # ... then one row per department. headcount counts EMPLOYEES, never logs.
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "work_cents": {"$sum": "$work_cents"},
            "work_n": {"$sum": "$work_n"},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(first: dt.date, last: dt.date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees have emp = [] and are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4). Sorting on the total ONLY, so ties really tie.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so a tie at the cutoff is kept whole
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def build_department_trend_pipeline(department: str, codes: list, first: dt.date, last: dt.date) -> list:
    x = log_exprs("$")
    lower = dt.datetime(first.year, first.month, first.day, tzinfo=UTC)
    upper = dt.datetime(last.year, last.month, last.day, tzinfo=UTC) + dt.timedelta(days=1)  # upper bound is exclusive
    return [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "emp_code": {"$in": codes}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": x["presence_any_day"]},
                    "late_count": {"$sum": x["late_flag"]}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"d": 1}},
        # gap filling: one document per calendar day in [first, last], generated by the database
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {  # R9: employees of the department who have joined on or before that day
            "from": "employees", "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, MONGO_WEEKDAYS]},
        }},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4),
            None,
        ]}}},
        # trailing 7-row window inside the requested range; $avg ignores the null rates of weekends
        {"$setWindowFields": {"sortBy": {"d": 1},
                              "output": {"avg7": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$isNumber": "$avg7"}, half_up_expr("$avg7", 4), None]},
        }},
    ]


def prepare_trend(department: str, date_from: str, date_to: str) -> tuple[list, dt.date, dt.date]:
    """Validates the trend request and builds its pipeline (shared by the endpoint and /admin/explain)."""
    first = parse_date_str(date_from, ["query", "from"])
    last = parse_date_str(date_to, ["query", "to"])
    if last < first:
        raise unprocessable(["query", "to"], "to must not be before from")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(["query", "to"], f"the range must not exceed {MAX_TREND_DAYS} days")
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    codes = [e["emp_code"] for e in db.employees.find(
        {"department": department, "joined_on": {"$lte": date_to}}, {"emp_code": 1, "_id": 0})]
    return build_department_trend_pipeline(department, codes, first, last), first, last


# --------------------------------------------------------------------------- #
# 7. Analytics: endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = parse_month(month, ["query", "month"])
    emp = get_employee(emp_code)
    rows = list(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, first, last)))
    agg = rows[0] if rows else {}
    present = Decimal(str(agg.get("present_days", 0)))
    # R7: working days are counted from joined_on for mid-month joiners
    working_days = count_weekdays(max(first, dt.date.fromisoformat(emp["joined_on"])), last)
    pct = None if working_days == 0 else float(round_half_up(present / Decimal(working_days) * 100, 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, last = parse_month(month, ["query", "month"])
    items = []
    for row in db.employees.aggregate(build_department_summary_pipeline(first, last, department)):
        work_n = int(row["work_n"])
        avg = None
        if work_n:  # mean over RECORDS: total hundredths / number of records / 100, rounded half-up
            avg = float(round_half_up(Decimal(int(row["work_cents"])) / Decimal(work_n) / 100, 2))
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": float(Decimal(str(row["present_days"]))),
            "avg_work_hours": avg,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = parse_month(month, ["query", "month"])
    rows = db.attendance_logs.aggregate(build_late_leaderboard_pipeline(first, last, limit, department))
    items = [{
        "rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
        "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"]),
    } for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to")):
    pipeline, _, _ = prepare_trend(department, date_from, date_to)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 8. Admin: explain
# --------------------------------------------------------------------------- #
def _jsonable(doc: dict) -> dict:
    return json.loads(json_util.dumps(doc))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs explain('executionStats') on the main query of the named endpoint, built by the same functions."""
    if endpoint == "attendance_list":
        spec = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": spec["filter"], "sort": dict(spec["sort"]),
                   "skip": spec["skip"], "limit": spec["limit"]}
    else:
        if endpoint == "employee_monthly":
            first, last = parse_month(require(month, "month"), ["query", "month"])
            collection = "attendance_logs"
            pipeline = build_employee_monthly_pipeline(require(emp_code, "emp_code"), first, last)
        elif endpoint == "department_summary":
            first, last = parse_month(require(month, "month"), ["query", "month"])
            collection = "employees"
            pipeline = build_department_summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            first, last = parse_month(require(month, "month"), ["query", "month"])
            collection = "attendance_logs"
            pipeline = build_late_leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            pipeline, _, _ = prepare_trend(require(department, "department"), require(from_, "from"), require(to, "to"))
            collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": _jsonable(result)}
