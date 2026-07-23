"""Tests for tools/preflight_quota.py. No network, no LLM calls; every
test builds a synthetic gateway/-shaped tmp directory (config.yaml,
budgets.yaml, *.db) and points the functions under test at it via the
root= parameter -- mirrors tools/test_usage_report.py's style.

Run from the repo root: python -m pytest tools/test_preflight_quota.py
"""

import datetime
import sqlite3
from pathlib import Path

import pytest
import yaml

from preflight_quota import (
    QuotaDatabaseLockedError,
    alias_provider_models,
    default_root,
    discover_dbs,
    format_text,
    load_budgets,
    load_config,
    normalize_provider_model,
    parse_provider_429,
    parse_ts,
    release_schedule,
    resolve_limit,
    resolve_target,
    usage_in_window,
)

REQUESTS_SCHEMA = """
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

CONFIG = {
    "model_list": [
        {"model_name": "middle-groq", "litellm_params": {"model": "groq/llama-3.3-70b-versatile"}},
        {"model_name": "builder-groq", "litellm_params": {"model": "groq/openai/gpt-oss-120b"}},
        {"model_name": "judge-groq", "litellm_params": {"model": "groq/openai/gpt-oss-120b"}},
        {"model_name": "lead-gemini", "litellm_params": {"model": "gemini/gemini-2.5-flash"}},
        {"model_name": "mock", "litellm_params": {"model": "anthropic/claude-fable-5"}},
    ]
}

BUDGETS = {
    "quota_windows": {
        "middle-groq": [{"window_seconds": 86400, "limit_tokens": 100000}],
        "builder-groq": [{"window_seconds": 60, "limit_tokens": 8000}],
    }
}


def _seed_root(tmp_path, config=None, budgets=None) -> Path:
    root = tmp_path / "gateway"
    root.mkdir()
    with open(root / "config.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(config if config is not None else CONFIG, f)
    with open(root / "budgets.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(budgets if budgets is not None else BUDGETS, f)
    return root


def _seed_db(db_path: Path, rows: list):
    """rows: list of (ts, provider_model, status, total_tokens)."""
    conn = sqlite3.connect(db_path)
    conn.execute(REQUESTS_SCHEMA)
    for ts, provider_model, status, total_tokens in rows:
        conn.execute(
            "INSERT INTO requests (ts, model, provider_model, status, total_tokens)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, "some-alias", provider_model, status, total_tokens),
        )
    conn.commit()
    conn.close()


# ---- load_config exists-guard (a documented finding, class D-0043
# alongside load_budgets, which already had this shape) ----


def test_load_config_missing_file_returns_empty_dict(tmp_path):
    root = tmp_path / "gateway"
    root.mkdir()
    # no config.yaml written at all -- this toolkit's own subscription-
    # contour default state.
    assert load_config(root) == {}


def test_load_config_existing_valid_file_still_loads(tmp_path):
    root = _seed_root(tmp_path)
    config = load_config(root)
    assert config["model_list"][0]["model_name"] == "middle-groq"


def test_load_config_malformed_yaml_still_raises(tmp_path):
    # The exists-guard covers ABSENCE only -- a config.yaml that exists
    # but is not valid YAML is a different failure class (corrupt
    # content) and must still surface loudly here (this function's own
    # caller with no fallback -- session_context.py's quota_lines() --
    # catches it at ITS OWN boundary instead). load_budgets() right
    # below used to share this exact asymmetry but no longer does (see
    # its own tests): the two functions are guarded at different layers
    # now, not identically.
    root = tmp_path / "gateway"
    root.mkdir()
    (root / "config.yaml").write_text("model_list: [unclosed", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_config(root)


def test_load_budgets_missing_file_returns_empty_quota_windows(tmp_path):
    # Sibling check (rule 9): load_budgets already had this exists-guard
    # shape before this task -- confirms load_config now matches it,
    # not a new asymmetry.
    root = tmp_path / "gateway"
    root.mkdir()
    assert load_budgets(root) == {"quota_windows": {}}
    assert "_parse_error" not in load_budgets(root)


def test_load_budgets_malformed_yaml_no_longer_raises(tmp_path):
    # load_budgets() now GUARDS parseability internally -- corrupt
    # content degrades to the same default as absence, plus an honest
    # "_parse_error" reason.
    root = tmp_path / "gateway"
    root.mkdir()
    (root / "budgets.yaml").write_text(
        "quota_windows: [this is not: valid: yaml: at all\n", encoding="utf-8"
    )
    result = load_budgets(root)
    assert result["quota_windows"] == {}
    assert "_parse_error" in result
    assert result["_parse_error"]


def test_load_budgets_malformed_yaml_reason_is_single_line(tmp_path):
    root = tmp_path / "gateway"
    root.mkdir()
    (root / "budgets.yaml").write_text(
        "quota_windows: [this is not: valid: yaml: at all\n", encoding="utf-8"
    )
    result = load_budgets(root)
    assert len(result["_parse_error"].splitlines()) == 1


def test_load_budgets_valid_yaml_no_parse_error_key(tmp_path):
    root = _seed_root(tmp_path)
    result = load_budgets(root)
    assert "_parse_error" not in result


# ---- config / provider_model normalization ----

def test_normalize_provider_model_strips_first_segment_only():
    assert normalize_provider_model("groq/openai/gpt-oss-120b") == "openai/gpt-oss-120b"
    assert normalize_provider_model("groq/llama-3.3-70b-versatile") == "llama-3.3-70b-versatile"
    assert normalize_provider_model("gemini/gemini-2.5-flash") == "gemini-2.5-flash"
    assert normalize_provider_model("no-slash-model") == "no-slash-model"


def test_alias_provider_models_mapping():
    mapping = alias_provider_models(CONFIG)
    assert mapping["middle-groq"] == "llama-3.3-70b-versatile"
    assert mapping["builder-groq"] == "openai/gpt-oss-120b"
    assert mapping["judge-groq"] == "openai/gpt-oss-120b"


def test_resolve_target_groups_aliases_sharing_provider_model():
    # judge-groq and builder-groq both sit on groq/openai/gpt-oss-120b --
    # their traffic sums against ONE provider quota (spec example).
    provider_model, group = resolve_target(CONFIG, "builder-groq")
    assert provider_model == "openai/gpt-oss-120b"
    assert group == {"builder-groq", "judge-groq"}


def test_resolve_target_singleton_group_for_unique_provider_model():
    provider_model, group = resolve_target(CONFIG, "middle-groq")
    assert provider_model == "llama-3.3-70b-versatile"
    assert group == {"middle-groq"}


def test_resolve_target_unknown_alias_raises_keyerror():
    with pytest.raises(KeyError):
        resolve_target(CONFIG, "nonexistent-alias")


def test_default_root_points_at_gateway_dir():
    root = default_root()
    assert root.name == "gateway"


# ---- ts parsing: both formats ----

def test_parse_ts_handles_t_separator():
    dt = parse_ts("2026-07-10T02:18:52.122060")
    assert dt == datetime.datetime(2026, 7, 10, 2, 18, 52, 122060)


def test_parse_ts_handles_space_separator():
    dt = parse_ts("2026-07-10 02:18:52.122060")
    assert dt == datetime.datetime(2026, 7, 10, 2, 18, 52, 122060)


def test_parse_ts_both_formats_agree():
    assert parse_ts("2026-07-10T02:18:52.122060") == parse_ts("2026-07-10 02:18:52.122060")


# ---- window sum: boundary, multi-db, grouping ----

def test_usage_in_window_sums_only_matching_provider_model(tmp_path):
    root = _seed_root(tmp_path)
    now = datetime.datetime(2026, 7, 10, 12, 0, 0)
    _seed_db(
        root / "requests.db",
        [
            ("2026-07-10T11:00:00", "llama-3.3-70b-versatile", "success", 500),
            ("2026-07-10T11:00:00", "openai/gpt-oss-120b", "success", 9999),  # different model, must NOT count
        ],
    )
    usage = usage_in_window(root, "llama-3.3-70b-versatile", 86400, now)
    assert usage["used_tokens"] == 500


def test_usage_in_window_boundary_24h(tmp_path):
    root = _seed_root(tmp_path)
    now = datetime.datetime(2026, 7, 10, 12, 0, 0)
    inside = now - datetime.timedelta(seconds=86400) + datetime.timedelta(seconds=1)
    outside = now - datetime.timedelta(seconds=86400) - datetime.timedelta(seconds=1)
    exactly_on = now - datetime.timedelta(seconds=86400)
    _seed_db(
        root / "requests.db",
        [
            (inside.isoformat(), "llama-3.3-70b-versatile", "success", 100),
            (outside.isoformat(), "llama-3.3-70b-versatile", "success", 200),
            (exactly_on.isoformat(), "llama-3.3-70b-versatile", "success", 300),
        ],
    )
    usage = usage_in_window(root, "llama-3.3-70b-versatile", 86400, now)
    # inside (100) and exactly-on-the-boundary (300, ts >= since is inclusive)
    # count; strictly-outside (200) does not.
    assert usage["used_tokens"] == 400


def test_usage_in_window_excludes_non_success_status(tmp_path):
    root = _seed_root(tmp_path)
    now = datetime.datetime(2026, 7, 10, 12, 0, 0)
    _seed_db(
        root / "requests.db",
        [
            ("2026-07-10T11:00:00", "llama-3.3-70b-versatile", "failure", 700),
        ],
    )
    usage = usage_in_window(root, "llama-3.3-70b-versatile", 86400, now)
    assert usage["used_tokens"] == 0


def test_usage_in_window_sums_across_multiple_dbs(tmp_path):
    # Main requests.db + a side db (F-27: gateway/t013.db-style GATEWAY_DB_PATH
    # traffic burns the same provider quota and must be counted too).
    root = _seed_root(tmp_path)
    now = datetime.datetime(2026, 7, 10, 12, 0, 0)
    _seed_db(root / "requests.db", [("2026-07-10T11:00:00", "llama-3.3-70b-versatile", "success", 14175)])
    _seed_db(root / "t013.db", [("2026-07-09T18:56:00", "llama-3.3-70b-versatile", "success", 68054)])
    usage = usage_in_window(root, "llama-3.3-70b-versatile", 86400, now)
    assert usage["used_tokens"] == 14175 + 68054
    assert usage["by_db"]["requests.db"] == 14175
    assert usage["by_db"]["t013.db"] == 68054


def test_usage_in_window_group_sum_shared_provider_model(tmp_path):
    # judge-groq and builder-groq traffic both land under provider_model
    # "openai/gpt-oss-120b" -- usage_in_window queries by provider_model,
    # so both aliases' rows sum together automatically.
    root = _seed_root(tmp_path)
    now = datetime.datetime(2026, 7, 10, 12, 0, 0)
    _seed_db(
        root / "requests.db",
        [
            ("2026-07-10T11:00:00", "openai/gpt-oss-120b", "success", 3000),  # from builder-groq
            ("2026-07-10T11:30:00", "openai/gpt-oss-120b", "success", 2000),  # from judge-groq
        ],
    )
    usage = usage_in_window(root, "openai/gpt-oss-120b", 86400, now)
    assert usage["used_tokens"] == 5000


def test_discover_dbs_skips_files_without_requests_table(tmp_path):
    root = _seed_root(tmp_path)
    conn = sqlite3.connect(root / "unrelated.db")
    conn.execute("CREATE TABLE something_else (id INTEGER)")
    conn.commit()
    conn.close()
    _seed_db(root / "requests.db", [])
    found = {p.name for p in discover_dbs(root)}
    assert found == {"requests.db"}


# ---- limit resolution: budgets.yaml vs --limit-tokens vs neither ----

def test_resolve_limit_from_budgets_yaml():
    assert resolve_limit(BUDGETS, "middle-groq", 86400) == 100000


def test_resolve_limit_override_wins_over_budgets_yaml():
    assert resolve_limit(BUDGETS, "middle-groq", 86400, override=5) == 5


def test_resolve_limit_none_when_neither_given():
    assert resolve_limit(BUDGETS, "middle-groq", 3600) is None  # wrong window
    assert resolve_limit(BUDGETS, "unknown-alias", 86400) is None


# ---- release schedule ----

def test_release_schedule_computes_go_at_when_headroom_reached(tmp_path):
    now = datetime.datetime(2026, 7, 10, 12, 0, 0)
    rows = [(now - datetime.timedelta(hours=22), 90000)]  # ages out in 2h
    schedule, go_at = release_schedule(rows, limit=100000, window_seconds=86400, need=15000, now=now)
    assert len(schedule) == 24
    # headroom starts at 10000 (100000-90000), below need=15000, until the
    # row ages out at +2h, after which headroom jumps to 100000.
    assert go_at is not None
    assert go_at == now + datetime.timedelta(hours=2)


def test_release_schedule_go_at_none_when_never_reached_in_24h():
    now = datetime.datetime(2026, 7, 10, 12, 0, 0)
    rows = [(now - datetime.timedelta(hours=1), 99000)]  # ages out at +23h, near the end
    # need exceeds the limit itself, so headroom can NEVER reach it, no
    # matter how much ages out of the window within 24h.
    schedule, go_at = release_schedule(rows, limit=100000, window_seconds=86400, need=150000, now=now)
    assert go_at is None


def test_release_schedule_headroom_immediately_sufficient():
    now = datetime.datetime(2026, 7, 10, 12, 0, 0)
    schedule, go_at = release_schedule([], limit=100000, window_seconds=86400, need=1000, now=now)
    # no usage at all -> headroom already >= need at the very first bucket
    assert go_at == now + datetime.timedelta(hours=1)


# ---- 429 provider-truth parsing (canonical example, no network) ----

GROQ_429_SHORT = (
    "Rate limit reached for model `llama-3.3-70b-versatile` in organization "
    "`org_xxxxxxxxxxxxxxxxxxxxxxxx` service tier `on_demand` on tokens per "
    "minute (TPM): Limit 12000, Used 11862, Requested 1758. Please try again "
    "in 3.1s. Need more tokens? Upgrade to Dev Tier today at "
    "https://console.groq.com/settings/billing"
)

GROQ_429_LONG_WAIT = (
    "Rate limit reached for model `openai/gpt-oss-120b`: Limit 100000, "
    "Used 90614, Requested 17053, try again in 1h50m37.926s."
)


def test_parse_provider_429_canonical_example():
    parsed = parse_provider_429(GROQ_429_SHORT)
    assert parsed == {
        "limit": 12000,
        "used": 11862,
        "requested": 1758,
        "retry_after_text": "3.1s",
    }


def test_parse_provider_429_long_wait_variant():
    parsed = parse_provider_429(GROQ_429_LONG_WAIT)
    assert parsed["limit"] == 100000
    assert parsed["used"] == 90614
    assert parsed["requested"] == 17053
    assert parsed["retry_after_text"] == "1h50m37.926s"


def test_parse_provider_429_returns_none_for_unrelated_text():
    assert parse_provider_429("500 internal server error, nothing to see here") is None


# ---- GO/NO-GO exit codes (via main(), no network since --probe is not passed) ----

def test_main_go_exit_code_zero(tmp_path, capsys):
    from preflight_quota import main

    root = _seed_root(tmp_path)
    now = datetime.datetime.now()
    _seed_db(root / "requests.db", [(now.isoformat(), "llama-3.3-70b-versatile", "success", 100)])
    code = main(["--alias", "middle-groq", "--need", "1000", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "VERDICT: GO" in out


def test_main_no_go_exit_code_one(tmp_path, capsys):
    from preflight_quota import main

    root = _seed_root(tmp_path)
    now = datetime.datetime.now()
    _seed_db(root / "requests.db", [(now.isoformat(), "llama-3.3-70b-versatile", "success", 99000)])
    code = main(["--alias", "middle-groq", "--need", "50000", "--root", str(root)])
    assert code == 1
    out = capsys.readouterr().out
    assert "VERDICT: NO-GO" in out


def test_main_exit_code_two_when_limit_not_measured_and_not_given(tmp_path, capsys):
    from preflight_quota import main

    root = _seed_root(tmp_path)
    code = main(["--alias", "builder-groq", "--need", "1000", "--window", "86400", "--root", str(root)])
    # builder-groq only has a 60s window in BUDGETS -- no 86400s entry, no override.
    assert code == 2
    err = capsys.readouterr().err
    assert "not measured and not given" in err


def test_main_limit_tokens_override_avoids_exit_two(tmp_path, capsys):
    from preflight_quota import main

    root = _seed_root(tmp_path)
    code = main(
        ["--alias", "builder-groq", "--need", "1000", "--window", "86400",
         "--limit-tokens", "5000", "--root", str(root)]
    )
    assert code in (0, 1)  # measured GO/NO-GO, not the exit-2 error path


# ---- N5 (a documented review finding): locked db fails loud, not silently, not a traceback ----

def test_discover_dbs_raises_on_locked_database(tmp_path):
    # Sibling fix (found while testing usage_in_window's guard): discover_dbs's
    # own schema probe hits the identical lock and, before this fix, silently
    # dropped the db from the discovered list via its bare 'except
    # sqlite3.Error: continue' -- verified empirically the schema read itself
    # blocks under another connection's BEGIN EXCLUSIVE.
    root = _seed_root(tmp_path)
    db_path = root / "requests.db"
    _seed_db(db_path, [])
    locker = sqlite3.connect(db_path)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(QuotaDatabaseLockedError) as exc_info:
            discover_dbs(root)
        assert "requests.db" in str(exc_info.value)
    finally:
        locker.rollback()
        locker.close()


def test_usage_in_window_raises_quota_database_locked_error(tmp_path):
    # Real sqlite lock (no mocks): a second connection holding an EXCLUSIVE
    # transaction open makes the reading connection inside usage_in_window
    # genuinely hit sqlite3.OperationalError("database is locked").
    root = _seed_root(tmp_path)
    db_path = root / "requests.db"
    _seed_db(db_path, [("2026-07-10T11:00:00", "llama-3.3-70b-versatile", "success", 100)])

    locker = sqlite3.connect(db_path)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        now = datetime.datetime(2026, 7, 10, 12, 0, 0)
        with pytest.raises(QuotaDatabaseLockedError) as exc_info:
            usage_in_window(root, "llama-3.3-70b-versatile", 86400, now)
        assert "requests.db" in str(exc_info.value)
    finally:
        locker.rollback()
        locker.close()


def test_main_exit_two_on_locked_database(tmp_path, capsys, monkeypatch):
    # main()'s own catch/exit-2/message logic -- the underlying detection
    # is proven for real above; here we isolate main()'s handling so this
    # test does not also pay the sqlite busy-timeout wait.
    import preflight_quota

    root = _seed_root(tmp_path)

    def fake_usage_in_window(*a, **kw):
        raise QuotaDatabaseLockedError("requests.db")

    monkeypatch.setattr(preflight_quota, "usage_in_window", fake_usage_in_window)
    code = preflight_quota.main(["--alias", "middle-groq", "--need", "1000", "--root", str(root)])
    assert code == 2
    err = capsys.readouterr().err
    assert "locked" in err
    assert "requests.db" in err


# ---- N3 (a documented review finding): probe-informed conservative go_at + reconciliation line ----

def test_format_text_reports_conservative_horizon_and_reconciliation():
    now = datetime.datetime(2026, 7, 10, 12, 0, 0)
    rows = [(now - datetime.timedelta(hours=1), 20000)]  # local corzina, ages out at +23h
    schedule_opt, go_at_opt = release_schedule(rows, limit=100000, window_seconds=86400, need=20000, now=now)
    delta = 70000
    augmented_rows = rows + [(now, delta)]  # off-ledger delta ages out at exactly +24h
    schedule_cons, go_at_cons = release_schedule(
        augmented_rows, limit=100000, window_seconds=86400, need=20000, now=now
    )

    report = {
        "alias": "middle-groq", "provider_model": "llama-3.3-70b-versatile",
        "group_aliases": ["middle-groq"], "window_seconds": 86400,
        "limit_tokens": 100000, "limit_source": "budgets.yaml",
        "used_tokens": 90000, "local_used_tokens": 20000,
        "headroom_tokens": 10000, "need_tokens": 20000, "verdict": "NO-GO",
        "by_db": {"requests.db": 20000},
        "since": (now - datetime.timedelta(seconds=86400)).isoformat(),
        "now": now.isoformat(),
        "schedule": [{**s, "bucket_end": s["bucket_end"].isoformat()} for s in schedule_opt],
        "go_at": go_at_opt.isoformat() if go_at_opt else None,
        "schedule_conservative": [{**s, "bucket_end": s["bucket_end"].isoformat()} for s in schedule_cons],
        "go_at_conservative": go_at_cons.isoformat() if go_at_cons else None,
        "reconciliation": {"provider_used": 90000, "local_used": 20000, "delta": 70000},
        "probe": {
            "ok": False, "status": 429,
            "provider_429": {"limit": 100000, "used": 90000, "requested": 1000, "retry_after_text": "5s"},
        },
    }
    text = format_text(report)
    assert "RECONCILIATION: provider Used=90000 tok, local sum=20000 tok, delta=70000 tok" in text
    assert "[optimistic: local corzinas only]" in text
    assert "[conservative: off-ledger delta released at window end]" in text
    # base usage is higher in the conservative model -> its go_at can never
    # be earlier than the optimistic one.
    assert go_at_cons is not None and go_at_opt is not None and go_at_cons >= go_at_opt


def test_main_probe_delta_full_flow_reports_both_horizons(tmp_path, capsys, monkeypatch):
    # End-to-end through main(): probe reveals provider Used (90000) far
    # above our local sum (20000) -- verdict was already provider-based
    # before this fix (headroom computed from bumped `used`); N3 fixes
    # go_at, which used to stay optimistic (local-only) even then.
    import preflight_quota

    root = _seed_root(tmp_path)
    now = datetime.datetime.now()
    _seed_db(
        root / "requests.db",
        [((now - datetime.timedelta(hours=1)).isoformat(), "llama-3.3-70b-versatile", "success", 20000)],
    )

    def fake_probe(alias, *a, **kw):
        return {
            "ok": False, "status": 429, "raw_error": "...",
            "provider_429": {"limit": 100000, "used": 90000, "requested": 1000, "retry_after_text": "5s"},
        }

    monkeypatch.setattr(preflight_quota, "probe", fake_probe)
    code = preflight_quota.main(
        ["--alias", "middle-groq", "--need", "20000", "--probe", "--root", str(root)]
    )
    out = capsys.readouterr().out
    assert code == 1  # NO-GO: headroom 100000-90000=10000 < need 20000
    assert "RECONCILIATION: provider Used=90000 tok, local sum=20000 tok, delta=70000 tok" in out
    assert "[optimistic: local corzinas only]" in out
    assert "[conservative: off-ledger delta released at window end]" in out


def test_no_probe_output_unchanged_single_horizon(tmp_path, capsys):
    # Backward compatibility: without --probe (or with no delta found),
    # the single-horizon text is byte-for-byte the pre-N3 format (no
    # "[optimistic...]"/"[conservative...]" labels, no RECONCILIATION line).
    from preflight_quota import main

    root = _seed_root(tmp_path)
    now = datetime.datetime.now()
    _seed_db(root / "requests.db", [(now.isoformat(), "llama-3.3-70b-versatile", "success", 99000)])
    code = main(["--alias", "middle-groq", "--need", "50000", "--root", str(root)])
    out = capsys.readouterr().out
    assert code == 1
    assert "RECONCILIATION" not in out
    assert "[optimistic" not in out
    assert "[conservative" not in out
    assert "next possible GO (measured release schedule):" in out or (
        "no hour in the next 24h reaches headroom >= need" in out
    )
