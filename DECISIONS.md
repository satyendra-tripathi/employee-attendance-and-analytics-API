# DECISIONS.md

1. **Indexes.**
   We created the following indexes:
   - `employees: {emp_code: 1}` (unique) prevents duplicate employee codes and accelerates single-employee lookups.
   - `employees: {department: 1, joined_on: 1}` serves department summary and trend queries filtering active employees by headcount dates.
   - `attendance_logs: {emp_code: 1, date: 1}` (unique) guarantees exactly one log per employee per day and speeds up monthly aggregations.
   - `attendance_logs: {date: -1, emp_code: 1}` accelerates paginated attendance listings with index-only sorting.
   - `attendance_logs: {date: 1, late_minutes: 1}` powers the late leaderboard by filtering positive late minutes across date intervals.
   - `attendance_logs: {emp_code: 1, punch_in: -1}` enables sub-millisecond lookups for the latest punch-in during punch-out.
   *Rejected index:* We rejected an index on `{status: 1}` alone due to low cardinality (5 values), which leads to poor query selectivity.

2. **Punch-in race.**
   When two identical punch-in requests arrive simultaneously, both compute the attendance date and attempt `db.attendance_logs.insert_one(doc)`. The unique compound index on `(emp_code, date)` acts as the concurrency barrier at the storage engine level. One request succeeds and returns HTTP 201; the second immediately raises a `DuplicateKeyError`, which our exception handler intercepts and converts into a clean HTTP 409 Conflict.

3. **Ties.**
   We use MongoDB's `$rank` window operator, which implements standard competition ranking (1, 2, 2, 4). When employees tie at the cutoff of `limit` (e.g. both rank 10), both are returned because our filter checks `rank <= limit`. The endpoint returns all tied candidates without arbitrarily dropping anyone, strictly satisfying the contract.

4. **Headcount.**
   The department summary pipeline initiates its aggregation on the `employees` collection matching `joined_on <= month_end`, rather than `attendance_logs`. It then performs a `$lookup` into `attendance_logs` followed by an `$unwind` with `preserveNullAndEmptyArrays: True`. Employees with zero logs remain in the pipeline with empty log fields, and their distinct employee codes are counted into the department set via `$addToSet: "$emp_code"`.

5. **One thing you would change for 100x data.**
   At 100x scale (~10 million attendance records), on-the-fly cross-collection `$lookup` aggregations for monthly summaries would saturate CPU and I/O. We would implement an asynchronous daily rollup worker or pre-aggregated monthly summary collection updated incrementally on each punch-out/regularization.
