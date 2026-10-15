import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from bson import Binary, ObjectId, Regex, Timestamp
from bson.decimal128 import Decimal128
from bson.int64 import Int64
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def explain_result():
    return {
        "queryPlanner": {
            "winningPlan": {
                "stage": "IXSCAN",
                "inputStage": {"stage": "FETCH", "timestamp": Timestamp(123456, 7)},
                "filter": {"employee_id": ObjectId("507f1f77bcf86cd799439011")},
            }
        },
        "executionStats": {
            "nReturned": Int64(2),
            "executionTimeMillis": 3,
            "nested": [
                {"value": Decimal128("12.50")},
                {"value": datetime(2026, 10, 14, tzinfo=timezone.utc)},
                {"value": Binary(b"plan", subtype=0)},
                {"value": Regex("^EMP", "i")},
            ],
        },
    }


@pytest.fixture
def mock_explain_db(monkeypatch, explain_result):
    mock_db = Mock()
    mock_db.command.return_value = explain_result
    mock_db.employees.find.return_value = [{"emp_code": "EMP704897"}]
    monkeypatch.setattr(main, "db", mock_db)
    return mock_db


def test_explain_response_serializes_nested_bson_values(explain_result):
    response = main._explain_response("attendance_list", "attendance_logs", explain_result)

    payload = json.loads(response.body)

    assert response.media_type == "application/json"
    assert payload["explain"]["queryPlanner"]["winningPlan"]["stage"] == "IXSCAN"
    assert payload["explain"]["queryPlanner"]["winningPlan"]["inputStage"]["timestamp"] == {
        "$timestamp": {"t": 123456, "i": 7}
    }
    assert payload["explain"]["executionStats"]["nReturned"] == 2
    assert payload["explain"]["executionStats"]["nested"][0]["value"] == {
        "$numberDecimal": "12.50"
    }
    assert payload["explain"]["executionStats"]["nested"][1]["value"] == {
        "$date": "2026-10-14T00:00:00Z"
    }


@pytest.mark.parametrize(
    ("endpoint", "query", "expected_collection"),
    [
        (
            "attendance_list",
            "emp_code=EMP704897&date_from=2026-10-14&date_to=2026-10-14&limit=10&page=1&page_size=20",
            "attendance_logs",
        ),
        ("employee_monthly", "emp_code=EMP704897&month=2026-10", "attendance_logs"),
        ("department_summary", "month=2026-10&department=Engineering", "employees"),
        ("late_leaderboard", "month=2026-10&limit=10", "attendance_logs"),
        (
            "department_trend",
            "department=Engineering&from=2026-10-01&to=2026-10-14",
            "attendance_logs",
        ),
    ],
)
def test_admin_explain_branches_return_serialized_bson(
    endpoint, query, expected_collection, mock_explain_db
):
    with TestClient(main.app) as client:
        response = client.get(f"/admin/explain/{endpoint}?{query}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["endpoint"] == endpoint
    assert payload["collection"] == expected_collection
    assert payload["explain"]["queryPlanner"]["winningPlan"]["stage"] == "IXSCAN"
    assert payload["explain"]["executionStats"]["nReturned"] == 2
    assert payload["explain"]["queryPlanner"]["winningPlan"]["inputStage"]["timestamp"] == {
        "$timestamp": {"t": 123456, "i": 7}
    }
    mock_explain_db.command.assert_called_once()
    assert mock_explain_db.command.call_args.kwargs["verbosity"] == "executionStats"
