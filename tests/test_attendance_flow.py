import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from app.main import app, datetime_to_epoch_ms
import mongomock

@pytest.fixture(autouse=True)
def mock_mongo(monkeypatch):
    mock_client = mongomock.MongoClient()
    mock_db = mock_client["test_attendance_db"]
    mock_db.employees.create_index([("emp_code", 1)], unique=True)
    mock_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    monkeypatch.setattr("app.main.client", mock_client)
    monkeypatch.setattr("app.main.db", mock_db)
    yield mock_db

def test_full_attendance_lifecycle():
    with TestClient(app) as client:
        # 1. Create employee
        res_emp = client.post("/employees", json={
            "emp_code": "EMP0100",
            "name": "Alice Smith",
            "email": "alice@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01"
        })
        assert res_emp.status_code == 201

        # 2. Punch-in on 2026-07-06 at 09:45:00 IST (15 min after shift start, > 10 min grace -> late 15 min)
        # 09:45:00 IST = 04:15:00 UTC
        in_dt = datetime(2026, 7, 6, 4, 15, 0, tzinfo=timezone.utc)
        in_ms = datetime_to_epoch_ms(in_dt)

        res_in = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0100",
            "punched_at": in_ms,
            "status": "PRESENT"
        })
        assert res_in.status_code == 201
        data_in = res_in.json()
        assert data_in["emp_code"] == "EMP0100"
        assert data_in["date"] == "2026-07-06"
        assert data_in["status"] == "PRESENT"
        assert data_in["punch_in"] == in_ms
        assert data_in["punch_out"] is None
        assert data_in["work_hours"] is None
        assert data_in["late_minutes"] == 15
        assert data_in["overtime_minutes"] == 0
        assert data_in["half_day"] is False

        # Duplicate punch-in on same day -> 409
        res_dup_in = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0100",
            "punched_at": in_ms + 1000,
        })
        assert res_dup_in.status_code == 409

        # 3. Punch-out at 19:15:00 IST (13:45:00 UTC)
        # Shift end was 18:30 IST. 19:15 is 45 min after shift end -> overtime = 45 min
        # Total duration: 09:45 to 19:15 = 9.5 hours
        out_dt = datetime(2026, 7, 6, 13, 45, 0, tzinfo=timezone.utc)
        out_ms = datetime_to_epoch_ms(out_dt)

        res_out = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0100",
            "punched_at": out_ms
        })
        assert res_out.status_code == 200
        data_out = res_out.json()
        assert data_out["punch_out"] == out_ms
        assert data_out["work_hours"] == 9.5
        assert data_out["late_minutes"] == 15
        assert data_out["overtime_minutes"] == 45
        assert data_out["half_day"] is False

        # Duplicate punch-out -> 409
        res_dup_out = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0100",
            "punched_at": out_ms + 5000
        })
        assert res_dup_out.status_code == 409

        # 4. Regularize attendance (Manager corrects punch-in to 09:30:00 IST = 04:00:00 UTC)
        reg_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        reg_in_ms = datetime_to_epoch_ms(reg_in_dt)

        res_reg = client.patch("/attendance/EMP0100/2026-07-06", json={
            "punch_in": reg_in_ms,
            "reason": "Traffic delay approved by manager",
            "regularized_by": "MGR001"
        })
        assert res_reg.status_code == 200
        data_reg = res_reg.json()
        assert data_reg["punch_in"] == reg_in_ms
        assert data_reg["late_minutes"] == 0  # no longer late
        assert len(data_reg["history"]) == 1
        hist = data_reg["history"][0]
        assert hist["by"] == "MGR001"
        assert hist["reason"] == "Traffic delay approved by manager"
        assert "punch_in" in hist["changes"]
        assert "late_minutes" in hist["changes"]

        # 5. Regularize to ABSENT clears punches
        res_reg_absent = client.patch("/attendance/EMP0100/2026-07-06", json={
            "status": "ABSENT",
            "reason": "Marked absent per HR review",
            "regularized_by": "HR001"
        })
        assert res_reg_absent.status_code == 200
        data_absent = res_reg_absent.json()
        assert data_absent["status"] == "ABSENT"
        assert data_absent["punch_in"] is None
        assert data_absent["punch_out"] is None
        assert data_absent["work_hours"] is None
        assert data_absent["late_minutes"] == 0
        assert data_absent["overtime_minutes"] == 0
        assert len(data_absent["history"]) == 2
