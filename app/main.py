"""
Employee Attendance & Analytics API - Production Implementation
Contract: openapi.yaml (v2.0.0)
Data Model: DATA_MODEL.md
"""
import calendar
import math
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from bson import json_util

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load .env without overriding system environment variables
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure all required indexes exist idempotently on startup
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])

        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])
    except Exception:
        # If DB is temporarily unreachable at boot, lifespan will pass so health probe works
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Math & Business Logic Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up_2(val: Optional[Union[float, int, Decimal]]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[Union[float, int, Decimal]]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def epoch_ms_to_datetime(ms: int) -> datetime:
    # R1: Truncate instants to whole seconds
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Truncate to whole seconds
    sec = int(dt.timestamp())
    return sec * 1000


def truncate_datetime_to_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0)


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_ist = punch_in_utc.astimezone(IST)
    h_end, m_end = map(int, shift_end.split(":"))
    end_time = time(h_end, m_end)
    is_overnight = shift_end <= shift_start

    if is_overnight and punch_ist.time() < end_time:
        att_date = punch_ist.date() - timedelta(days=1)
    else:
        att_date = punch_ist.date()
    return att_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Grace period 10 minutes. A punch-in is late only if strictly more than 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start - not since grace period.
    """
    punch_ist = truncate_datetime_to_seconds(punch_in_utc.astimezone(IST))
    h_start, m_start = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(date_str).replace(
        hour=h_start, minute=m_start, second=0, microsecond=0, tzinfo=IST
    )

    elapsed_seconds = (punch_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:  # strictly more than 10 minutes (600 seconds)
        return int(math.floor(elapsed_seconds / 60))
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    p_in = truncate_datetime_to_seconds(punch_in_utc)
    p_out = truncate_datetime_to_seconds(punch_out_utc)
    diff_sec = Decimal(int((p_out - p_in).total_seconds()))
    hours = diff_sec / Decimal(3600)
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """
    R3: Whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    punch_out_ist = truncate_datetime_to_seconds(punch_out_utc.astimezone(IST))
    h_end, m_end = map(int, shift_end.split(":"))
    base_date = datetime.fromisoformat(date_str)
    if shift_end <= shift_start:  # overnight shift
        shift_end_date = base_date + timedelta(days=1)
    else:
        shift_end_date = base_date

    shift_end_dt = shift_end_date.replace(
        hour=h_end, minute=m_end, second=0, microsecond=0, tzinfo=IST
    )
    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    diff_min = int(math.floor(diff_sec / 60))
    if diff_min >= 30:
        return diff_min
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day = true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Working days are Mon-Fri, no holiday calendar.
    For an employee who joined mid-month, working days are counted from joined_on.
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    first_of_month = date(year, month, 1)
    last_of_month = date(year, month, num_days)

    joined_date = date.fromisoformat(joined_on_str)
    start_date = max(first_of_month, joined_date)

    if start_date > last_of_month:
        return 0

    working_days = 0
    curr = start_date
    while curr <= last_of_month:
        if curr.weekday() < 5:  # Monday=0 ... Friday=4
            working_days += 1
        curr += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Pydantic Schemas strictly compliant with openapi.yaml
# --------------------------------------------------------------------------- #
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


