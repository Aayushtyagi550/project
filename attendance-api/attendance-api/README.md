# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0 or newer). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). `GET /health` returns 200 once MongoDB answers.

## Notes
* Everything in the contract is implemented, including the explain endpoint. Analytics are aggregation pipelines
  (`$lookup`, `$setWindowFields` with `$rank`, `$densify`) and need MongoDB 6.0+.
* Derived fields are computed by integer arithmetic on epoch seconds and `Decimal` (half-up); analytics read the stored values.
* A concurrent regularization that loses the compare-and-set gets 409 (allowed by the contract); nothing is overwritten.
* `REVIEW.md` lists the defects found in the starter, `DECISIONS.md` answers the five questions.

## Tests (optional, in `tests/`)
```bash
pip install pytest mongomock httpx
pytest tests/test_rules.py tests/test_api_mock.py     # no database needed (rules, CRUD, races, history)
python tests/live_check.py                            # needs a real MongoDB: seeds an awkward dataset, checks all analytics + explain
```
