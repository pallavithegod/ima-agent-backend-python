import os
import tempfile

# Point the app at a throwaway SQLite database before any app module loads;
# unit tests must not require a running Postgres.
_tmpdir = tempfile.mkdtemp(prefix="recallops-tests-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_tmpdir, 'test.db')}")
os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-that-is-long-enough-123456")
os.environ.setdefault("MONITOR_INTERVAL_MS", "0")

import pytest  # noqa: E402

from app.db.repo import init_database  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    init_database()
    yield
