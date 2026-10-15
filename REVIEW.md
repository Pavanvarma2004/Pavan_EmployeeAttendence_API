# REVIEW.md

## Defects Identified in the Starter Code

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees` | Incorrect pagination skip can omit the first page of employees. | Request page 1 when multiple pages exist and inspect the results. | Calculate skip using `(page - 1) * page_size`. |
| 2 | `create_employee` | A pre-insert existence check alone cannot prevent concurrent duplicate employee creation. | Send simultaneous requests with the same employee code. | Use a unique employee-code index and return HTTP 409 for `DuplicateKeyError`. |
| 3 | `punch_in` | Checking for an existing record before inserting does not prevent concurrent duplicate punch-ins. | Send simultaneous punch-ins for the same employee and attendance date. | Enforce a unique `(emp_code, date)` index and translate duplicate-key errors into HTTP 409. |
| 4 | `punch_out` | Updating without a conditional check can allow concurrent requests to overwrite the punch-out time. | Send simultaneous punch-out requests for one open attendance record. | Update only when `punch_out` is `None` and return HTTP 409 if the update loses the race. |
| 5 | `create_employee` | Invalid joining dates or shift-time formats can break attendance calculations. | Submit malformed dates or shift times. | Validate the date and time formats and reject invalid values with HTTP 422. |
| 6 | `list_attendance` | Invalid date filters or a reversed date range can produce invalid requests. | Submit malformed dates or a `date_from` later than `date_to`. | Validate date formats and reject reversed ranges with HTTP 422. |
| 7 | `regularize_attendance` | Concurrent updates can lose changes or history entries without concurrency protection. | Submit simultaneous regularizations and inspect the final record history. | Compare the previous history during the update and return HTTP 409 when the record has changed. |
| 8 | `punch_out` | Invalid punch-out times can create inconsistent attendance durations. | Submit a punch-out before punch-in or more than 24 hours later. | Reject invalid durations with HTTP 422. |
| 9 | `department_summary` | Loading every employee into Python and building a large `$in` filter may scale poorly. | Test with a large employee dataset and inspect query execution statistics. | The current implementation still uses this approach; moving eligibility and aggregation fully into MongoDB is a recommended improvement. |
| 10 | `department_trend` | `$densify` may not produce the requested calendar rows when no attendance documents match the date range. | Query a valid department and date range with no matching attendance records. | The current pipeline uses `$densify`, but the entirely empty-input case still needs verification and may require generating the calendar range independently. |

## Items Checked and Not Considered Defects

- The unique employee-code index and unique `(emp_code, date)` attendance index support the required uniqueness guarantees.
- The current pagination calculation is correct.
- Duplicate-key handling in employee creation and punch-in addresses concurrent insert conflicts.
- Conditional punch-out updates and history-based regularization updates provide concurrency safeguards.
- The monthly analytics and leaderboard use MongoDB aggregation rather than calculating all attendance totals in Python.
- Competition ranking (`1, 2, 2, 4`) is intentional, and alphabetical employee-code ordering provides deterministic tie ordering.
- The department summary intentionally includes eligible employees who have no attendance logs.
- The current department summary and trend implementations still warrant scalability and empty-range testing before claiming those cases are fully handled.
