"""main_gate.py -- Stop hook that blocks the MAIN thread's own turn
from ending with the same invariant tools/dod_gate.py applies to a
SubagentStop: "no green verification run after the last edit". This
exists because SubagentStop structurally never sees the coordinator's
own session -- it only fires when a SUBAGENT stops -- so a
coordinator that edits a deliverable itself after the last
critic/DoD cycle was, before this hook, invisible to every other gate
in this file set.

Contract (Stop hook stdin JSON): base fields (session_id, cwd) plus
hook_event_name="Stop", stop_hook_active. Unlike SubagentStop, a
"Stop" event does not carry agent_id/agent_type as event-specific
fields; on the main thread, the base agent_id field (shared with every
other hook, see tools/dod_track.py) is also unset -- this is exactly
what lets tools/dod_track.py tell a main-thread record apart from a
subagent's own (see _is_main_entry below).

"Blocked" uses the same protocol as every other hook in this set: exit
code 2 with a message on stderr.

Checks in main():
 (a) main-thread edits happened (dod_track edits/runs with agent_id
     absent/None -- see tools/dod_track.py) AND there is no GREEN
     main-only run after the last main-only edit -> BLOCK. The same
     "2 consecutive blocks" safety valve as dod_gate.py -- a third
     consecutive block in one cycle is skipped, with an honest note
     in the track's gate_log (key "gate":"main" tells main_gate.py's
     log entries apart from dod_gate.py's in the shared list).
 (b) logs/routing-log.jsonl EXISTS and is empty (0 non-blank lines)
     WHILE the main-only dod_track is non-empty (at least one
     main-only edit/run) -> an ADDITIONAL warning line folded INTO the
     SAME block message as (a). Read literally: the warning lives
     INSIDE (a)'s block message, not as an independent path -- if (a)
     finds no violation (a green run after the last main-only edit
     exists), check (b) produces no separate output or exit code of
     its own, since there is no block message for it to be appended
     to.

This hook does not call any journal-format validator itself -- check
(b) is not about whether the journal is VALID, only about whether the
file on disk is empty; format validation is a separate concern
(tools/journal_validator.py, run at commit time).

Fail-open: unparseable JSON or a missing session_id -> exit 0, no
side effects, same as the rest of this file set.

Own counter: this hook keeps its OWN "main_gate_state.consecutive_blocks"
counter in the track file, deliberately separate from dod_gate.py's
"gate_state.consecutive_blocks" -- session_id is shared between the
main thread and every one of its subagents, so a shared counter would
let a Stop-block and a SubagentStop-block interfere with each other's
safety valve.
"""

import json
import sys
from pathlib import Path

BLOCK_MESSAGE = (
    "Stop blocked: there is no green verification run after the "
    "coordinator's last edit. Run your DoD check (pytest / your "
    "verification command) and stop on green."
)

EMPTY_JOURNAL_WARNING = (
    " WARNING: logs/routing-log.jsonl exists and is empty, even though "
    "the main-thread track is non-empty -- this session's routing has "
    "not been logged."
)

SAFETY_SKIP_MESSAGE = (
    "main_gate: safety valve triggered -- 2 consecutive blocks already "
    "happened in this session, the stop is allowed WITHOUT a green run "
    "(recorded in the track; this is not a substitute for verification)."
)

CONSECUTIVE_BLOCK_LIMIT = 2

JOURNAL_REL_PATH = Path("logs") / "routing-log.jsonl"


def _track_path(cwd: str, session_id: str) -> Path:
    return Path(cwd or ".") / ".claude" / "dod_track" / f"{session_id}.json"


def _default_track() -> dict:
    return {"edits": [], "runs": [], "main_gate_state": {"consecutive_blocks": 0}}


def _load_track(path: Path) -> dict:
    if not path.exists():
        return _default_track()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_track()
    if not isinstance(data, dict):
        return _default_track()
    data.setdefault("edits", [])
    data.setdefault("runs", [])
    data.setdefault("main_gate_state", {"consecutive_blocks": 0})
    data["main_gate_state"].setdefault("consecutive_blocks", 0)
    return data


