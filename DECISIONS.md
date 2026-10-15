# DECISIONS.md

1. **Indexes.** A unique index on `employees.emp_code` prevents duplicate employee codes. A unique `(emp_code, date)` attendance index guarantees one record per employee per date and protects against duplicate punch-ins. Indexes beginning with `emp_code` and `date` support employee history and date filtering. I would benchmark a wider covering index before keeping it, because extra indexes increase write and storage costs.

2. **Punch-in race.** Both requests may initially find no record and then attempt insertion. MongoDB's unique `(emp_code, date)` index allows only one insert. The other raises `DuplicateKeyError`, which the API converts to HTTP 409; the winner returns HTTP 201.

3. **Ties.** The leaderboard uses competition ranking (`1, 2, 2, 4`). It sorts ties by `emp_code`. If a tie reaches the requested rank limit, all employees sharing that rank are included.

4. **Headcount.** Department headcount comes from eligible employee documents using `joined_on` and the requested month end. It therefore includes employees with no attendance logs; their attendance metrics default to zero.

5. **100x scale.** I would benchmark representative data and explain plans, then move employee eligibility and headcount calculations fully into MongoDB rather than building large employee-code lists in Python. I would also load-test concurrent writes and analytics.