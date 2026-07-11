"""Tests for the SQLite request logger. No API keys required:
litellm's mock_response short-circuits the network call while still
firing the logging callbacks.

Run: python -m pytest gateway/test_sqlite_logger.py
"""

import json
import os
import sqlite3
import time

import pytest


@pytest.fixture()
def db(tmp_path):
    # GATEWAY_DB_PATH already points here: the autouse fixture in
    # conftest.py sets it to this exact tmp_path for every test.
    return tmp_path / "requests.db"


def wait_for_row(path, status, timeout=10):
    """Sync callbacks run in a worker thread; poll until the row lands."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            rows = sqlite3.connect(path).execute(
                "SELECT * FROM requests WHERE status = ?", (status,)
            ).fetchall()
            if rows:
                return rows
        time.sleep(0.2)
    raise AssertionError(f"no '{status}' row appeared in {path} within {timeout}s")


def test_success_is_logged(db):
    import litellm
    from sqlite_logger import logger_instance

    litellm.callbacks = [logger_instance]
    litellm.completion(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "ping"}],
        mock_response="pong",
    )

    rows = wait_for_row(db, "success")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests WHERE status = 'success'").fetchone()

    assert row["model"] == "gpt-3.5-turbo"
    assert row["response"] == "pong"
    assert json.loads(row["prompt"]) == [{"role": "user", "content": "ping"}]
    assert row["total_tokens"] is not None
    assert row["latency_ms"] is not None


def test_failure_is_logged(db):
    import litellm
    from sqlite_logger import logger_instance

    litellm.callbacks = [logger_instance]
    with pytest.raises(Exception):
        litellm.completion(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "ping"}],
            mock_response="litellm.InternalServerError",
        )

    wait_for_row(db, "failure")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM requests WHERE status = 'failure'").fetchall()
    assert any("InternalServerError" in (r["error"] or "") for r in rows)


def test_untagged_call_logs_real_traffic_kind(db):
    import litellm
    from sqlite_logger import logger_instance

    litellm.callbacks = [logger_instance]
    litellm.completion(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "ping"}],
        mock_response="pong",
    )

    wait_for_row(db, "success")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests WHERE status = 'success'").fetchone()
    assert row["traffic_kind"] == "real"


@pytest.mark.parametrize("kind", ["replay", "judge", "synthetic"])
def test_metadata_traffic_kind_is_logged(db, kind):
    import litellm
    from sqlite_logger import logger_instance

    litellm.callbacks = [logger_instance]
    litellm.completion(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "ping"}],
        mock_response="pong",
        metadata={"traffic_kind": kind},
    )

    wait_for_row(db, "success")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests WHERE status = 'success'").fetchone()
    assert row["traffic_kind"] == kind


def test_migration_adds_column_and_backfills_existing_rows(db):
    from sqlite_logger import _connect

    # Simulate a pre-migration database (no traffic_kind column).
    old_schema = """
    CREATE TABLE requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        model TEXT,
        provider_model TEXT,
        status TEXT NOT NULL,
        latency_ms REAL,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        total_tokens INTEGER,
        cost_usd REAL,
        prompt TEXT,
        response TEXT,
        error TEXT
    );
    """
    conn = sqlite3.connect(db)
    conn.execute(old_schema)
    conn.execute(
        "INSERT INTO requests (ts, status, prompt, response) VALUES (?, ?, ?, ?)",
        ("2026-01-01T00:00:00", "success", "hi", "hello"),
    )
    conn.execute(
        "INSERT INTO requests (ts, status, prompt, response) VALUES (?, ?, ?, ?)",
        ("2026-01-01T00:00:00", "success",
         "You are an impartial judge comparing two answers to the same task.", "EQUIVALENT"),
    )
    conn.commit()
    conn.close()

    migrated = _connect()
    rows = migrated.execute("SELECT prompt, traffic_kind FROM requests ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0][1] == "synthetic"
    assert rows[1][1] == "judge"
