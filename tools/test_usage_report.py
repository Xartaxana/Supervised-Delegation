"""Tests for tools/usage_report.py. No network, no LLM calls -- pure
parsing/SQL over a small sanitized fixture transcript
(tools/fixtures/sample_transcript.jsonl, synthetic usage numbers only,
no real prompt content).

Run from tools/: python -m pytest test_usage_report.py
"""

import json
import sqlite3
from pathlib import Path

import pytest

from usage_report import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    PRICES_PER_TOKEN_USD,
    SCHEMA,
    accounted_cost,
    backfill_costs,
    build_report,
    import_transcripts,
    iter_assistant_turns,
    transcript_glob,
)

FIXTURE = str(Path(__file__).parent / "fixtures" / "sample_transcript.jsonl")


def _write_jsonl(path: Path, lines: list):
    """Test helper: write a list of dicts as a JSONL transcript file,
    creating parent directories as needed (used to build the nested
    <project>/<session>/subagents/agent-*.jsonl layout in tmp dirs,
    since the projects root cannot be hardcoded -- Delegated Task 6)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")


def _assistant_line(session_id=None, request_id="req-x", is_sidechain=False,
                     model="claude-sonnet-5", uuid="uuid-x", extra=None):
    obj = {
        "type": "assistant",
        "uuid": uuid,
        "requestId": request_id,
        "isSidechain": is_sidechain,
        "timestamp": "2026-07-07T12:00:00.000Z",
        "parentUuid": None,
        "message": {
            "id": f"msg_{uuid}",
            "model": model,
            "role": "assistant",
            "usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
            "content": [{"type": "text", "text": "synthetic fixture text"}],
        },
    }
    if session_id is not None:
        obj["sessionId"] = session_id
    if extra:
        obj.update(extra)
    return obj


@pytest.fixture()
def db_file(tmp_path):
    return tmp_path / "requests.db"


# ---- parsing ----

def test_parses_only_assistant_lines_and_skips_synthetic():
    turns = list(iter_assistant_turns(FIXTURE))
    models = [t["model"] for t in turns]
    assert "<synthetic>" not in models
    # 8 lines in the fixture: 1 user line and 1 <synthetic> line must be
    # excluded, leaving 6 assistant turns (including the 2 duplicate-
    # requestId lines, which iter_assistant_turns does NOT dedupe --
    # that's import_transcripts's job via the UNIQUE constraint).
    assert len(turns) == 6


def test_skips_non_assistant_line_types():
    turns = list(iter_assistant_turns(FIXTURE))
    for t in turns:
        assert t["model"] != "<synthetic>"
    # the 'user' line in the fixture carries no usage field and no
    # model -- confirm nothing resembling it leaked through.
    assert all(t["model"] for t in turns)


def test_session_id_prefers_json_field_over_filename():
    turns = list(iter_assistant_turns(FIXTURE))
    sessions = {t["session_id"] for t in turns}
    assert sessions == {"session-aaa", "session-bbb"}


def test_dedupe_key_shared_by_split_turn():
    turns = list(iter_assistant_turns(FIXTURE))
    keyed = {t["dedupe_key"]: t for t in turns}
    # req-0002 appears on two JSONL lines (uuid-0002, uuid-0003) with
    # identical usage -- both must produce the SAME dedupe_key so the
    # importer's UNIQUE constraint collapses them to one row.
    dupe_keys = [t["dedupe_key"] for t in turns if t["dedupe_key"].endswith(":req-0002")]
    assert len(dupe_keys) == 2
    assert dupe_keys[0] == dupe_keys[1]


# ---- idempotent import / dedup ----

def test_import_is_idempotent(db_file):
    rows1, sessions1, warnings1 = import_transcripts(FIXTURE, db_file)
    rows2, sessions2, warnings2 = import_transcripts(FIXTURE, db_file)

    conn = sqlite3.connect(db_file)
    count = conn.execute("SELECT COUNT(*) FROM cc_usage").fetchone()[0]

    # 6 assistant turns in the fixture, but req-0002's split lines
    # collapse to 1 row -> 5 distinct API turns.
    assert count == 5
    assert rows1 == 5
    # Second run finds nothing new to insert (INSERT OR IGNORE).
    assert rows2 == 0


def test_import_does_not_touch_requests_table(db_file):
    # Pre-seed a `requests` table row (as the gateway would) and verify
    # the importer never touches it -- cc_usage is a new table, spec
    # explicitly forbids touching `requests`.
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE requests (id INTEGER PRIMARY KEY, model TEXT)"
    )
    conn.execute("INSERT INTO requests (model) VALUES ('sentinel')")
    conn.commit()
    conn.close()

    import_transcripts(FIXTURE, db_file)

    conn = sqlite3.connect(db_file)
    row = conn.execute("SELECT model FROM requests").fetchone()
    assert row == ("sentinel",)


def test_cc_usage_schema_has_required_columns(db_file):
    import_transcripts(FIXTURE, db_file)
    conn = sqlite3.connect(db_file)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cc_usage)")}
    for expected in (
        "ts", "project", "session_id", "turn_index", "model",
        "input_tokens", "output_tokens", "cache_creation_tokens",
        "cache_read_tokens", "accounted_cost_usd", "traffic_kind",
        "is_sidechain",
    ):
        assert expected in columns


def test_is_sidechain_flag_recorded(db_file):
    import_transcripts(FIXTURE, db_file)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM cc_usage WHERE model = 'claude-opus-4-8'"
    ).fetchone()
    assert row["is_sidechain"] == 1
    assert row["traffic_kind"] == "real"


# ---- price math including cache rates ----

def test_accounted_cost_known_model():
    cost, warning = accounted_cost(
        "claude-sonnet-5",
        input_tokens=1000, output_tokens=200,
        cache_creation_tokens=500, cache_read_tokens=4000,
    )
    assert warning is None
    input_price, output_price = PRICES_PER_TOKEN_USD["claude-sonnet-5"]
    expected = (
        1000 * input_price
        + 200 * output_price
        + 500 * input_price * CACHE_WRITE_MULTIPLIER
        + 4000 * input_price * CACHE_READ_MULTIPLIER
    )
    assert cost == pytest.approx(expected)


def test_accounted_cost_cache_rates_are_distinct_from_base_input():
    # cache write and cache read must NOT be priced the same as a bare
    # input token, or D-0032's "cache write/read price distinction"
    # requirement is violated.
    base_cost, _ = accounted_cost("claude-sonnet-5", 1000, 0, 0, 0)
    write_cost, _ = accounted_cost("claude-sonnet-5", 0, 0, 1000, 0)
    read_cost, _ = accounted_cost("claude-sonnet-5", 0, 0, 0, 1000)
    assert write_cost > base_cost  # 1.25x premium
    assert read_cost < base_cost  # 0.1x discount
    assert write_cost != read_cost


# ---- unknown-model warning path ----

def test_unknown_model_cost_is_none_with_warning():
    cost, warning = accounted_cost("claude-unknown-model-x", 500, 100, 0, 0)
    assert cost is None
    assert warning is not None
    assert "claude-unknown-model-x" in warning
    assert "WARNING" in warning


def test_unknown_model_never_silently_zero(db_file):
    import_transcripts(FIXTURE, db_file)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM cc_usage WHERE model = 'claude-unknown-model-x'"
    ).fetchone()
    assert row is not None
    assert row["accounted_cost_usd"] is None  # None, never 0.0


def test_report_surfaces_unknown_model_warning(db_file):
    _, _, warnings = import_transcripts(FIXTURE, db_file)
    assert any("claude-unknown-model-x" in w for w in warnings)


# ---- report building ----

def test_build_report_totals_exclude_unknown_cost_from_sum(db_file):
    import_transcripts(FIXTURE, db_file)
    report = build_report(db_file, days=None)
    assert report["totals"]["rows"] == 5
    assert report["totals"]["unknown_cost_rows"] == 1
    # accounted_cost_usd sum should be a real float, not NaN/None, and
    # should not silently include the unknown-model row as $0 hidden
    # inside a total that looks complete.
    assert report["totals"]["accounted_cost_usd"] > 0


def test_build_report_per_project_and_per_session(db_file):
    import_transcripts(FIXTURE, db_file)
    report = build_report(db_file, days=None)
    # both fixture rows share one project dir (the fixture file's own
    # parent directory name), but two distinct session_ids.
    assert len(report["per_project"]) == 1
    session_keys = {s["session_id"] for s in report["top_sessions_by_cost"]}
    assert "session-aaa" in session_keys
    assert "session-bbb" in session_keys


def test_build_report_cache_read_share_of_input(db_file):
    import_transcripts(FIXTURE, db_file)
    report = build_report(db_file, days=None)
    share = report["cache_read_share_of_input"]
    assert share is not None
    assert 0 <= share <= 1


def test_build_report_sidechain_share(db_file):
    import_transcripts(FIXTURE, db_file)
    report = build_report(db_file, days=None)
    assert report["sidechain_tokens"] > 0
    assert report["sidechain_share_of_tokens"] is not None
    assert 0 < report["sidechain_share_of_tokens"] < 1


def test_build_report_days_filter_excludes_old_rows(db_file):
    # The fixture's timestamps are 2026-07-01; a days=1 window relative
    # to "now" (run date is long after the fixture dates) should
    # exclude everything.
    import_transcripts(FIXTURE, db_file)
    report = build_report(db_file, days=1)
    assert report["totals"]["rows"] == 0


# ---- subagent/sidechain transcripts (Delegated Task 6) ----
#
# Real subagent transcripts live at
# <project>/<session-id>/subagents/agent-*.jsonl, one directory layer
# deeper than the top-level <project>/<session>.jsonl layout. The
# projects root cannot be hardcoded here (real path is
# ~/.claude/projects, but tests must not depend on the developer
# machine's home directory), so each test below builds its own tmp_path
# tree with both layouts and points transcript_glob()/import_transcripts
# at it via the base_dir override.

def test_project_attribution_top_level_layout(tmp_path):
    path = tmp_path / "myproj" / "session-1.jsonl"
    _write_jsonl(path, [_assistant_line(session_id="session-1")])
    turns = list(iter_assistant_turns(str(path)))
    assert len(turns) == 1
    assert turns[0]["project"] == "myproj"
    assert turns[0]["session_id"] == "session-1"


def test_project_attribution_subagent_layout(tmp_path):
    path = tmp_path / "myproj" / "session-1" / "subagents" / "agent-x.jsonl"
    _write_jsonl(path, [_assistant_line(session_id="session-1", is_sidechain=True)])
    turns = list(iter_assistant_turns(str(path)))
    assert len(turns) == 1
    # Must be "myproj" (the real project), NOT "subagents" (the file's
    # immediate parent dir name) and not "session-1" either.
    assert turns[0]["project"] == "myproj"
    assert turns[0]["session_id"] == "session-1"
    assert turns[0]["is_sidechain"] == 1


def test_subagent_layout_session_id_fallback_uses_directory_not_agent_filename(tmp_path):
    # No "sessionId" JSON field at all -- the fallback must derive the
    # session id from the <session-id> directory name one level above
    # subagents/, NOT from the file's own stem (which is the sub-agent
    # id, e.g. "agent-y", and would be wrong as a session id).
    path = tmp_path / "myproj2" / "sess-xyz" / "subagents" / "agent-y.jsonl"
    _write_jsonl(path, [_assistant_line(session_id=None, is_sidechain=True)])
    turns = list(iter_assistant_turns(str(path)))
    assert len(turns) == 1
    assert turns[0]["session_id"] == "sess-xyz"
    assert turns[0]["project"] == "myproj2"


def test_transcript_glob_returns_two_patterns(tmp_path):
    patterns = transcript_glob(base_dir=tmp_path)
    assert isinstance(patterns, list)
    assert len(patterns) == 2
    assert str(tmp_path) in patterns[0]
    assert patterns[0].endswith("*.jsonl")
    assert "subagents" in patterns[1]
    assert patterns[1].endswith("*.jsonl")


def test_import_transcripts_scans_both_layouts_together(tmp_path, db_file):
    top_level = tmp_path / "projA" / "session-1.jsonl"
    _write_jsonl(top_level, [
        _assistant_line(session_id="session-1", request_id="req-parent-1"),
    ])
    sub = tmp_path / "projA" / "session-1" / "subagents" / "agent-a1.jsonl"
    _write_jsonl(sub, [
        _assistant_line(session_id="session-1", request_id="req-sub-1", is_sidechain=True),
    ])

    patterns = transcript_glob(base_dir=tmp_path)
    rows_imported, sessions_seen, warnings = import_transcripts(patterns, db_file)

    assert rows_imported == 2
    assert sessions_seen == {"session-1"}

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM cc_usage ORDER BY dedupe_key").fetchall()
    assert len(rows) == 2
    by_sidechain = {r["is_sidechain"] for r in rows}
    assert by_sidechain == {0, 1}
    for r in rows:
        # Both rows must attribute to the real project "projA", never
        # "subagents" or the session id.
        assert r["project"] == "projA"
        assert r["session_id"] == "session-1"
    # No dedupe_key collision between the parent-session row and the
    # subagent row sharing the same session_id.
    dedupe_keys = {r["dedupe_key"] for r in rows}
    assert len(dedupe_keys) == 2


def test_import_transcripts_subagent_layout_is_idempotent(tmp_path, db_file):
    sub = tmp_path / "projB" / "session-2" / "subagents" / "agent-b1.jsonl"
    _write_jsonl(sub, [
        _assistant_line(session_id="session-2", request_id="req-sub-2", is_sidechain=True),
    ])
    patterns = transcript_glob(base_dir=tmp_path)

    rows1, _, _ = import_transcripts(patterns, db_file)
    rows2, _, _ = import_transcripts(patterns, db_file)

    assert rows1 == 1
    assert rows2 == 0  # second run finds nothing new

    conn = sqlite3.connect(db_file)
    count = conn.execute("SELECT COUNT(*) FROM cc_usage").fetchone()[0]
    assert count == 1



# ---- agent attribution + haiku pricing (Delegated Task 7) ----

def test_migration_adds_columns_to_old_schema_db_without_data_loss(db_file):
    # Simulate a pre-Task-7 database: the OLD schema (no agent_id/
    # agent_type columns), with a real row already in it.
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE cc_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            project TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cache_creation_tokens INTEGER NOT NULL,
            cache_read_tokens INTEGER NOT NULL,
            accounted_cost_usd REAL,
            traffic_kind TEXT NOT NULL DEFAULT 'real',
            is_sidechain INTEGER NOT NULL DEFAULT 0,
            dedupe_key TEXT NOT NULL UNIQUE
        )
        """
    )
    conn.execute(
        """
        INSERT INTO cc_usage
            (ts, project, session_id, turn_index, model, input_tokens,
             output_tokens, cache_creation_tokens, cache_read_tokens,
             accounted_cost_usd, traffic_kind, is_sidechain, dedupe_key)
        VALUES ('2026-07-01T00:00:00Z', 'oldproj', 'old-session', 0,
                'claude-sonnet-5', 100, 50, 0, 0, 0.001, 'real', 0, 'old-session:req-old')
        """
    )
    conn.commit()
    conn.close()

    # Any import (even of an empty/unrelated transcript set) goes
    # through _connect(), which must migrate the existing table
    # in-place rather than erroring or dropping data.
    import_transcripts(FIXTURE, db_file)

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cc_usage)")}
    assert "agent_id" in columns
    assert "agent_type" in columns

    old_row = conn.execute(
        "SELECT * FROM cc_usage WHERE dedupe_key = 'old-session:req-old'"
    ).fetchone()
    assert old_row is not None
    assert old_row["model"] == "claude-sonnet-5"
    assert old_row["accounted_cost_usd"] == pytest.approx(0.001)
    assert old_row["agent_id"] is None
    assert old_row["agent_type"] is None


