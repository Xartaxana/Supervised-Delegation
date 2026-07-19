"""SQLite request logger for the LiteLLM gateway.

Every request passing through the gateway is recorded in a SQLite log.
The schema already contains what the Ledger (Phase 1 step 3) needs,
including raw prompt/response text for the context-repetition ratio --
but writing that raw text is now OPT-IN (safe-telemetry default,
operator decision 2026-07-16, port-queue item 13): by default the
`prompt`/`response` columns hold a short marker instead of the actual
conversation text, while every accounting/ledger field (model,
tokens, cost, ts, category, traffic_kind) is written unconditionally
-- cost/budget/category accounting never depends on raw text, only
the Ledger's context-repetition ratio and keyword categorize() do
(see metrics.py; both degrade to a meaningless signal, not a crash,
when raw text is masked -- see gateway/README.md).

Set GATEWAY_LOG_RAW_TEXT=true to store the real prompt/response text
(needed for Shadow Evaluation replay and the context-repetition
ratio). Truthy values: "1", "true", "yes", "on" (case-insensitive);
anything else, including unset, is disabled. Masking writes a marker
STRING (not NULL) into the two raw-text columns, chosen over NULL so
a masked row is visibly distinct from a legitimate NULL (e.g. a
failure row with no response, or a call with no messages) when
inspecting requests.db directly.

The database path is taken from the GATEWAY_DB_PATH environment variable,
defaulting to requests.db next to this file.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

from litellm.integrations.custom_logger import CustomLogger

# Marker written to the `prompt`/`response` columns when raw text
# logging is disabled (the default). See module docstring.
RAW_TEXT_DISABLED_MARKER = "[raw text logging disabled]"

# Truncation applied to the `error` column when raw text logging is off
# (operator decision, docs/tasks/2026-07-20_toolkit-release-v040.md,
# "Next batch queue" item 3, option "b"): provider exceptions can echo
# fragments of the prompt/response (content-policy errors especially),
# so `error` is not exempt from the masking the raw-text flag applies
# to the other two text columns. Truncating to the first ~200 chars of
# the first line keeps the error useful for diagnosis while bounding
# what can leak through it. Full error text is still recorded when raw
# text logging is on.
ERROR_TRUNCATE_LENGTH = 200
ERROR_TRUNCATE_SUFFIX = "...[truncated]"


def _truncate_error(error_text: str) -> str:
    """Bound the `error` column at raw-off: first line, first
    ERROR_TRUNCATE_LENGTH chars of it, with ERROR_TRUNCATE_SUFFIX
    appended whenever anything was actually cut -- a later line
    dropped, or the first line itself over the limit. A single-line
    error at or under the limit passes through unchanged, no suffix."""
    truncated = False
    if "\n" in error_text:
        first_line, _ = error_text.split("\n", 1)
        truncated = True
    else:
        first_line = error_text
    if len(first_line) > ERROR_TRUNCATE_LENGTH:
        first_line = first_line[:ERROR_TRUNCATE_LENGTH]
        truncated = True
    return first_line + ERROR_TRUNCATE_SUFFIX if truncated else first_line


def raw_text_logging_enabled() -> bool:
    """GATEWAY_LOG_RAW_TEXT env flag -- default disabled (false)."""
    return os.environ.get("GATEWAY_LOG_RAW_TEXT", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


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
    traffic_kind TEXT NOT NULL DEFAULT 'real',
    cache_creation_input_tokens INTEGER,
    cache_read_input_tokens INTEGER,
    category TEXT
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    # Cache-token columns (prompt-cache columns): added independently of
    # the traffic_kind migration above so a DB that already has
    # traffic_kind (post earlier migration) but predates this change still
    # gets them. Nullable: pre-migration rows and non-Anthropic traffic
    # never populate them.
    if "cache_creation_input_tokens" not in columns:
        conn.execute("ALTER TABLE requests ADD COLUMN cache_creation_input_tokens INTEGER")
    if "cache_read_input_tokens" not in columns:
        conn.execute("ALTER TABLE requests ADD COLUMN cache_read_input_tokens INTEGER")
    # category column (ground-truth category column): ground-truth category
    # from regression set or any caller that tags metadata.category; NULL
    # for untagged traffic. Added independently of prior migrations so a DB
    # that already has all earlier columns but predates this change still
    # gets it.
    if "category" not in columns:
        conn.execute("ALTER TABLE requests ADD COLUMN category TEXT")
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
    prompt_text = json.dumps(messages, ensure_ascii=False) if messages else None
    return {
        "ts": start_time.isoformat() if start_time else None,
        "model": metadata.get("model_group") or kwargs.get("model"),
        "provider_model": kwargs.get("model"),
        "latency_ms": (end_time - start_time).total_seconds() * 1000
        if start_time and end_time
        else None,
        "prompt": prompt_text if raw_text_logging_enabled() else RAW_TEXT_DISABLED_MARKER,
        "traffic_kind": metadata.get("traffic_kind") or "real",
        "category": metadata.get("category"),
    }


def _cache_tokens(usage) -> tuple:
    """Extract Anthropic prompt-cache token counts from a litellm Usage
    object.

    litellm.types.utils.Usage.__init__ maps the Anthropic response
    fields ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
    onto the Usage object THREE ways -- as direct same-named attributes
    (via its trailing ``for k, v in params.items(): setattr(self, k, v)``),
    as underscore-prefixed ``_cache_creation_input_tokens`` /
    ``_cache_read_input_tokens``, and inside
    ``usage.prompt_tokens_details.cache_creation_tokens`` /
    ``.cached_tokens``. The direct attribute is used first since it
    matches the field names this column pair is named after; the
    prompt_tokens_details path is the fallback for any usage object
    that only got constructed with that field populated. Non-Anthropic
    responses (mock, Groq, Gemini, Ollama) carry neither -- getattr's
    None default keeps this a no-op for them, not a crash.
    """
    if usage is None:
        return None, None
    details = getattr(usage, "prompt_tokens_details", None)
    creation = getattr(usage, "cache_creation_input_tokens", None)
    if creation is None:
        creation = getattr(details, "cache_creation_tokens", None)
    read = getattr(usage, "cache_read_input_tokens", None)
    if read is None:
        read = getattr(details, "cached_tokens", None)
    return creation, read


def _success_row(kwargs, response_obj, start_time, end_time) -> dict:
    row = _base_row(kwargs, start_time, end_time)
    usage = getattr(response_obj, "usage", None)
    choices = getattr(response_obj, "choices", None)
    cache_creation, cache_read = _cache_tokens(usage)
    response_text = choices[0].message.content if choices else None
    row.update(
        {
            "status": "success",
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cost_usd": kwargs.get("response_cost"),
            "response": response_text if raw_text_logging_enabled() else RAW_TEXT_DISABLED_MARKER,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        }
    )
    return row


def _failure_row(kwargs, start_time, end_time) -> dict:
    row = _base_row(kwargs, start_time, end_time)
    error_text = str(kwargs.get("exception") or "")
    row.update(
        {
            "status": "failure",
            "error": error_text if raw_text_logging_enabled() else _truncate_error(error_text),
        }
    )
    return row


class SQLiteLogger(CustomLogger):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Self-declaration of the raw-text-logging posture (safe-telemetry
        # requirement 4): one honest line to stderr at logger start-up, so
        # an operator staring at the proxy's boot output sees which mode is
        # live without having to go read this file or requests.db. Printed
        # per-instance (not once at import) so it always reflects the
        # GATEWAY_LOG_RAW_TEXT value in effect when THIS logger starts --
        # relevant for tests, which construct fresh instances under
        # monkeypatched env rather than reloading the module.
        state = "ENABLED" if raw_text_logging_enabled() else "disabled"
        print(f"raw text logging: {state}", file=sys.stderr)

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        _insert(_success_row(kwargs, response_obj, start_time, end_time))

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _insert(_failure_row(kwargs, start_time, end_time))

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        _insert(_success_row(kwargs, response_obj, start_time, end_time))

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _insert(_failure_row(kwargs, start_time, end_time))


logger_instance = SQLiteLogger()
