# Employee Attendance & Analytics API

A FastAPI application backed by MongoDB for employee management, attendance tracking, attendance regularization, and analytics.

## Requirements

- Python 3.10+
- MongoDB 6.0 or newer
- Dependencies listed in `requirements.txt`

## Local Setup

### 1. Create and activate a virtual environment

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure MongoDB

Set the `MONGO_URI` and `MONGO_DB` environment variables, or use a local `.env` file if supported by your setup.

Example configuration:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Start MongoDB using Docker if required:

```powershell
docker run -d --name attendance-mongo -p 27017:27017 -v attendance_mongo_data:/data/db mongo:7
```

If the container already exists, start it instead:

```powershell
docker start attendance-mongo
```

### 3. Load sample data

From the project root, run:

```powershell
python sample_seed.py
```

Run this only when you need the supplied sample data.

### 4. Start the API

```powershell
uvicorn app.main:app --port 8000
```

For development with automatic reload:

```powershell
uvicorn app.main:app --reload --port 8000
```

## API Documentation

- Health check: http://127.0.0.1:8000/health
- Swagger UI: http://127.0.0.1:8000/docs
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json

## Features

- Employee creation and paginated listing
- Attendance listing
- Punch-in and punch-out
- Attendance regularization with history
- Monthly employee attendance analytics
- Department attendance summary
- Late-arrival leaderboard
- Department attendance trends
- MongoDB query-plan inspection

## Submission

The repository should contain:

- `app/main.py`
- `requirements.txt`
- `README.md`
- `REVIEW.md`
- `DECISIONS.md`

Do not commit `.env` files, credentials, secrets, virtual environments, or unnecessary local data dumps.

The application must start with:

```bash
uvicorn app.main:app --port 8000
```