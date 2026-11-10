"""Run isolated specification PostgreSQL suites using only the test .env URL."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

from dotenv import dotenv_values


root = Path(__file__).resolve().parents[1]
os.chdir(root)
sys.path.insert(0, str(root))
test_url = dotenv_values(root / ".env").get("TEST_DATABASE_URL")
if not test_url:
    raise SystemExit("TEST_DATABASE_URL is missing or empty in .env")
if not test_url.startswith(("postgresql://", "postgresql+psycopg://", "postgresql+psycopg2://")):
    raise SystemExit("TEST_DATABASE_URL must use a PostgreSQL driver")

# Set before importing application configuration. Never connect the application
# DATABASE_URL while running the isolated test fixtures.
os.environ["TEST_DATABASE_URL"] = test_url
os.environ["DATABASE_URL"] = test_url
os.environ.setdefault("PGCONNECT_TIMEOUT", "15")

secrets = [test_url]
password = urlsplit(test_url).password
if password:
    secrets.extend([password, unquote(password)])

def redact(value):
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[redacted]")
    return value

captured = StringIO()
with redirect_stdout(captured), redirect_stderr(captured):
    import pytest
    result = pytest.main([
        "backend/tests/test_specification_review_postgres.py",
        "backend/tests/test_specification_review_postgres_concurrency.py",
        "backend/tests/test_specification_review_api_postgres.py",
        "backend/tests/test_specification_phase5_postgres.py",
        "-q", "--tb=short", "--disable-warnings",
    ])

output = redact(captured.getvalue())
(root / "tmp/specification-postgres-results.txt").write_text(output, encoding="utf-8")
print(output)
raise SystemExit(result)