def test_agent_id_and_type_stored_for_subagent_layout_line(tmp_path, db_file):
    path = tmp_path / "myproj" / "session-1" / "subagents" / "agent-x.jsonl"
    _write_jsonl(path, [
        _assistant_line(
            session_id="session-1", is_sidechain=True,
            extra={"agentId": "agent-x-id", "attributionAgent": "test-maintainer"},
        ),
    ])
    turns = list(iter_assistant_turns(str(path)))
    assert len(turns) == 1
    assert turns[0]["agent_id"] == "agent-x-id"
    assert turns[0]["agent_type"] == "test-maintainer"

    import_transcripts(str(tmp_path / "*" / "*" / "subagents" / "*.jsonl"), db_file)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM cc_usage WHERE session_id = 'session-1'"
    ).fetchone()
    assert row["agent_id"] == "agent-x-id"
    assert row["agent_type"] == "test-maintainer"


def test_top_level_line_gets_null_agent_id():
    # The fixture's top-level lines carry neither agentId nor
    # attributionAgent (real top-level transcripts never do either,
    # per the module docstring).
    turns = list(iter_assistant_turns(FIXTURE))
    assert all(t["agent_id"] is None for t in turns)
    assert all(t["agent_type"] is None for t in turns)


def test_haiku_cost_computed_no_warning():
    cost, warning = accounted_cost(
        "claude-haiku-4-5-20251001",
        input_tokens=1_000_000, output_tokens=1_000_000,
        cache_creation_tokens=0, cache_read_tokens=0,
    )
    assert warning is None
    assert cost == pytest.approx(1.00 + 5.00)