def _save_track(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _is_main_entry(entry: dict) -> bool:
    """agent_id absent/None/empty -> a main-thread record (see
    tools/dod_track.py._extract_agent_id -- the same "empty" treatment)."""
    return not entry.get("agent_id")


# Same doc-only rule and the same fail-closed treatment of an unknown
# file_path as tools/dod_gate.py -- applied here to the MAIN-ONLY subset.
DOC_ONLY_EXTENSIONS = {".md", ".json", ".jsonl"}


def _is_doc_only_file(file_path) -> bool:
    if not isinstance(file_path, str) or not file_path:
        return False
    return Path(file_path).suffix.lower() in DOC_ONLY_EXTENSIONS


def _all_edits_doc_only(edits) -> bool:
    if not edits:
        return False
    return all(_is_doc_only_file(e.get("file_path")) for e in edits)


def evaluate(track: dict) -> tuple[bool, str]:
    """Check (a), pure logic -- the same signature/semantics as
    dod_gate.evaluate(), but on the MAIN-ONLY subset of edits/runs.
    Doc-only (.md/.json/.jsonl) main-only edits are exempt from the
    invariant entirely (see DOC_ONLY_EXTENSIONS above)."""
    edits = [e for e in (track.get("edits") or []) if _is_main_entry(e)]
    if not edits:
        return False, "no-main-edits"

    if _all_edits_doc_only(edits):
        return False, "doc-only-edits-exempt"

    runs = [r for r in (track.get("runs") or []) if _is_main_entry(r)]
    last_edit_ts = max(e["ts"] for e in edits)

    green_runs = [r for r in runs if r.get("outcome") == "green"]
    if not green_runs:
        return True, "no-green-run"

    last_green_ts = max(r["ts"] for r in green_runs)
    if last_green_ts < last_edit_ts:
        return True, "green-before-last-edit"

    return False, "green-after-last-edit"


def _journal_empty_warning_applies(cwd: str, track: dict) -> bool:
    """Check (b): True iff logs/routing-log.jsonl EXISTS and is empty
    (0 non-blank lines) WHILE the main-only dod_track is non-empty (at
    least one main-only edit/run -- literally "non-empty track", not
    "check (a) found a violation")."""
    journal_path = Path(cwd or ".") / JOURNAL_REL_PATH
    if not journal_path.exists():
        return False
    try:
        text = journal_path.read_text(encoding="utf-8")
    except Exception:
        return False
    if text.strip():
        return False  # journal is non-empty -- check (b) doesn't apply

    main_edits = [e for e in (track.get("edits") or []) if _is_main_entry(e)]
    main_runs = [r for r in (track.get("runs") or []) if _is_main_entry(r)]
    return bool(main_edits or main_runs)


def decide(track: dict, cwd: str = ".") -> tuple[int, str, dict]:
    """Pure decision logic after the track is loaded -- the same style
    as dod_gate.decide(). cwd is needed only for check (b) (reading
    logs/routing-log.jsonl); track is already loaded by the caller.
    Returns (exit_code, stderr_message, updated_track)."""
    violation, reason = evaluate(track)
    gate_state = track.setdefault("main_gate_state", {"consecutive_blocks": 0})
    consecutive = gate_state.get("consecutive_blocks", 0)

    if not violation:
        if consecutive:
            gate_state["consecutive_blocks"] = 0
        return 0, "", track

    warn = _journal_empty_warning_applies(cwd, track)

    if consecutive >= CONSECUTIVE_BLOCK_LIMIT:
        gate_state["consecutive_blocks"] = 0
        track.setdefault("gate_log", []).append(
            {"action": "skipped_after_2_blocks", "reason": reason, "gate": "main"}
        )
        message = SAFETY_SKIP_MESSAGE + (EMPTY_JOURNAL_WARNING if warn else "")
        return 0, message, track

    gate_state["consecutive_blocks"] = consecutive + 1
    track.setdefault("gate_log", []).append(
        {"action": "blocked", "reason": reason, "gate": "main"}
    )
    message = BLOCK_MESSAGE + (EMPTY_JOURNAL_WARNING if warn else "")
    return 2, message, track


def _reconfigure_stderr_utf8():
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    _reconfigure_stderr_utf8()

    raw_bytes = sys.stdin.buffer.read()
    raw = raw_bytes.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except Exception:
        return 0

    session_id = payload.get("session_id")
    if not session_id:
        return 0

    cwd = payload.get("cwd") or "."
    path = _track_path(cwd, session_id)
    existed_before = path.exists()
    track = _load_track(path)

    exit_code, message, updated_track = decide(track, cwd)

    # Same principle as dod_gate.py: don't create the track file out
    # of nothing if it didn't exist before and there are no main-only
    # edits at all (a session with zero coordinator edits -- a
    # read-only turn).
    if existed_before or updated_track.get("edits"):
        _save_track(path, updated_track)

    if message:
        sys.stderr.write(message + "\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
