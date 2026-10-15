
import os
import json
import calendar
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException,Query
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from decimal import Decimal, ROUND_HALF_UP
from bson import json_util

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
db.employees.create_index("emp_code", unique=True)
db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
db.attendance_logs.create_index([("date", -1), ("status", 1)])
db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
db.attendance_logs.create_index([("date", -1), ("status", 1), ("emp_code", 1)])
db.attendance_logs.create_index(
    [("emp_code", 1), ("date", 1)],
    unique=True,
    )
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")



def truncate_to_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    punch_in = truncate_to_seconds(as_ist(punch_in))

    start_date = date.fromisoformat(attendance_date)
    start_h, start_m = map(int, shift_start.split(":"))

    shift_start_time = datetime(
        start_date.year,
        start_date.month,
        start_date.day,
        start_h,
        start_m,
        tzinfo=IST,
    )

    elapsed_seconds = (punch_in - shift_start_time).total_seconds()

    if elapsed_seconds <= 600:
        return 0

    return int(elapsed_seconds // 60)


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    punch_in = truncate_to_seconds(as_ist(punch_in))
    punch_out = truncate_to_seconds(as_ist(punch_out))

    seconds = Decimal(
        str((punch_out - punch_in).total_seconds())
    )
    hours = seconds / Decimal("3600")

    return float(
        hours.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    punch_out = truncate_to_seconds(as_ist(punch_out))

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    end_date = date.fromisoformat(attendance_date)

    if end_minutes <= start_minutes:
        end_date += timedelta(days=1)

    expected_end = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        end_h,
        end_m,
        tzinfo=IST,
    )

    overtime_seconds = (
        punch_out - expected_end
    ).total_seconds()

    if overtime_seconds < 30 * 60:
        return 0

    return int(overtime_seconds // 60)


def serialize_api(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        value = value.replace(microsecond=0)
        return int(value.timestamp() * 1000)

    if isinstance(value, dict):
        return {
            key: serialize_api(item)
            for key, item in value.items()
            if key not in {"_id", "id"}
        }

    if isinstance(value, list):
        return [serialize_api(item) for item in value]

    return value


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None, strict=True, ge=100000000000
    )
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None, strict=True, ge=100000000000
    )


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(
        default=None, strict=True, ge=100000000000
    )
    punch_out: Optional[int] = Field(
        default=None, strict=True, ge=100000000000
    )
    reason: str
    regularized_by: str



@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    try:
        joined = date.fromisoformat(body.joined_on)
        if joined.isoformat() != body.joined_on:
            raise ValueError
        for value in (body.shift_start, body.shift_end):
            parsed = datetime.strptime(value, "%H:%M")
            if parsed.strftime("%H:%M") != value:
                raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="joined_on must be YYYY-MM-DD and shifts must be HH:MM",
        )

    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_api(doc)



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1:
        raise HTTPException(
            status_code=422,
            detail="page must be at least 1"
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100"
        )

    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return serialize_api({
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    })



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    
    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(
            status_code=422,
            detail="status must be PRESENT, WFH, or ON_DUTY",
        )


    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
        body.punched_at / 1000,
        tz=timezone.utc
        ).astimezone(IST)
    else:
        ts = datetime.now(IST)

        
    ts = truncate_to_seconds(ts)

    start_h, start_m = map(
        int, emp["shift_start"].split(":")
    )
    end_h, end_m = map(
        int, emp["shift_end"].split(":")
    )

    attendance_day = ts.date()

    if (
        (end_h, end_m) <= (start_h, start_m)
        and (ts.hour, ts.minute) < (end_h, end_m)
    ):
        attendance_day -= timedelta(days=1)

    d = attendance_day.isoformat()


    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date",
        )

    
    punch_in_time = ts
    late_minutes = compute_late_minutes(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        d,
    )

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": punch_in_time,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date",
        )
    return serialize_api(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1:
        raise HTTPException(422, "page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100"
        )

    try:
        for supplied_date in (date_from, date_to):
            if supplied_date is not None:
                parsed_date = date.fromisoformat(supplied_date)
                if parsed_date.isoformat() != supplied_date:
                    raise ValueError
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
    except ValueError:
        raise HTTPException(422, "date_from and date_to must use YYYY-MM-DD format")

    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)

    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = list(cursor)

    return serialize_api({
    "items": items,
    "total": total,
    "page": page,
    "page_size": page_size,
    })


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

        
    now = datetime.now(IST)
    ts = (
        datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        ).astimezone(IST)
        if body.punched_at is not None
        else now
    )

    ts = truncate_to_seconds(ts)

    date_str = ts.date().isoformat()


    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": date_str,
        "punch_out": None,
    })

    if not record:
        previous_date = (ts.date() - timedelta(days=1)).isoformat()

        previous_record = db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "date": previous_date,
            "punch_out": None,
        })

        if previous_record:
            previous_start = previous_record.get("punch_in")

            if isinstance(previous_start, datetime):
                if previous_start.tzinfo is None:
                    previous_start = previous_start.replace(
                        tzinfo=timezone.utc
                    ).astimezone(IST)

                shift_start_h, shift_start_m = map(
                    int, emp["shift_start"].split(":")
                )
                shift_end_h, shift_end_m = map(
                    int, emp["shift_end"].split(":")
                )

                if (shift_end_h, shift_end_m) <= (
                    shift_start_h, shift_start_m
                ) and ts.date() > previous_start.date():
                    record = previous_record


    
    if not record:
        for check_date in (
            ts.date().isoformat(),
            (ts.date() - timedelta(days=1)).isoformat(),
        ):
            existing = db.attendance_logs.find_one({
                "emp_code": body.emp_code,
                "date": check_date,
            })

            if existing and existing.get("punch_out") is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Already punched out for this date",
                )

        raise HTTPException(
            status_code=404,
            detail="Punch-in record not found for this date",
        )


    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Already punched out for this date",
        )

    
    punch_in_time = record.get("punch_in")

    if not isinstance(punch_in_time, datetime):
        raise HTTPException(
            status_code=409,
            detail="Attendance record has no valid punch-in time",
        )


    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc).astimezone(IST)

    elapsed_seconds = (ts - punch_in_time).total_seconds()
    if elapsed_seconds <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(422, "Punch duration cannot exceed 24 hours")

    work_hours = compute_work_hours(punch_in_time, ts)

    overtime_minutes = compute_overtime(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        record["date"],
    )

    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )
    if result.modified_count != 1:
        raise HTTPException(409, "Already punched out for this date")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_api(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    if len(body.reason.strip()) < 5 or len(body.reason) > 200:
        raise HTTPException(422, "reason must contain 5-200 characters")

    if not body.regularized_by.strip() or len(body.regularized_by) > 50:
        raise HTTPException(422, "regularized_by must contain 1-50 characters")

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "date must use YYYY-MM-DD format")

    
    if body.status is not None and body.status not in {
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    }:
        raise HTTPException(422, "Invalid status")


    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })
    if not record:
        raise HTTPException(404, "Attendance record not found")

    def as_ist(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(IST)

    def from_epoch_ms(value):
        if value is None:
            return None
        return datetime.fromtimestamp(
            value / 1000, tz=timezone.utc
        ).astimezone(IST)

    def to_epoch_ms(value):
        if value is None:
            return None
        value = as_ist(value)
        return int(value.timestamp() * 1000)

    old_status = record.get("status", "PRESENT")
    old_in = as_ist(record.get("punch_in"))
    old_out = as_ist(record.get("punch_out"))

    new_status = body.status if body.status is not None else old_status
    new_in = (
        from_epoch_ms(body.punch_in)
        if body.punch_in is not None else old_in
    )
    new_out = (
        from_epoch_ms(body.punch_out)
        if body.punch_out is not None else old_out
    )

    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422, "Cannot supply punch times for ABSENT or LEAVE"
            )
        new_in = None
        new_out = None

    if new_status == "PRESENT" and new_in is None:
        raise HTTPException(422, "PRESENT requires a punch_in time")

    
    if new_in is not None:
        start_h, start_m = map(int, emp["shift_start"].split(":"))
        end_h, end_m = map(int, emp["shift_end"].split(":"))

        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        punch_date = new_in.date().isoformat()

        if end_minutes <= start_minutes:
            expected_date = (
                new_in.date() - timedelta(days=1)
                if new_in.hour * 60 + new_in.minute < end_minutes
                else new_in.date()
            ).isoformat()
        else:
            expected_date = punch_date

        if expected_date != date:
            raise HTTPException(
                422,
                "punch_in does not belong to the attendance date's shift"
            )


        
    if new_in is not None:
        new_in = truncate_to_seconds(as_ist(new_in))

    if new_out is not None:
        new_out = truncate_to_seconds(as_ist(new_out))

    if new_out is not None:
        if new_in is None:
            raise HTTPException(422, "punch_out requires punch_in")

        elapsed = (new_out - new_in).total_seconds()

        if elapsed <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")

        if elapsed > 24 * 60 * 60:
            raise HTTPException(422, "Punch duration cannot exceed 24 hours")

    if new_in is not None:
        late_minutes = compute_late_minutes(
            new_in,
            emp["shift_start"],
            emp["shift_end"],
            date,
        )
    else:
        late_minutes = 0


    if new_in is not None and new_out is not None:
        work_hours = compute_work_hours(new_in, new_out)

        overtime_minutes = compute_overtime(
            new_out,
            emp["shift_start"],
            emp["shift_end"],
            date,
        )

        half_day = work_hours < 4.50
    else:
        work_hours = None
        overtime_minutes = 0
        half_day = False

    proposed = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {}
    for field, new_value in proposed.items():
        old_value = record.get(field)

        if field in {"punch_in", "punch_out"}:
            old_value = as_ist(old_value)
            if old_value != new_value:
                changes[field] = {
                    "from": to_epoch_ms(old_value),
                    "to": to_epoch_ms(new_value),
                }
        elif old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(422, "The request does not change the record")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by.strip(),
        "reason": body.reason.strip(),
        "changes": changes,
    }

    old_history = record.get("history", [])

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": old_history,
        },
        {
            "$set": proposed,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409, "Attendance record changed concurrently; fetch it and retry"
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_api(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Validate YYYY-MM
    try:
        month_date = datetime.strptime(month, "%Y-%m").date()
        if month_date.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format, for example 2026-07"
        )

    # Verify employee
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, month_num = month_date.year, month_date.month
    days_in_month = calendar.monthrange(year, month_num)[1]
    month_end = date(year, month_num, days_in_month)

    # Apply R7: working days begin on joined_on, if later than month start
    joined_on = date.fromisoformat(emp["joined_on"])
    effective_start = max(month_date.replace(day=1), joined_on)

    working_days = sum(
        1
        for day in range(effective_start.day, days_in_month + 1)
        if effective_start <= date(year, month_num, day)
        and date(year, month_num, day).weekday() < 5
    ) if effective_start <= month_end else 0

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month + "-01",
                    "$lte": month + f"-{days_in_month:02d}"
                }
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    summary = results[0] if results else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0
    }

    present_days = summary["present_days"]

    # R8: half-up rounding; attendance percentage uses 4 decimals
    attendance_pct = None
    if working_days > 0:
        attendance_pct = float(
            (Decimal(str(present_days)) / Decimal(working_days) * 100)
            .quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary["leave_days"],
        "late_count": summary["late_count"],
        "total_late_minutes": summary["total_late_minutes"],
        "total_overtime_minutes": summary["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    # Validate month format: YYYY-MM
    try:
        month_date = datetime.strptime(month, "%Y-%m").date()
        if month_date.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format, for example 2026-07"
        )

    year, month_num = month_date.year, month_date.month
    days_in_month = calendar.monthrange(year, month_num)[1]
    month_end = date(year, month_num, days_in_month)

    # Find employees who had joined by the end of the requested month.
    employee_filter = {}
    if department is not None:
        employee_filter["department"] = department

    employees = list(db.employees.find(employee_filter))
    eligible_employees = [
        emp for emp in employees
        if date.fromisoformat(emp["joined_on"]) <= month_end
    ]

    # Department headcount includes employees without attendance logs.
    headcounts = {}
    for emp in eligible_employees:
        dept = emp["department"]
        headcounts[dept] = headcounts.get(dept, 0) + 1

    # Only eligible employee codes should contribute attendance metrics.
    eligible_codes = [emp["emp_code"] for emp in eligible_employees]

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": eligible_codes},
                "date": {
                    "$gte": f"{month}-01",
                    "$lte": f"{month}-{days_in_month:02d}"
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"},
        {
            "$group": {
                "_id": "$employee.department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]}
                                ]
                            },
                            "$work_hours",
                            0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "ON_DUTY"]},
                            1,
                            0
                        ]
                    }
                }
            }
        }
    ]

    aggregated = list(db.attendance_logs.aggregate(pipeline))
    metrics_by_department = {
        item["_id"]: item for item in aggregated
    }

    # Include departments with eligible employees but no attendance logs.
    items = []
    for dept, headcount in headcounts.items():
        metrics = metrics_by_department.get(dept, {})
        hours_count = metrics.get("work_hours_count", 0)

        avg_work_hours = None
        if hours_count:
            avg_work_hours = float(
                (
                    Decimal(str(metrics["work_hours_sum"]))
                    / Decimal(hours_count)
                ).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP
                )
            )

        items.append({
            "department": dept,
            "headcount": headcount,
            "present_days": metrics.get("present_days", 0),
            "avg_work_hours": avg_work_hours,
            "late_count": metrics.get("late_count", 0),
            "total_late_minutes": metrics.get("total_late_minutes", 0),
            "leave_count": metrics.get("leave_count", 0),
            "on_duty_count": metrics.get("on_duty_count", 0)
        })

    # Required alphabetical ordering
    items.sort(key=lambda item: item["department"])

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None
):
    # Validate month format
    try:
        month_date = datetime.strptime(month, "%Y-%m").date()
        if month_date.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format, for example 2026-07"
        )

    # Validate limit
    if limit < 1 or limit > 50:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 50"
        )

    days_in_month = calendar.monthrange(
        month_date.year, month_date.month
    )[1]

    # Aggregate late minutes by employee and join employee details.
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": f"{month}-01",
                    "$lte": f"{month}-{days_in_month:02d}"
                },
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$match": {"total_late_minutes": {"$gt": 0}}
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        }
    ])

    results = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking: 1, 2, 2, 4.
    items = []
    previous_total = None
    current_rank = 0

    for position, employee in enumerate(results, start=1):
        total = employee["total_late_minutes"]

        if total != previous_total:
            current_rank = position
            previous_total = total

        if current_rank > limit:
            break

        items.append({
            "rank": current_rank,
            "emp_code": employee["emp_code"],
            "name": employee["name"],
            "department": employee["department"],
            "total_late_minutes": total,
            "late_count": employee["late_count"]
        })

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,from_date: str = Query(..., alias="from"),to_date: str = Query(..., alias="to")):
    # Swagger query parameter aliases are handled below through explicit names.
    # These checks prevent invalid or excessively long date ranges.
    if not from_date or not to_date:
        raise HTTPException(
            status_code=422,
            detail="Both from and to query parameters are required"
        )

    try:
        start_date = date.fromisoformat(from_date)
        end_date = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="from and to must use YYYY-MM-DD format"
        )

    if start_date.isoformat() != from_date or end_date.isoformat() != to_date:
        raise HTTPException(
            status_code=422,
            detail="from and to must use YYYY-MM-DD format"
        )

    if end_date < start_date:
        raise HTTPException(422, "to must be on or after from")

    day_count = (end_date - start_date).days + 1
    if day_count > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    # Confirm the department exists.
    employees = list(db.employees.find({"department": department}))
    if not employees:
        raise HTTPException(404, "Department not found")

    employee_codes = [emp["emp_code"] for emp in employees]

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": employee_codes},
                "date": {
                    "$gte": from_date,
                    "$lte": to_date
                }
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "date_value": {
                    "$dateFromString": {
                        "dateString": "$_id",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC"
                    }
                },
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
            "field": "date_value",
            "range": {
            "step": 1,
            "unit": "day",
            "bounds": [
                datetime.combine(start_date, datetime.min.time()),
                datetime.combine(end_date, datetime.min.time())
                + timedelta(days=1)
                ]
                }
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_value",
                        "timezone": "UTC"
                    }
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date_value"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_value"}, 6]}
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": {
                                "$literal": [
                                    {
                                        "emp_code": emp["emp_code"],
                                        "joined_on": emp["joined_on"]
                                    }
                                    for emp in employees
                                ]
                            },
                            "as": "emp",
                            "cond": {
                                "$lte": ["$$emp.joined_on", "$date"]
                            }
                        }
                    }
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4
                            ]
                        },
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$moving_avg_7d", 10000]},
                                            0.5
                                        ]
                                    }
                                },
                                10000
                            ]
                        },
                        None
                    ]
                }

            }
        },
        {"$sort": {"date": 1}}
    ]

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "department": department,
        "items": items
    }

