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


def test_success_is_logged(db, monkeypatch):
    # This test asserts on the real prompt/response text, so it must opt
    # into raw-text logging explicitly -- the default is now masked (see
    # test_raw_text_logging_disabled_by_default below).
    monkeypatch.setenv("GATEWAY_LOG_RAW_TEXT", "true")
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


def test_migration_adds_cache_token_columns(db):
    """A database that has traffic_kind but no cache columns gets the two
    nullable cache-token columns added on the next _connect(), without
    disturbing existing rows."""
    from sqlite_logger import _connect

    pre_cache_schema = """
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
        error TEXT,
        traffic_kind TEXT NOT NULL DEFAULT 'real'
    );
    """
    conn = sqlite3.connect(db)
    conn.execute(pre_cache_schema)
    conn.execute(
        "INSERT INTO requests (ts, status, prompt, response, traffic_kind)"
        " VALUES (?, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00", "success", "hi", "hello", "real"),
    )
    conn.commit()
    conn.close()

    migrated = _connect()
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(requests)")}
    assert "cache_creation_input_tokens" in columns
    assert "cache_read_input_tokens" in columns

    rows = migrated.execute(
        "SELECT prompt, cache_creation_input_tokens, cache_read_input_tokens"
        " FROM requests ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "hi"
    assert rows[0][1] is None
    assert rows[0][2] is None


def test_migration_from_pre_traffic_kind_db_also_adds_cache_columns(db):
    """A database that predates BOTH migrations (no traffic_kind, no
    cache columns) ends up with all of them after one _connect()."""
    from sqlite_logger import _connect

    ancient_schema = """
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
    conn.execute(ancient_schema)
    conn.execute(
        "INSERT INTO requests (ts, status, prompt, response) VALUES (?, ?, ?, ?)",
        ("2026-01-01T00:00:00", "success", "hi", "hello"),
    )
    conn.commit()
    conn.close()

    migrated = _connect()
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(requests)")}
    assert "traffic_kind" in columns
    assert "cache_creation_input_tokens" in columns
    assert "cache_read_input_tokens" in columns


def test_success_row_fills_cache_tokens_from_usage(db):
    """A response_obj carrying an Anthropic-shaped Usage (as litellm
    builds it from cache_creation_input_tokens / cache_read_input_tokens
    kwargs, verified empirically -- see sqlite_logger._cache_tokens) logs
    those counts into the new columns."""
    from litellm.types.utils import ModelResponse, Usage

    from sqlite_logger import logger_instance

    usage = Usage(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cache_creation_input_tokens=20,
        cache_read_input_tokens=30,
    )
    response_obj = ModelResponse(
        choices=[{"message": {"role": "assistant", "content": "hi there"}}],
        usage=usage,
    )
    kwargs = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "ping"}],
        "litellm_params": {"metadata": {}},
        "response_cost": 0.001,
    }
    import datetime

    now = datetime.datetime.now()
    logger_instance.log_success_event(kwargs, response_obj, now, now)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests WHERE status = 'success'").fetchone()
    assert row["cache_creation_input_tokens"] == 20
    assert row["cache_read_input_tokens"] == 30


def test_success_row_defaults_cache_tokens_to_null_without_usage_fields(db):
    """A response with a plain (non-Anthropic) usage object -- no cache
    fields at all -- must log NULL for both columns, not raise."""
    from litellm.types.utils import ModelResponse, Usage

    from sqlite_logger import logger_instance

    usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    response_obj = ModelResponse(
        choices=[{"message": {"role": "assistant", "content": "pong"}}],
        usage=usage,
    )
    kwargs = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "ping"}],
        "litellm_params": {"metadata": {}},
        "response_cost": 0.0001,
    }
    import datetime

    now = datetime.datetime.now()
    logger_instance.log_success_event(kwargs, response_obj, now, now)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests WHERE status = 'success'").fetchone()
    assert row["cache_creation_input_tokens"] is None
    assert row["cache_read_input_tokens"] is None


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


def test_migration_adds_category_column(db):
    """A database that has traffic_kind and cache columns but no category
    column gets the column added without losing existing rows."""
    from sqlite_logger import _connect

    pre_category_schema = """
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
        error TEXT,
        traffic_kind TEXT NOT NULL DEFAULT 'real',
        cache_creation_input_tokens INTEGER,
        cache_read_input_tokens INTEGER
    );
    """
    conn = sqlite3.connect(db)
    conn.execute(pre_category_schema)
    conn.execute(
        "INSERT INTO requests (ts, status, prompt, response, traffic_kind)"
        " VALUES (?, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00", "success", "hi", "hello", "real"),
    )
    conn.commit()
    conn.close()

    migrated = _connect()
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(requests)")}
    assert "category" in columns

    rows = migrated.execute(
        "SELECT prompt, category FROM requests ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "hi"
    assert rows[0][1] is None   # pre-existing row: NULL (no backfill needed)


def test_metadata_category_is_logged(db):
    """metadata.category from a litellm call reaches the category column."""
    from litellm.types.utils import ModelResponse, Usage
    from sqlite_logger import logger_instance

    usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    response_obj = ModelResponse(
        choices=[{"message": {"role": "assistant", "content": "pong"}}],
        usage=usage,
    )
    kwargs = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "def foo(): ..."}],
        "litellm_params": {"metadata": {"category": "coding"}},
        "response_cost": 0.0001,
    }
    import datetime
    now = datetime.datetime.now()
    logger_instance.log_success_event(kwargs, response_obj, now, now)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests WHERE status = 'success'").fetchone()
    assert row["category"] == "coding"


def test_raw_text_logging_disabled_by_default(db, monkeypatch):
    """Safe telemetry default (GATEWAY_LOG_RAW_TEXT unset): prompt/response
    columns hold the marker, not the conversation text, while every
    accounting field is still populated in full."""
    monkeypatch.delenv("GATEWAY_LOG_RAW_TEXT", raising=False)
    import litellm
    from sqlite_logger import RAW_TEXT_DISABLED_MARKER, logger_instance

    litellm.callbacks = [logger_instance]
    litellm.completion(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "this is a secret prompt"}],
        mock_response="this is a secret response",
        metadata={"category": "coding"},
    )

    wait_for_row(db, "success")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests WHERE status = 'success'").fetchone()

    assert row["prompt"] == RAW_TEXT_DISABLED_MARKER
    assert row["response"] == RAW_TEXT_DISABLED_MARKER
    assert "secret" not in row["prompt"]
    assert "secret" not in row["response"]
    # accounting/ledger fields are unaffected by the flag
    assert row["model"] == "gpt-3.5-turbo"
    assert row["total_tokens"] is not None
    assert row["latency_ms"] is not None
    assert row["category"] == "coding"


@pytest.mark.parametrize("flag_value", ["false", "0", "no", "off", "garbage", ""])
def test_raw_text_logging_disabled_for_falsy_and_unrecognized_values(db, monkeypatch, flag_value):
    """Anything other than a recognized truthy value is treated as
    disabled -- fail closed, not fail open, on a malformed flag."""
    monkeypatch.setenv("GATEWAY_LOG_RAW_TEXT", flag_value)
    from sqlite_logger import RAW_TEXT_DISABLED_MARKER, logger_instance
    import litellm

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
    assert row["prompt"] == RAW_TEXT_DISABLED_MARKER
    assert row["response"] == RAW_TEXT_DISABLED_MARKER


@pytest.mark.parametrize("flag_value", ["true", "1", "yes", "on", "TRUE", "True"])
def test_raw_text_logging_enabled_stores_real_content(db, monkeypatch, flag_value):
    """GATEWAY_LOG_RAW_TEXT truthy (any case/spelling variant) stores the
    real prompt/response text, not the marker."""
    monkeypatch.setenv("GATEWAY_LOG_RAW_TEXT", flag_value)
    from sqlite_logger import logger_instance
    import litellm

    litellm.callbacks = [logger_instance]
    litellm.completion(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "real prompt text"}],
        mock_response="real response text",
    )

    wait_for_row(db, "success")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests WHERE status = 'success'").fetchone()
    assert row["response"] == "real response text"
    assert json.loads(row["prompt"]) == [{"role": "user", "content": "real prompt text"}]


@pytest.mark.parametrize(
    "content",
    [
        "",  # empty text
        "Привет, мир! éè \U0001F600",  # non-ASCII incl. surrogate-pair emoji
        "x" * 200_000,  # very long text
    ],
    ids=["empty", "non-ascii", "very-long"],
)
def test_raw_text_logging_enabled_handles_boundary_inputs(db, monkeypatch, content):
    """With logging enabled, boundary-sized/encoded content round-trips
    through the DB without the logger raising."""
    monkeypatch.setenv("GATEWAY_LOG_RAW_TEXT", "true")
    from sqlite_logger import logger_instance
    import litellm

    litellm.callbacks = [logger_instance]
    litellm.completion(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": content}],
        mock_response=content or "(empty)",
    )

    wait_for_row(db, "success")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests WHERE status = 'success'").fetchone()
    assert json.loads(row["prompt"]) == [{"role": "user", "content": content}]
    assert row["response"] == (content or "(empty)")


def test_startup_banner_reports_disabled_by_default(capsys, monkeypatch):
    """Requirement 4: one honest stderr line at logger start-up naming
    the active mode. Constructing a fresh SQLiteLogger() (rather than
    relying on the module-level singleton created at import time) lets
    this test observe both regimes under monkeypatched env."""
    monkeypatch.delenv("GATEWAY_LOG_RAW_TEXT", raising=False)
    import sqlite_logger

    sqlite_logger.SQLiteLogger()
    captured = capsys.readouterr()
    assert "raw text logging" in captured.err.lower()
    assert "disabled" in captured.err.lower()
    assert "enabled" not in captured.err.lower()


def test_startup_banner_reports_enabled_when_flag_true(capsys, monkeypatch):
    monkeypatch.setenv("GATEWAY_LOG_RAW_TEXT", "true")
    import sqlite_logger

    sqlite_logger.SQLiteLogger()
    captured = capsys.readouterr()
    assert "raw text logging" in captured.err.lower()
    assert "enabled" in captured.err.lower()


def test_metadata_category_null_when_not_provided(db):
    """Without metadata.category the column stays NULL."""
    from litellm.types.utils import ModelResponse, Usage
    from sqlite_logger import logger_instance

    usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    response_obj = ModelResponse(
        choices=[{"message": {"role": "assistant", "content": "pong"}}],
        usage=usage,
    )
    kwargs = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "ping"}],
        "litellm_params": {"metadata": {}},
        "response_cost": 0.0001,
    }
    import datetime
    now = datetime.datetime.now()
    logger_instance.log_success_event(kwargs, response_obj, now, now)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests WHERE status = 'success'").fetchone()
    assert row["category"] is None