def test_haiku_bare_id_also_priced():
    cost, warning = accounted_cost("claude-haiku-4-5", 1_000_000, 0, 0, 0)
    assert warning is None
    assert cost == pytest.approx(1.00)


def test_backfill_fills_agent_fields_and_null_costs_idempotently(tmp_path, db_file):
    # Simulate a row imported BEFORE Task 7: no agent_id/agent_type,
    # and a NULL cost because its model (haiku) wasn't priced yet at
    # import time.
    sub = tmp_path / "projB" / "session-9" / "subagents" / "agent-b9.jsonl"
    _write_jsonl(sub, [
        _assistant_line(
            session_id="session-9", request_id="req-b9", is_sidechain=True,
            model="claude-haiku-4-5-20251001",
            extra={"agentId": "agent-b9-id", "attributionAgent": "builder"},
        ),
    ])

    conn = sqlite3.connect(db_file)
    conn.execute(SCHEMA)
    conn.execute(
        """
        INSERT INTO cc_usage
            (ts, project, session_id, turn_index, model, input_tokens,
             output_tokens, cache_creation_tokens, cache_read_tokens,
             accounted_cost_usd, traffic_kind, is_sidechain, agent_id,
             agent_type, dedupe_key)
        VALUES ('2026-07-07T12:00:00.000Z', 'projB', 'session-9', 0,
                'claude-haiku-4-5-20251001', 10, 5, 0, 0, NULL, 'real', 1,
                NULL, NULL, 'session-9:req-b9')
        """
    )
    conn.commit()
    conn.close()

    patterns = str(tmp_path / "*" / "*" / "subagents" / "*.jsonl")
    rows1, _, _ = import_transcripts(patterns, db_file)
    assert rows1 == 0  # the row already existed; nothing NEW inserted

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM cc_usage WHERE dedupe_key = 'session-9:req-b9'").fetchone()
    assert row["agent_id"] == "agent-b9-id"
    assert row["agent_type"] == "builder"
    assert row["accounted_cost_usd"] is not None
    assert row["accounted_cost_usd"] > 0
    conn.close()

    # Idempotent: a second run updates 0 rows (the columns are already filled).
    rows2, _, _ = import_transcripts(patterns, db_file)
    assert rows2 == 0
    conn = sqlite3.connect(db_file)
    updated_again = backfill_costs(conn)
    conn.commit()
    conn.close()
    assert updated_again == 0


