# Employee Attendance & Analytics API

Production-ready implementation of the Employee Attendance & Analytics API built with FastAPI, Python, and MongoDB.

## Features
- **Strict OpenAPI Contract**: Fully implements all endpoints specified in `openapi.yaml` (v2.0.0).
- **Domain Business Rules (R1 - R10)**:
  - IST timezone handling (UTC+05:30) with support for overnight shifts (`shift_end <= shift_start`).
  - Whole-second instant truncation and millisecond epoch timestamps.
  - 10-minute grace period (`strictly > 10 min` incurs late minutes from shift start).
  - Half-up rounding (`ROUND_HALF_UP`) for work hours (2 decimals), attendance percentage, and rates (4 decimals).
  - Overtime floor calculation with >= 30-minute threshold.
  - Mid-month joiner working days (Mon-Fri) and headcount progression (R9).
- **Concurrency & Atomic Operations**:
  - Unique compound index on `(emp_code, date)` guarantees prevention of duplicate punch-ins.
  - Atomic `find_one_and_update` on punch-out and regularizations with optimistic concurrency control.
- **Index-Optimized Analytics (Part B & C)**:
  - High-performance aggregation pipelines running on MongoDB.
  - Native `/admin/explain/{endpoint}` generating `executionStats` verifying `IXSCAN` index usage.

## Setup & Running

### 1. Virtual Environment & Dependencies
```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment
Set `MONGO_URI` to your MongoDB instance (Atlas cluster or MongoDB 6.0+):
```bash
# In .env (or exported in environment):
MONGO_URI=mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

### 3. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 4. Run Application
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Interactive API documentation will be available at `http://localhost:8000/docs`.

### 5. Run Test Suite
```bash
python -m pytest tests/ -v
```

## Documentation
- `REVIEW.md`: Detailed defect analysis, symptoms, and fixes for the starter code.
- `DECISIONS.md`: Architectural choices regarding indexing, concurrency, leaderboard ties, headcount, and horizontal scaling.