def validate_epoch_ms(v: Optional[int]) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, float) or isinstance(v, str) or not isinstance(v, int):
        raise ValueError("Timestamp must be an integer epoch millisecond count")
    if v < MIN_EPOCH_MS or v > MAX_EPOCH_MS:
        raise ValueError("Timestamp out of accepted range (must be epoch milliseconds)")
    return v


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: PresenceStatusType = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_ts(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(v)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_ts(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_ts(cls, v: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(v)


def serialize_history_entry(entry: dict) -> dict:
    at_val = entry.get("at")
    at_ms = datetime_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val
    raw_changes = entry.get("changes", {})
    changes = {}
    for k, v in raw_changes.items():
        from_v = v.get("from")
        to_v = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_v, datetime):
                from_v = datetime_to_epoch_ms(from_v)
            if isinstance(to_v, datetime):
                to_v = datetime_to_epoch_ms(to_v)
        changes[k] = {"from": from_v, "to": to_v}
    return {
        "at": at_ms,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    raw_hist = doc.get("history") or []
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(punch_in) if isinstance(punch_in, datetime) else punch_in,
        "punch_out": datetime_to_epoch_ms(punch_out) if isinstance(punch_out, datetime) else punch_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in raw_hist],
    }


def serialize_employee_doc(doc: dict) -> dict:
    c_at = doc.get("created_at")
    c_ms = datetime_to_epoch_ms(c_at) if isinstance(c_at, datetime) else c_at
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": c_ms,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database ping failed")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    created_at_dt = truncate_datetime_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = created_at_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee {body.emp_code} already exists")

    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_in_dt = truncate_datetime_to_seconds(datetime.now(timezone.utc))

    att_date = determine_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail=f"Attendance record already exists for {body.emp_code} on {att_date}",
        )

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_out_dt = truncate_datetime_to_seconds(datetime.now(timezone.utc))

    # Find the most recent record whose punch_in <= punched_at
    latest_rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )

    if not latest_rec:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    if latest_rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")

    p_in_dt = latest_rec["punch_in"]
    if p_in_dt.tzinfo is None:
        p_in_dt = p_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_dt - p_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(
            status_code=422,
            detail="Punch-out time must be strictly after punch-in and within 24 hours",
        )

    work_hours = compute_work_hours(p_in_dt, punch_out_dt)
    overtime = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], latest_rec["date"])
    half_day = compute_half_day(work_hours)

    # Concurrency safe: only update if punch_out is still None
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": latest_rec["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record was punched out concurrently")

    return serialize_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to:
        if date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail=f"No attendance record found for {date}")

    orig_status = rec["status"]
    orig_p_in = rec.get("punch_in")
    orig_p_out = rec.get("punch_out")
    orig_work_hours = rec.get("work_hours")
    orig_late = rec.get("late_minutes", 0)
    orig_ot = rec.get("overtime_minutes", 0)
    orig_half = rec.get("half_day", False)
    orig_history = rec.get("history") or []

    new_status = body.status if body.status is not None else orig_status

    # Rule: setting status ABSENT or LEAVE clears both punch times,
    # so supplying punch_in or punch_out with it is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        new_p_in = None
        new_p_out = None
    else:
        new_p_in = (
            epoch_ms_to_datetime(body.punch_in)
            if body.punch_in is not None
            else orig_p_in
        )
        new_p_out = (
            epoch_ms_to_datetime(body.punch_out)
            if body.punch_out is not None
            else orig_p_out
        )

    # A presence status requires a punch_in (supplied or already stored)
    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_p_in is None:
        raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

    # punch_in must stay on the record's attendance date (R1)
    if new_p_in is not None:
        computed_date = determine_attendance_date(new_p_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {computed_date}, not {date}",
            )

    # punch_out must be after punch_in and within 24 hours
    if new_p_in is not None and new_p_out is not None:
        p_in_sec = truncate_datetime_to_seconds(new_p_in)
        p_out_sec = truncate_datetime_to_seconds(new_p_out)
        diff_s = (p_out_sec - p_in_sec).total_seconds()
        if diff_s <= 0 or diff_s > 86400:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late = 0
        new_ot = 0
        new_half = False
    else:
        new_late = compute_late_minutes(new_p_in, emp["shift_start"], date)
        if new_p_out is not None:
            new_work_hours = compute_work_hours(new_p_in, new_p_out)
            new_ot = compute_overtime(new_p_out, emp["shift_start"], emp["shift_end"], date)
            new_half = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_ot = 0
            new_half = False

    # Check for changes
    changes = {}
    if new_status != orig_status:
        changes["status"] = {"from": orig_status, "to": new_status}
    if new_p_in != orig_p_in:
        changes["punch_in"] = {"from": orig_p_in, "to": new_p_in}
    if new_p_out != orig_p_out:
        changes["punch_out"] = {"from": orig_p_out, "to": new_p_out}
    if new_work_hours != orig_work_hours:
        changes["work_hours"] = {"from": orig_work_hours, "to": new_work_hours}
    if new_late != orig_late:
        changes["late_minutes"] = {"from": orig_late, "to": new_late}
    if new_ot != orig_ot:
        changes["overtime_minutes"] = {"from": orig_ot, "to": new_ot}
    if new_half != orig_half:
        changes["half_day"] = {"from": orig_half, "to": new_half}

    if not changes:
        raise HTTPException(status_code=422, detail="Regularization request modifies nothing")

    now_dt = truncate_datetime_to_seconds(datetime.now(timezone.utc))
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking: ensure history array length matches to prevent losing concurrent edits
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            f"history.{len(orig_history)}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_p_in,
                "punch_out": new_p_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_half,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines (Part B)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, m_start: str, m_end: str) -> List[Dict[str, Any]]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},  # Mon to Fri
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    m_start = f"{month}-01"
    m_end = f"{month}-{num_days:02d}"

    working_days = count_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, m_start, m_end)

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up_2((present_days / working_days) * 100)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up_2(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(m_start: str, m_end: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": m_end}}
    if department is not None:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", m_start]},
                                    {"$lte": ["$date", m_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "status": "$logs.status",
                "work_hours": "$logs.work_hours",
                "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
                "half_day": {"$ifNull": ["$logs.half_day", False]},
                "day_of_week": {
                    "$cond": [
                        {"$ne": ["$logs.date", None]},
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": {"$ifNull": ["$logs.date", "2026-01-01"]},
                                    "format": "%Y-%m-%d",
                                }
                            }
                        },
                        0,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    m_start = f"{month}-01"
    m_end = f"{month}-{num_days:02d}"

    pipeline = build_department_summary_pipeline(m_start, m_end, department)
    items = []
    for doc in db.employees.aggregate(pipeline):
        headcount = len(doc["employees_set"])
        if headcount == 0:
            continue
        wh_count = doc["work_hours_count"]
        if wh_count > 0:
            avg_wh = round_half_up_2(doc["total_work_hours"] / wh_count)
        else:
            avg_wh = None

        items.append(
            {
                "department": doc["_id"],
                "headcount": headcount,
                "present_days": round_half_up_2(doc["present_days"]),
                "avg_work_hours": avg_wh,
                "late_count": int(doc["late_count"]),
                "total_late_minutes": int(doc["total_late_minutes"]),
                "leave_count": int(doc["leave_count"]),
                "on_duty_count": int(doc["on_duty_count"]),
            }
        )

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    m_start: str, m_end: str, limit: int, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": m_start, "$lte": m_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$project": {
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    m_start = f"{month}-01"
    m_end = f"{month}-{num_days:02d}"

    pipeline = build_late_leaderboard_pipeline(m_start, m_end, limit, department)
    docs = list(db.attendance_logs.aggregate(pipeline))
    items = [
        {
            "rank": d["rank"],
            "emp_code": d["emp_code"],
            "name": d["name"],
            "department": d["department"],
            "total_late_minutes": d["total_late_minutes"],
            "late_count": d["late_count"],
        }
        for d in docs
    ]
    return {"month": month, "items": items}


def build_department_trend_pipeline(from_date: str, to_date: str, dept_emp_codes: List[str]) -> List[Dict[str, Any]]:
    # Index-optimized pipeline: first stage matches indexed date on attendance_logs
    return [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        start_d = date.fromisoformat(from_date)
        end_d = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    if end_d < start_d:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")

    diff_days = (end_d - start_d).days + 1
    if diff_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # Check department exists
    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    dept_emp_codes = [e["emp_code"] for e in dept_emps]
    pipeline = build_department_trend_pipeline(from_date, to_date, dept_emp_codes)
    log_map = {row["_id"]: row for row in db.attendance_logs.aggregate(pipeline)}

    items = []
    curr = start_d
    rates_window: List[Optional[float]] = []

    while curr <= end_d:
        curr_str = curr.isoformat()
        is_working = curr.weekday() < 5  # Mon-Fri
        headcount = sum(1 for e in dept_emps if e.get("joined_on", "") <= curr_str)

        log_data = log_map.get(curr_str, {})
        pres_count = float(log_data.get("present_count", 0.0))
        late_count = int(log_data.get("late_count", 0))

        if is_working and headcount > 0:
            att_rate = round_half_up_4(pres_count / headcount)
        else:
            att_rate = None

        rates_window.append(att_rate)
        # Window of up to 7 days inside the range (current and up to 6 preceding)
        win = rates_window[-7:]
        valid_rates = [r for r in win if r is not None]
        if valid_rates:
            m_avg = round_half_up_4(sum(valid_rates) / len(valid_rates))
        else:
            m_avg = None

        items.append(
            {
                "date": curr_str,
                "is_working_day": is_working,
                "headcount": headcount,
                "present_count": round_half_up_2(pres_count),
                "late_count": late_count,
                "attendance_rate": att_rate,
                "moving_avg_7d": m_avg,
            }
        )
        curr += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint (Part C)
# --------------------------------------------------------------------------- #

def _explain_response(endpoint: str, collection: str, raw_explain: dict) -> Response:
    """Serialize the full explain result with BSON-aware Extended JSON."""
    envelope = {
        "endpoint": endpoint,
        "collection": collection,
        "explain": raw_explain,
    }
    # json_util.dumps handles ObjectId, Timestamp, Int64, Datetime, Decimal128, etc.
    json_str = json_util.dumps(envelope)
    return Response(content=json_str, media_type="application/json")


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusType] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        if endpoint == "attendance_list":
            if date_from and date_to and date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
            q: Dict[str, Any] = {}
            if emp_code is not None:
                q["emp_code"] = emp_code
            if date_from or date_to:
                q["date"] = {}
                if date_from:
                    q["date"]["$gte"] = date_from
                if date_to:
                    q["date"]["$lte"] = date_to
            if status is not None:
                q["status"] = status
            skip = (page - 1) * page_size
            raw = db.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": q,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": skip,
                    "limit": page_size,
                },
                verbosity="executionStats",
            )
            return _explain_response(endpoint, "attendance_logs", raw)

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="emp_code and month are required")
            year, m_num = map(int, month.split("-"))
            num_days = calendar.monthrange(year, m_num)[1]
            m_start = f"{month}-01"
            m_end = f"{month}-{num_days:02d}"
            pipe = build_employee_monthly_pipeline(emp_code, m_start, m_end)
            raw = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
                verbosity="executionStats",
            )
            return _explain_response(endpoint, "attendance_logs", raw)

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            year, m_num = map(int, month.split("-"))
            num_days = calendar.monthrange(year, m_num)[1]
            m_start = f"{month}-01"
            m_end = f"{month}-{num_days:02d}"
            pipe = build_department_summary_pipeline(m_start, m_end, department)
            raw = db.command(
                "explain",
                {"aggregate": "employees", "pipeline": pipe, "cursor": {}},
                verbosity="executionStats",
            )
            return _explain_response(endpoint, "employees", raw)

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            year, m_num = map(int, month.split("-"))
            num_days = calendar.monthrange(year, m_num)[1]
            m_start = f"{month}-01"
            m_end = f"{month}-{num_days:02d}"
            pipe = build_late_leaderboard_pipeline(m_start, m_end, limit, department)
            raw = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
                verbosity="executionStats",
            )
            return _explain_response(endpoint, "attendance_logs", raw)

        elif endpoint == "department_trend":
            if not from_date or not to_date or not department:
                raise HTTPException(status_code=422, detail="department, from, and to are required")
            try:
                start_d = date.fromisoformat(from_date)
                end_d = date.fromisoformat(to_date)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date format")
            if end_d < start_d:
                raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")
            if (end_d - start_d).days + 1 > 92:
                raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
            dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
            dept_emp_codes = [e["emp_code"] for e in dept_emps]
            pipe = build_department_trend_pipeline(from_date, to_date, dept_emp_codes)
            raw = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
                verbosity="executionStats",
            )
            return _explain_response(endpoint, "attendance_logs", raw)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