# ---- deep subagent transcripts under workflows/ (regression) ----
#
# A workflow tool can write agent transcripts at a THIRD depth, still
# under .../subagents/ but nested further:
# <project>/<session>/subagents/workflows/wf_*/agent-*.jsonl. The
# Task 6 glob/detection (one directory deep only) silently missed
# these. Regression coverage below builds a tmp tree with all three
# layouts together (top-level session, flat subagent, deep workflow
# subagent) and confirms every one is counted.

def test_project_attribution_deep_workflow_subagent_layout(tmp_path):
    path = (
        tmp_path / "myproj" / "session-1" / "subagents" / "workflows"
        / "wf_abc123" / "agent-deep1.jsonl"
    )
    _write_jsonl(path, [_assistant_line(session_id="session-1", is_sidechain=True)])
    turns = list(iter_assistant_turns(str(path)))
    assert len(turns) == 1
    # Must attribute to the real project/session two levels above the
    # "subagents" ancestor, NOT "wf_abc123", "workflows", or
    # "subagents" itself.
    assert turns[0]["project"] == "myproj"
    assert turns[0]["session_id"] == "session-1"
    assert turns[0]["is_sidechain"] == 1


def test_deep_workflow_subagent_session_id_fallback_uses_directory_not_agent_filename(tmp_path):
    # No "sessionId" JSON field -- fallback must still resolve to the
    # session directory one level above "subagents/", regardless of
    # how many extra directories (workflows/wf_x/...) sit in between.
    path = (
        tmp_path / "myproj2" / "sess-xyz" / "subagents" / "workflows"
        / "wf_def456" / "agent-deep2.jsonl"
    )
    _write_jsonl(path, [_assistant_line(session_id=None, is_sidechain=True)])
    turns = list(iter_assistant_turns(str(path)))
    assert len(turns) == 1
    assert turns[0]["session_id"] == "sess-xyz"
    assert turns[0]["project"] == "myproj2"