def validate_explain_month(month: str):
    try:
        parsed = datetime.strptime(month, "%Y-%m").date()
        if parsed.strftime("%Y-%m") != month:
            raise ValueError
        return parsed
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format"
        )


def run_explain_find(query, sort=None, skip=0, limit=None):
    command = {
        "find": "attendance_logs",
        "filter": query,
        "skip": skip,
    }

    if sort:
        command["sort"] = sort

    if limit is not None:
        command["limit"] = limit

    return db.command(
        "explain",
        command,
        verbosity="executionStats"
    )


def run_explain_aggregate(pipeline):
    return db.command(
        "explain",
        {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        },
        verbosity="executionStats"
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of {sorted(allowed)}"
        )

    # 1. Attendance list: same filters, sorting and pagination.
    if endpoint == "attendance_list":
        if page < 1 or page_size < 1:
            raise HTTPException(
                status_code=422,
                detail="page and page_size must be at least 1"
            )

        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if date_from or date_to:
            query["date"] = {}

            if date_from:
                query["date"]["$gte"] = date_from

            if date_to:
                query["date"]["$lte"] = date_to

        if status:
            query["status"] = status

        result = run_explain_find(
            query,
            sort={"date": -1},
            skip=(page - 1) * page_size,
            limit=page_size
        )

    # 2. Employee monthly analytics: reproduce its aggregation.
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required"
            )

        month_date = validate_explain_month(month)

        if not db.employees.find_one({"emp_code": emp_code}):
            raise HTTPException(404, "Employee not found")

        days = calendar.monthrange(
            month_date.year, month_date.month
        )[1]

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": month + "-01",
                        "$lte": f"{month}-{days:02d}"
                    }
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
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {
                                            "$in": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                [2, 3, 4, 5, 6]
                                            ]
                                        }
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1
                                    ]
                                },
                                0
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1, 0
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1, 0
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    }
                }
            }
        ]

        result = run_explain_aggregate(pipeline)

    # 3. Department summary: same eligible employees and aggregation.
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")

        month_date = validate_explain_month(month)
        days = calendar.monthrange(
            month_date.year, month_date.month
        )[1]
        month_end = date(
            month_date.year, month_date.month, days
        )

        employee_filter = {}
        if department is not None:
            employee_filter["department"] = department

        employees = list(db.employees.find(employee_filter))
        eligible_codes = [
            emp["emp_code"]
            for emp in employees
            if date.fromisoformat(emp["joined_on"]) <= month_end
        ]

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": eligible_codes},
                    "date": {
                        "$gte": f"{month}-01",
                        "$lte": f"{month}-{days:02d}"
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee"
                }
            },
            {"$unwind": "$employee"},
            {
                "$group": {
                    "_id": "$employee.department",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {
                                            "$in": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                [2, 3, 4, 5, 6]
                                            ]
                                        }
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5, 1
                                    ]
                                },
                                0
                            ]
                        }
                    },
                    "work_hours_sum": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$work_hours", None]}
                                    ]
                                },
                                "$work_hours", 0
                            ]
                        }
                    },
                    "work_hours_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$work_hours", None]}
                                    ]
                                },
                                1, 0
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1, 0
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1, 0
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "ON_DUTY"]},
                                1, 0
                            ]
                        }
                    }
                }
            }
        ]

        result = run_explain_aggregate(pipeline)

    # 4. Late leaderboard: same grouping, lookup and sorting.
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")

        validate_explain_month(month)

        if limit < 1 or limit > 50:
            raise HTTPException(
                status_code=422,
                detail="limit must be between 1 and 50"
            )

        days = calendar.monthrange(
            int(month[:4]), int(month[5:7])
        )[1]

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": f"{month}-01",
                        "$lte": f"{month}-{days:02d}"
                    },
                    "late_minutes": {"$gt": 0}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee"
                }
            },
            {"$unwind": "$employee"}
        ]

        if department is not None:
            pipeline.append({
                "$match": {"employee.department": department}
            })

        pipeline.extend([
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1
                }
            }
        ])

        result = run_explain_aggregate(pipeline)

    # 5. Department trend: same date range, grouping and densification.
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required"
            )

        try:
            start_date = date.fromisoformat(from_date)
            end_date = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="from and to must use YYYY-MM-DD format"
            )

        if (start_date.isoformat() != from_date
                or end_date.isoformat() != to_date):
            raise HTTPException(
                status_code=422,
                detail="from and to must use YYYY-MM-DD format"
            )

        if end_date < start_date:
            raise HTTPException(422, "to must be on or after from")

        if (end_date - start_date).days + 1 > 92:
            raise HTTPException(
                422, "Date range cannot exceed 92 days"
            )

        employees = list(db.employees.find(
            {"department": department}
        ))

        if not employees:
            raise HTTPException(404, "Department not found")

        employee_codes = [emp["emp_code"] for emp in employees]

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": employee_codes},
                    "date": {"$gte": from_date, "$lte": to_date}
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5, 1
                                    ]
                                },
                                0
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1, 0
                            ]
                        }
                    }
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": "$_id",
                    "date_value": {
                        "$dateFromString": {
                            "dateString": "$_id",
                            "format": "%Y-%m-%d",
                            "timezone": "UTC"
                        }
                    },
                    "present_count": 1,
                    "late_count": 1
                }
            },
            {
                "$densify": {
                    "field": "date_value",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [
                            datetime.combine(
                                start_date, datetime.min.time()
                            ),
                            datetime.combine(
                                end_date, datetime.min.time()
                            ) + timedelta(days=1)
                        ]
                    }
                }
            },
            {
                "$set": {
                    "date": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$date_value",
                            "timezone": "UTC"
                        }
                    },
                    "present_count": {
                        "$ifNull": ["$present_count", 0]
                    },
                    "late_count": {"$ifNull": ["$late_count", 0]}
                }
            },
            {
                "$set": {
                    "is_working_day": {
                        "$and": [
                            {"$gte": [{"$dayOfWeek": "$date_value"}, 2]},
                            {"$lte": [{"$dayOfWeek": "$date_value"}, 6]}
                        ]
                    },
                    "headcount": {
                        "$size": {
                            "$filter": {
                                "input": {
                                    "$literal": [
                                        {
                                            "emp_code": emp["emp_code"],
                                            "joined_on": emp["joined_on"]
                                        }
                                        for emp in employees
                                    ]
                                },
                                "as": "emp",
                                "cond": {
                                    "$lte": ["$$emp.joined_on", "$date"]
                                }
                            }
                        }
                    }
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]}
                                ]
                            },
                            {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        {
                                                            "$divide": [
                                                                "$present_count",
                                                                "$headcount"
                                                            ]
                                                        },
                                                        10000
                                                    ]
                                                },
                                                0.5
                                            ]
                                        }
                                    },
                                    10000
                                ]
                            },
                            None
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]}
                        }
                    }
                }
            },
            {
                "$sort": {"date": 1}
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": {
                        "$cond": [
                            {"$ne": ["$moving_avg_7d", None]},
                            {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        "$moving_avg_7d",
                                                        10000
                                                    ]
                                                },
                                                0.5
                                            ]
                                        }
                                    },
                                    10000
                                ]
                            },
                            None
                        ]
                    }
                }
            }
            ]


        result = run_explain_aggregate(pipeline)

    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": json.loads(json_util.dumps(result))
    }
