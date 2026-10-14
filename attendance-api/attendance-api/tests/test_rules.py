"""Pure unit tests for the business rules - no MongoDB needed:  pytest tests/test_rules.py"""
import datetime as dt
from decimal import Decimal

import pytest

from app.main import (IST, attendance_date, compute_late_minutes, compute_overtime_minutes, compute_work_hours,
                      count_weekdays, derive_fields, dt_to_ms, ms_to_dt, round_half_up)


def ist(y, mo, d, h, mi, s=0):
    return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp())


DAY = dt.date(2026, 7, 6)  # a Monday


@pytest.mark.parametrize("h,mi,s,expected", [
    (9, 40, 0, 0),      # exactly 10:00 after 09:30 -> not late (spec example)
    (9, 40, 1, 10),     # 10:01 after -> late, floor minutes since shift_start
    (9, 40, 59, 10),    # old starter returned 0 here (int(10.98) > 10 is False)
    (10, 15, 59, 45),   # spec example
    (9, 0, 0, 0),       # early
    (9, 30, 0, 0),
])
def test_late(h, mi, s, expected):
    assert compute_late_minutes(ist(2026, 7, 6, h, mi, s), DAY, "09:30") == expected


@pytest.mark.parametrize("h,mi,s,expected", [
    (18, 30, 0, 0), (18, 59, 59, 0),        # < 30 minutes -> 0
    (19, 0, 0, 30), (19, 0, 59, 30),        # >= 30 minutes counts, floor
    (19, 10, 0, 40),
    (17, 0, 0, 0),                          # left early
])
def test_overtime_day_shift(h, mi, s, expected):
    assert compute_overtime_minutes(ist(2026, 7, 6, h, mi, s), DAY, "09:30", "18:30") == expected


def test_overtime_overnight_ends_next_day():
    # sample 7: shift 22:00-06:00, out 06:40 next day -> 40
    assert compute_overtime_minutes(ist(2026, 7, 7, 6, 40), DAY, "22:00", "06:00") == 40
    assert compute_overtime_minutes(ist(2026, 7, 7, 6, 20), DAY, "22:00", "06:00") == 0


@pytest.mark.parametrize("seconds,expected", [
    (3600, "1.00"), (32820, "9.12"), (31500, "8.75"),
    (16200, "4.50"),
    (3, "0.00"),
    (18, "0.01"),   # 0.005 -> half-up gives 0.01 (banker's/float round() gives 0.0)
    (54, "0.02"),   # 0.015 -> 0.02
])
def test_work_hours_half_up(seconds, expected):
    assert compute_work_hours(0, seconds) == Decimal(expected)


def test_half_day_uses_rounded_hours():
    day = DAY
    in_s = ist(2026, 7, 6, 9, 30)
    # 4h29m50s = 4.4972h -> rounds to 4.50 -> NOT a half day
    assert derive_fields("PRESENT", in_s, in_s + 4 * 3600 + 29 * 60 + 50, day, "09:30", "18:30")["half_day"] is False
    # 4h29m -> 4.4833 -> 4.48 -> half day
    assert derive_fields("PRESENT", in_s, in_s + 4 * 3600 + 29 * 60, day, "09:30", "18:30")["half_day"] is True
    # sample 4: 3.5h WFH
    assert derive_fields("WFH", in_s, in_s + 12600, day, "09:30", "18:30")["half_day"] is True


def test_derive_defaults():
    assert derive_fields("ABSENT", None, None, DAY, "09:30", "18:30") == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    open_rec = derive_fields("PRESENT", ist(2026, 7, 6, 10, 5), None, DAY, "09:30", "18:30")
    assert open_rec == {"work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False}


def test_attendance_date_day_shift():
    assert attendance_date(ist(2026, 7, 6, 23, 59, 59), "09:30", "18:30") == dt.date(2026, 7, 6)
    assert attendance_date(ist(2026, 7, 7, 0, 0, 0), "09:30", "18:30") == dt.date(2026, 7, 7)


def test_attendance_date_ist_not_utc():
    # 2026-07-06 03:58Z is 09:28 IST on the 6th; 2026-07-05 20:00Z is 01:30 IST on the 6th
    assert attendance_date(int(dt.datetime(2026, 7, 5, 20, 0, tzinfo=dt.timezone.utc).timestamp()), "09:30", "18:30") == DAY


@pytest.mark.parametrize("h,mi,s,expected_day", [
    (21, 55, 0, 6),     # sample 7
    (22, 0, 0, 6),
    (5, 59, 59, 5),     # earlier than shift_end -> previous day's shift
    (6, 0, 0, 6),       # not earlier than 06:00 -> same day
    (12, 0, 0, 6),
])
def test_attendance_date_overnight(h, mi, s, expected_day):
    assert attendance_date(ist(2026, 7, 6, h, mi, s), "22:00", "06:00") == dt.date(2026, 7, expected_day)


def test_late_for_overnight_uses_previous_day_start():
    in_s = ist(2026, 7, 7, 0, 30)  # belongs to the shift of the 6th, started 22:00
    day = attendance_date(in_s, "22:00", "06:00")
    assert compute_late_minutes(in_s, day, "22:00") == 150


def test_ms_roundtrip():
    ms = 1783312500000
    assert dt_to_ms(ms_to_dt(ms)) == ms
    assert dt_to_ms(ms_to_dt(ms).replace(tzinfo=None)) == ms  # naive datetimes are read as UTC


def test_count_weekdays():
    assert count_weekdays(dt.date(2026, 7, 1), dt.date(2026, 7, 31)) == 23   # July 2026
    assert count_weekdays(dt.date(2026, 7, 13), dt.date(2026, 7, 31)) == 15   # mid-month joiner
    assert count_weekdays(dt.date(2026, 7, 4), dt.date(2026, 7, 5)) == 0      # weekend only
    assert count_weekdays(dt.date(2026, 8, 1), dt.date(2026, 7, 31)) == 0     # joined after the month


def test_round_half_up():
    assert round_half_up(0.125, 2) == Decimal("0.13")
    assert round_half_up(2.675, 2) == Decimal("2.68")  # float round() gives 2.67
    assert round_half_up(Decimal("0.03125"), 4) == Decimal("0.0313")
