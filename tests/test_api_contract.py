import pytest
from fastapi.testclient import TestClient
from app.main import app
import mongomock

@pytest.fixture(autouse=True)
def mock_mongo(monkeypatch):
    mock_client = mongomock.MongoClient()
    mock_db = mock_client["test_attendance_db"]
    # Create indexes in mock
    mock_db.employees.create_index([("emp_code", 1)], unique=True)
    mock_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    monkeypatch.setattr("app.main.client", mock_client)
    monkeypatch.setattr("app.main.db", mock_db)
    yield mock_db

def test_employee_creation_and_validation():
    with TestClient(app) as client:
        # Invalid emp_code
        res = client.post("/employees", json={
            "emp_code": "INVALID",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        assert res.status_code == 422

        # Shift start == shift end
        res = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:00",
            "shift_end": "09:00",
            "joined_on": "2026-01-01"
        })
        assert res.status_code == 422

        # Successful creation
        res = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        assert res.status_code == 201
        body = res.json()
        assert body["emp_code"] == "EMP0001"
        assert isinstance(body["created_at"], int)
        assert body["created_at"] >= 100000000000

        # Duplicate emp_code -> 409
        res_dup = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Another User",
            "email": "another@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        assert res_dup.status_code == 409

def test_timestamp_seconds_rejected_with_422():
    with TestClient(app) as client:
        # Sending seconds (10 digits) instead of milliseconds (13 digits) must fail with 422
        res = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": 1783312500 # seconds!
        })
        assert res.status_code == 422

def test_pagination_validation():
    with TestClient(app) as client:
        # page < 1 -> 422
        res = client.get("/employees?page=0")
        assert res.status_code == 422

        # page_size > 100 -> 422
        res = client.get("/employees?page_size=101")
        assert res.status_code == 422

def test_attendance_list_date_range_validation():
    with TestClient(app) as client:
        # date_from > date_to -> 422
        res = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
        assert res.status_code == 422

def test_department_trend_range_validation():
    with TestClient(app) as client:
        # to < from -> 422
        res = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-05")
        assert res.status_code == 422

        # range > 92 days -> 422
        res = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01")
        assert res.status_code == 422
