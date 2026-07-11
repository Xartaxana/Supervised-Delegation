"""SQLite request logger for the LiteLLM gateway.

Every request passing through the gateway is recorded in a SQLite log.
The schema already contains what the Ledger (Phase 1 step 3) needs,
including raw prompt text for the context-repetition ratio.

The database path is taken from the GATEWAY_DB_PATH environment variable,
defaulting to requests.db next to this file.
"""

import json
import os
import sqlite3
from pathlib import Path

from litellm.integrations.custom_logger import CustomLogger

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
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

# traffic_kind convention (D-0033, CURRENT_CONTEXT.md "Delegated Task 2"):
# the caller tags its own traffic via a "metadata": {"traffic_kind": ...}
# field in the JSON body sent to this gateway. From a remote client this
# means extra_body={"metadata": {...}} (see shadow_eval.py replay() /
# judge_pair()) -- litellm.completion's own metadata= kwarg is a
# client-side-only no-op against a remote api_base, it never reaches the
# wire. Values: 'real' (default, anything untagged), 'synthetic'
# (working-set generation), 'replay' (shadow_eval.py target calls),
# 'judge' (shadow_eval.py judge calls). Gate G1 counts only 'real'.


def db_path() -> Path:
    return Path(os.environ.get("GATEWAY_DB_PATH", Path(__file__).parent / "requests.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.execute(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    if "traffic_kind" not in columns:
        conn.execute(
            "ALTER TABLE requests ADD COLUMN traffic_kind TEXT NOT NULL DEFAULT 'synthetic'"
        )
        # Pre-migration rows predate any real gateway traffic (today's log
        # is working sets, replays and judge calls); the judge LIKE filter
        # is the same one sample_requests() uses for contamination.
        # DEFAULT 'synthetic' here deliberately diverges from SCHEMA's
        # 'real': on a migrated DB it is the fail-closed direction for
        # gate G1, and the logger always writes the field explicitly.
        # Accepted as finding 4 of the Tasks 1-2 review
        # (docs/task_reports/task-1-2_cost-accounting-and-traffic-kind.md).
        conn.execute(
            "UPDATE requests SET traffic_kind = 'judge'"
            " WHERE prompt LIKE '%impartial judge comparing two answers%'"
        )
    return conn


def _insert(row: dict) -> None:
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO requests ({columns}) VALUES ({placeholders})",
            list(row.values()),
        )


def _base_row(kwargs, start_time, end_time) -> dict:
    messages = kwargs.get("messages")
    litellm_params = kwargs.get("litellm_params") or {}
    metadata = litellm_params.get("metadata") or {}
    # Through the proxy, kwargs["model"] is the resolved provider model;
    # the gateway alias the client asked for is metadata["model_group"].
    return {
        "ts": start_time.isoformat() if start_time else None,
        "model": metadata.get("model_group") or kwargs.get("model"),
        "provider_model": kwargs.get("model"),
        "latency_ms": (end_time - start_time).total_seconds() * 1000
        if start_time and end_time
        else None,
        "prompt": json.dumps(messages, ensure_ascii=False) if messages else None,
        "traffic_kind": metadata.get("traffic_kind") or "real",
    }


def _success_row(kwargs, response_obj, start_time, end_time) -> dict:
    row = _base_row(kwargs, start_time, end_time)
    usage = getattr(response_obj, "usage", None)
    choices = getattr(response_obj, "choices", None)
    row.update(
        {
            "status": "success",
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cost_usd": kwargs.get("response_cost"),
            "response": choices[0].message.content if choices else None,
        }
    )
    return row


def _failure_row(kwargs, start_time, end_time) -> dict:
    row = _base_row(kwargs, start_time, end_time)
    row.update(
        {
            "status": "failure",
            "error": str(kwargs.get("exception") or ""),
        }
    )
    return row


class SQLiteLogger(CustomLogger):
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        _insert(_success_row(kwargs, response_obj, start_time, end_time))

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _insert(_failure_row(kwargs, start_time, end_time))

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        _insert(_success_row(kwargs, response_obj, start_time, end_time))

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _insert(_failure_row(kwargs, start_time, end_time))


logger_instance = SQLiteLogger()