def test_transcript_glob_recursive_pattern_covers_deep_workflow_files(tmp_path, db_file):
    # PIN: on the pre-fix code (glob pattern "subagents/*.jsonl", one
    # directory deep only), this test fails -- the deep workflow file
    # is invisible to both the glob and iter_assistant_turns's
    # "immediate parent == subagents" check. All three real-world
    # layouts, together:
    top_level = tmp_path / "projA" / "session-1.jsonl"
    _write_jsonl(top_level, [
        _assistant_line(session_id="session-1", request_id="req-top-1"),
    ])
    flat_sub = tmp_path / "projA" / "session-1" / "subagents" / "agent-flat1.jsonl"
    _write_jsonl(flat_sub, [
        _assistant_line(session_id="session-1", request_id="req-flat-1", is_sidechain=True),
    ])
    deep_sub = (
        tmp_path / "projA" / "session-1" / "subagents" / "workflows"
        / "wf_xyz789" / "agent-deep1.jsonl"
    )
    _write_jsonl(deep_sub, [
        _assistant_line(session_id="session-1", request_id="req-deep-1", is_sidechain=True),
    ])
    # A workflow journal.jsonl sitting right next to the deep agent
    # file, real-shape lines (type: "started"/"result", no
    # message.usage) -- must stay inert, matched by the wider glob but
    # contributing zero rows, not an error.
    journal = deep_sub.parent / "journal.jsonl"
    _write_jsonl(journal, [
        {"type": "started", "key": "v2:abc", "agentId": "agent-deep1"},
        {"type": "result", "key": "v2:abc", "agentId": "agent-deep1", "result": {"summary": "ok"}},
    ])

    patterns = transcript_glob(base_dir=tmp_path)
    rows_imported, sessions_seen, warnings = import_transcripts(patterns, db_file)

    # 3 real assistant turns (top-level + flat subagent + deep
    # workflow subagent); the journal.jsonl contributes 0.
    assert rows_imported == 3
    assert sessions_seen == {"session-1"}

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM cc_usage ORDER BY dedupe_key").fetchall()
    assert len(rows) == 3
    for r in rows:
        assert r["project"] == "projA"
        assert r["session_id"] == "session-1"
    request_ids_seen = {r["dedupe_key"].split(":", 1)[1] for r in rows}
    assert request_ids_seen == {"req-top-1", "req-flat-1", "req-deep-1"}


