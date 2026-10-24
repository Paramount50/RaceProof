from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from raceproof.config import get_settings
from raceproof.contracts import EventKind
from raceproof.events import EventRecorder

_actor_context = threading.local()


def set_sql_actor(actor: str | None) -> None:
    _actor_context.actor = actor


def create_database_engine(url: str | None = None, recorder: EventRecorder | None = None) -> Engine:
    settings = get_settings()
    resolved_url = url or settings.database_url
    parsed = make_url(resolved_url)
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError("RaceProof requires PostgreSQL for executable concurrency evidence")
    if not settings.allow_nonlocal_database and parsed.host not in {
        "localhost",
        "127.0.0.1",
        "::1",
        None,
    }:
        raise ValueError(
            "non-local PostgreSQL targets are disabled; set "
            "RACEPROOF_ALLOW_NONLOCAL_DATABASE=true only for an isolated test database"
        )
    if parsed.host == "localhost":
        resolved_url = parsed.set(host="127.0.0.1").render_as_string(hide_password=False)
    engine = create_engine(
        resolved_url,
        isolation_level="READ COMMITTED",
        pool_pre_ping=True,
        future=True,
        connect_args={"connect_timeout": 5},
    )
    if recorder is not None:

        @event.listens_for(engine, "before_cursor_execute")
        def capture_sql(
            _connection: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            actor = getattr(_actor_context, "actor", None)
            if actor == "__lock_observer__":
                return
            compact = " ".join(statement.split())
            driver_connection = getattr(_connection.connection, "driver_connection", None)
            backend_pid = getattr(getattr(driver_connection, "info", None), "backend_pid", None)
            recorder.add(
                EventKind.SQL,
                stage="execute",
                actor=actor,
                action="sql",
                detail={"statement": compact[:600], "backend_pid": backend_pid},
            )

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


@contextmanager
def actor_session(engine: Engine, actor: str) -> Iterator[Session]:
    set_sql_actor(actor)
    try:
        with session_factory(engine).begin() as session:
            yield session
    finally:
        set_sql_actor(None)
