"""Session fixtures shared by the model tests.

Two things are provided here and nowhere else:

* the database the tests talk to, taken from DATABASE_URL (never a hardcoded
  connection string, and never a credential in the repository);
* PHONE_HASH_KEY, defaulted to a throwaway value so a bare `pytest -q` works on a
  fresh checkout. The real key is supplied by the deployment environment and is
  never written down here.

Every test starts from empty tables: the autouse fixture truncates the three
tables before each case, so tests do not depend on each other or on file order.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Throwaway value for local runs; NOT a production key and not a secret.
TEST_ONLY_KEY = "local-test-key-rotate-in-deployment"
os.environ.setdefault("PHONE_HASH_KEY", TEST_ONLY_KEY)

TABLES = ("mini_users", "member_auth_applications", "mini_login_events")

#: Machine-generated assertion record, written to docs/checks/assertions.json.
EVIDENCE: dict[str, object] = {}


def run_alembic(*args: str, database_url: str | None = None) -> subprocess.CompletedProcess:
    """Run `python -m alembic ...` exactly as a user would, and return the process."""
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    env = {**os.environ, "DATABASE_URL": url, "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail(
            "DATABASE_URL is not set. Point it at a PostgreSQL 16 database, e.g. "
            "postgresql+psycopg://postgres@127.0.0.1:55432/mini_auth - see README "
            "section 'Running the tests'."
        )
    return url


@pytest.fixture(scope="session")
def engine(database_url: str):
    """Bring the schema to head with the real Alembic CLI, then hand out an engine."""
    result = run_alembic("upgrade", "head", database_url=database_url)
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    EVIDENCE["conftest_upgrade_head_exit"] = result.returncode
    eng = create_engine(database_url, future=True)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_tables(engine):
    """Truncate the three tables before every test."""
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE " + ", ".join(TABLES) + " CASCADE"))
    yield
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE " + ", ".join(TABLES) + " CASCADE"))


@pytest.fixture(scope="session")
def evidence() -> dict[str, object]:
    return EVIDENCE


@pytest.fixture(scope="session", autouse=True)
def _write_evidence_at_end():
    yield
    target = REPO_ROOT / "docs" / "checks" / "assertions.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(EVIDENCE, indent=2, sort_keys=True) + "\n", encoding="utf-8")
