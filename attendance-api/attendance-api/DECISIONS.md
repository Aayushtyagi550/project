# DECISIONS.md

1. **Indexes.** On logs: unique `(emp_code, date)`, which guarantees one record per day and serves punch-out, PATCH, the monthly summary and per-employee lists; and `(date -1, emp_code 1)`, which matches the list sort and serves date-range scans for the leaderboard and trend. On employees: unique `emp_code`, `(department, emp_code)`, `(department, joined_on)` and `joined_on`. Rejected: `(emp_code, punch_in)` for punch-out - one employee has few records, so a small in-memory sort is cheaper than another index.
2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets one insert win; the other gets `DuplicateKeyError`, which I return as 409. There is no check-then-insert.
3. **Ties.** `$rank` sorts on the total only, so ties share a rank (1, 2, 2, 4). `limit` filters on rank afterwards, so a tie at the cutoff is returned whole, possibly more than `limit` rows.
4. **Headcount.** The pipeline starts from `employees`; `$lookup` keeps people with no logs (`logs: []`), and `$group` counts employees, not log rows. Future joiners are removed by the `joined_on` match.
5. **100x data.** Store `department` on each log (or keep daily per-employee rollups) so the trend does not need an `$in` list of every `emp_code`.
