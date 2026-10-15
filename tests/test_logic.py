from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from app.main import (
    IST,
    epoch_ms_to_datetime,
    datetime_to_epoch_ms,
    determine_attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
    count_working_days,
    round_half_up_2,
    round_half_up_4,
)

def test_epoch_ms_conversions_and_truncation():
    # 1783312500900 ms -> should truncate fractional seconds to whole seconds
    ms = 1783312500900
    dt = epoch_ms_to_datetime(ms)
    assert dt.microsecond == 0
    # Back to epoch ms should end with 000
    assert datetime_to_epoch_ms(dt) == 1783312500000

def test_r1_attendance_date_normal_shift():
    # Shift 09:30 - 18:30
    # 2026-07-06 09:28 IST = 2026-07-06 03:58 UTC
    dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    att_date = determine_attendance_date(dt, "09:30", "18:30")
    assert att_date == "2026-07-06"

def test_r1_attendance_date_overnight_shift():
    # Shift 22:00 - 06:00
    # Punched in at 21:55 IST on 2026-07-06 = 16:25 UTC on 2026-07-06
    dt1 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert determine_attendance_date(dt1, "22:00", "06:00") == "2026-07-06"

    # Punched in at 02:00 IST on 2026-07-07 (earlier than shift_end 06:00)
    # Belongs to shift that started previous day -> 2026-07-06
    # 02:00 IST = 2026-07-06 20:30 UTC
    dt2 = datetime(2026, 7, 6, 20, 30, 0, tzinfo=timezone.utc)
    assert determine_attendance_date(dt2, "22:00", "06:00") == "2026-07-06"

    # Punched in at 06:00 IST on 2026-07-07 (not earlier than shift_end 06:00)
    # Belongs to 2026-07-07
    dt3 = datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc)
    assert determine_attendance_date(dt3, "22:00", "06:00") == "2026-07-07"

def test_r2_late_calculation():
    # Shift 09:30. Grace period 10 minutes (09:40:00).
    # 09:40:00 -> 0 min late
    dt_on_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc) # 09:40:00 IST
    assert compute_late_minutes(dt_on_grace, "09:30", "2026-07-06") == 0

    # 09:40:01 -> 10 min late (strictly more than 10:00 after shift_start)
    dt_late_10 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc) # 09:40:01 IST
    assert compute_late_minutes(dt_late_10, "09:30", "2026-07-06") == 10

    # 10:15:59 -> 45 min late
    dt_late_45 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc) # 10:15:59 IST
    assert compute_late_minutes(dt_late_45, "09:30", "2026-07-06") == 45

    # Early arrival (09:15:00) -> 0
    dt_early = datetime(2026, 7, 6, 3, 45, 0, tzinfo=timezone.utc) # 09:15:00 IST
    assert compute_late_minutes(dt_early, "09:30", "2026-07-06") == 0

def test_r3_overtime_calculation():
    # Normal shift 09:30 - 18:30 on 2026-07-06
    # Punch out at 18:35 (5 min) -> 0 (< 30 min)
    dt_out_5 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_5, "09:30", "18:30", "2026-07-06") == 0

    # Punch out at 19:10 (40 min) -> 40
    dt_out_40 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_40, "09:30", "18:30", "2026-07-06") == 40

    # Overnight shift 22:00 - 06:00 on 2026-07-06
    # Shift end is 2026-07-07 06:00 IST. Punch out 06:40 IST = 01:10 UTC next day
    dt_out_overnight = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_overnight, "22:00", "06:00", "2026-07-06") == 40

def test_r4_and_r5_work_hours_half_day():
    # 9 hours 7 minutes = 32820 seconds -> 9.116666... -> 9.12
    p_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    p_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    wh = compute_work_hours(p_in, p_out)
    assert wh == 9.12
    assert compute_half_day(wh) is False

    # Half day (< 4.50 hours): 3.5 hours
    p_in2 = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    p_out2 = datetime(2026, 7, 7, 7, 30, 0, tzinfo=timezone.utc)
    wh2 = compute_work_hours(p_in2, p_out2)
    assert wh2 == 3.5
    assert compute_half_day(wh2) is True

def test_r7_working_days():
    # July 2026 has 31 days. July 1 is Wednesday.
    # 23 weekdays (Mon-Fri) in July 2026
    # If joined on 2026-01-05 (before July), working days = 23
    assert count_working_days("2026-01-05", "2026-07") == 23

    # Mid-month joiner: joined on 2026-07-15 (Wednesday)
    # Weekdays from 2026-07-15 to 2026-07-31:
    # 15,16,17 (3) + 20..24 (5) + 27..31 (5) = 13 weekdays
    assert count_working_days("2026-07-15", "2026-07") == 13

    # Joined next month: 2026-08-01 -> 0 working days in July 2026
    assert count_working_days("2026-08-01", "2026-07") == 0

def test_r8_rounding():
    # 2 decimals half-up
    assert round_half_up_2(12.345) == 12.35
    assert round_half_up_2(12.344) == 12.34
    assert round_half_up_2(12.3450000) == 12.35

    # 4 decimals half-up
    assert round_half_up_4(0.12345) == 0.1235
    assert round_half_up_4(0.12344) == 0.1234