def test_import_transcripts_deep_workflow_layout_is_idempotent(tmp_path, db_file):
    deep_sub = (
        tmp_path / "projD" / "session-4" / "subagents" / "workflows"
        / "wf_qqq" / "agent-deep-idem.jsonl"
    )
    _write_jsonl(deep_sub, [
        _assistant_line(session_id="session-4", request_id="req-deep-idem", is_sidechain=True),
    ])
    patterns = transcript_glob(base_dir=tmp_path)

    rows1, _, _ = import_transcripts(patterns, db_file)
    rows2, _, _ = import_transcripts(patterns, db_file)

    assert rows1 == 1
    assert rows2 == 0

    conn = sqlite3.connect(db_file)
    count = conn.execute("SELECT COUNT(*) FROM cc_usage").fetchone()[0]
    assert count == 1


# ---- max-output-tokens dedup (regression) ----
#
# A split subagent turn's lines share input/cache tokens but carry a
# placeholder output_tokens (2-7) on every line except the one that
# carries the real value. Deduping by FIRST occurrence (INSERT OR
# IGNORE) keeps the placeholder. Tie-breaking on LAST occurrence
# instead is also wrong -- it depends on `sorted(paths)`/line order,
# which is wrong for the cross-file collision case covered further
# below (test_max_wins_for_cross_file_dedupe_key_collision) --
# occurrence order there is an artifact of glob/filename sort,
# unrelated to which file holds the real value. The fix ties-break on
# VALUE instead: output_tokens/accounted_cost_usd is only overwritten
# when the new line's output_tokens is STRICTLY GREATER than what is
# stored (MAX-wins), which is order-independent by construction. These
# tests build the split-turn shape directly (not via `_assistant_line`,
# which hardcodes output_tokens=5 for every line) and PIN the fix.

