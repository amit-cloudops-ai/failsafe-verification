# Failsafe Verification

A Python verification service and test-analysis project focused on identifying verification gaps, recording failure hypotheses, and producing a readable report.

## What is in this repository

| Path | Purpose |
|---|---|
| `app/` | FastAPI application and supporting persistence/cache modules |
| `analyzer/scan.py` | Scanning and analysis logic |
| `analyzer/inventory.json` | Inventory data used by the analyzer |
| `analyzer/failure_hypotheses.json` | Failure hypotheses used during analysis |
| `analyzer/test_coverage_map.json` | Mapping between areas and verification coverage |
| `tests/` | Automated tests, including payment and verification-gap cases |
| `report/generate_report.py` | Report generation |
| `report/index.html` | HTML report view |
| `requirements.txt` | Python dependencies |

## Tech stack

- Python
- FastAPI and Uvicorn
- SQLAlchemy and Pydantic
- Pytest and HTTPX

## Run locally

Use Python 3.11+ and a virtual environment.

```bash
python -m venv .venv
```

Activate the environment:

```bash
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Install dependencies and run the test suite:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
pytest -q
```

Start the API locally:

```bash
uvicorn app.main:app --reload
```

Open the local API documentation at `http://127.0.0.1:8000/docs`.

## Explore the report

Review `report/index.html` for the report interface. To regenerate report output, inspect the command-line options in `report/generate_report.py` first and run it with the inputs appropriate to your checkout.

## Engineering notes

- Treat analyzer output as evidence to investigate, not as a substitute for engineering judgment.
- Keep test data and local configuration free of real credentials or personal data.
- Run tests before submitting changes and add regression tests when fixing a discovered gap.
- Document assumptions, known limitations, and reproducible steps for any reported issue.

## Project status

This README describes the current repository layout and local development entry points. Validate behavior against the code and test suite in your checkout; no CI status or production-readiness claim is implied.
