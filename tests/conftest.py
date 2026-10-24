from __future__ import annotations

import os

import pytest

from raceproof.database import create_database_engine


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get(
        "RACEPROOF_DATABASE_URL",
        "postgresql+psycopg://raceproof:raceproof_dev_only@localhost:55432/raceproof",
    )


@pytest.fixture(scope="session")
def postgres_engine(database_url: str):
    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any connectivity failure skips integration tests
        pytest.skip(f"isolated PostgreSQL is unavailable: {exc}")
    yield engine
    engine.dispose()