def _split_turn_lines(session_id, request_id, outputs, model="claude-sonnet-5",
                       is_sidechain=True, agent_id="agent-e1-id",
                       attribution_agent="builder"):
    lines = []
    for i, out in enumerate(outputs):
        obj = {
            "type": "assistant",
            "uuid": f"uuid-{request_id}-{i}",
            "requestId": request_id,
            "sessionId": session_id,
            "isSidechain": is_sidechain,
            "timestamp": "2026-07-14T00:00:00.000Z",
            "parentUuid": None,
            "message": {
                "id": f"msg_{request_id}_{i}",
                "model": model,
                "role": "assistant",
                "usage": {
                    "input_tokens": 100, "output_tokens": out,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                },
                "content": [{"type": "text", "text": "synthetic fixture text"}],
            },
        }
        if is_sidechain:
            obj["agentId"] = agent_id
            obj["attributionAgent"] = attribution_agent
        lines.append(obj)
    return lines


def test_max_wins_for_split_turn_output_tokens(tmp_path, db_file):
    # PIN: on the pre-fix code (INSERT OR IGNORE, first line wins),
    # this test fails -- the stored output_tokens is 5 (the first
    # line's placeholder), not 383 (the real value, only on the last
    # line). It also passes under a last-wins fix, since here the real
    # value happens to be last -- see
    # test_max_wins_for_cross_file_dedupe_key_collision below for the
    # case that distinguishes max-wins from last-wins.
    sub = tmp_path / "projE" / "session-5" / "subagents" / "agent-e1.jsonl"
    _write_jsonl(sub, _split_turn_lines("session-5", "req-split-1", [5, 5, 5, 383]))

    rows_imported, _, _ = import_transcripts(str(sub), db_file)
    assert rows_imported == 1  # one distinct API turn (one dedupe_key)

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM cc_usage WHERE dedupe_key = 'session-5:req-split-1'"
    ).fetchone()
    assert row is not None
    assert row["output_tokens"] == 383
    assert row["input_tokens"] == 100  # unaffected -- identical across all 4 lines
    input_price, output_price = PRICES_PER_TOKEN_USD["claude-sonnet-5"]
    assert row["accounted_cost_usd"] == pytest.approx(100 * input_price + 383 * output_price)


def test_max_wins_survives_reimport_and_stays_idempotent(tmp_path, db_file):
    # Re-running the importer over the SAME split-turn file must keep
    # the corrected value (not regress it back to a placeholder
    # mid-way through the re-scan) and must not inflate rows_imported
    # once the row is already correct. Under max-wins this also covers
    # the case where the re-scan re-presents an EQUAL (not smaller)
    # value -- output_tokens must stay at 383, not merely "not regress
    # to something smaller".
    sub = tmp_path / "projF" / "session-6" / "subagents" / "agent-f1.jsonl"
    _write_jsonl(sub, _split_turn_lines("session-6", "req-split-2", [3, 383]))

    rows1, _, _ = import_transcripts(str(sub), db_file)
    rows2, _, _ = import_transcripts(str(sub), db_file)

    assert rows1 == 1
    assert rows2 == 0  # nothing NEW on the second run

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM cc_usage WHERE dedupe_key = 'session-6:req-split-2'"
    ).fetchone()
    assert row["output_tokens"] == 383
    count = conn.execute("SELECT COUNT(*) FROM cc_usage").fetchone()[0]
    assert count == 1  # still exactly one row, no duplicate


def test_max_wins_does_not_break_main_chain_identical_duplicates(tmp_path, db_file):
    # Main-chain duplicate lines are byte-identical (first == last) --
    # confirm the max-wins fix is a genuine no-op in value for that
    # already-correct case (equal values are never "strictly greater",
    # so the conditional UPDATE never fires).
    top_level = tmp_path / "projG" / "session-7.jsonl"
    _write_jsonl(top_level, _split_turn_lines(
        "session-7", "req-main-dup", [50, 50], is_sidechain=False,
    ))

    rows_imported, _, _ = import_transcripts(str(top_level), db_file)
    assert rows_imported == 1

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM cc_usage WHERE dedupe_key = 'session-7:req-main-dup'"
    ).fetchone()
    assert row["output_tokens"] == 50


def test_max_wins_for_cross_file_dedupe_key_collision(tmp_path, db_file):
    # PIN: the same dedupe_key can appear in TWO DIFFERENT sibling
    # subagent files under one session's subagents/ directory, not
    # just on two lines of one file. This test builds a neutral
    # fixture reproducing that shape: a file that sorts FIRST carries
    # the real output_tokens; a file that sorts LATER carries only a
    # placeholder line for the SAME requestId.
    #
    # On a last-occurrence-wins fix, this test FAILS:
    # import_transcripts() processes files in sorted(paths) order, so
    # the later-sorting placeholder file overwrites the earlier-
    # sorting real value, leaving output_tokens == 3, not 3772.
    # Max-wins (the actual fix) is order-independent and keeps 3772
    # regardless of which file the glob visits first.
    session_id = "session-collision-1"
    request_id = "req-collision-1"
    real_file = tmp_path / "projH" / session_id / "subagents" / "agent-a-real.jsonl"
    placeholder_file = tmp_path / "projH" / session_id / "subagents" / "agent-b-placeholder.jsonl"
    assert str(real_file) < str(placeholder_file)  # sanity: matches the intended sort order

    _write_jsonl(real_file, _split_turn_lines(
        session_id, request_id, [3772], agent_id="agent-a-real", attribution_agent="worker-a",
    ))
    _write_jsonl(placeholder_file, _split_turn_lines(
        session_id, request_id, [3], agent_id="agent-b-placeholder", attribution_agent="worker-b",
    ))

    patterns = str(tmp_path / "*" / "*" / "subagents" / "*.jsonl")
    rows_imported, _, _ = import_transcripts(patterns, db_file)
    assert rows_imported == 1  # one distinct dedupe_key across the two files

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM cc_usage WHERE dedupe_key = ?",
        (f"{session_id}:{request_id}",),
    ).fetchone()
    assert row is not None
    assert row["output_tokens"] == 3772
    input_price, output_price = PRICES_PER_TOKEN_USD["claude-sonnet-5"]
    assert row["accounted_cost_usd"] == pytest.approx(100 * input_price + 3772 * output_price)


def test_import_transcripts_accepts_single_string_pattern_backward_compat(tmp_path, db_file):
    # The pre-Task-6 API (single glob string) must keep working, since
    # both the CLI --transcripts-glob override and any external caller
    # may still pass a plain string.
    sub = tmp_path / "projC" / "session-3" / "subagents" / "agent-c1.jsonl"
    _write_jsonl(sub, [
        _assistant_line(session_id="session-3", request_id="req-sub-3", is_sidechain=True),
    ])
    single_pattern = str(tmp_path / "*" / "*" / "subagents" / "*.jsonl")
    rows_imported, sessions_seen, _ = import_transcripts(single_pattern, db_file)
    assert rows_imported == 1
    assert sessions_seen == {"session-3"}
