"""dod_gate.py -- SubagentStop hook that blocks a worker's stop if
there is no green verification run after its last edit (rule 11: DoD
witness). It reads the track that tools/dod_track.py (PostToolUse)
writes -- see that file's docstring for the full track format.

Contract (SubagentStop hook stdin JSON): base fields (session_id, cwd)
plus hook_event_name="SubagentStop", agent_id, agent_type,
stop_hook_active. "Blocked" for a command hook means exit code 2 (this
hook's own protocol) -- the same convention as every other hook in
this file set.

Logic (main()):
 1. Read the track .claude/dod_track/<session_id>.json. No file, or no
    edits belonging to this agent -- nothing to check (a typical
    scout/critic-class subagent that never edited a file) -- pass,
    exit 0, gate_state untouched.
 2. There were edits: compare max(edit ts) against max(GREEN run ts),
    BOTH filtered to this agent's own records (see evaluate() below).
    No green run, or the last green run is earlier than the last
    edit -> DoD invariant violated.
 3. No violation (a green run exists after the last edit) -> exit 0;
    a consecutive_blocks counter accumulated from earlier violations
    resets to 0 (a successful stop clears the safety valve).
 4. Violation: look at gate_state.consecutive_blocks (0 by default).
      - < 2: block (exit 2 + BLOCK_MESSAGE on stderr), counter += 1.
      - >= 2 (this would be the THIRD consecutive block): safety valve
        -- do NOT block (exit 0), write SAFETY_SKIP_MESSAGE to stderr,
        reset the counter to 0 (a new cycle starts), and record a
        "skipped_after_2_blocks" gate_log event (visible for later
        review -- the skip is a fact, not silently dropped).
    Both branches append a gate_log event ("blocked" |
    "skipped_after_2_blocks") under the "gate_log" key of the same
    track file, alongside "edits"/"runs"/"gate_state"; dod_track.py's
    own read-modify-write leaves unknown keys alone.

Per-agent filtering (evaluate()/decide() take an agent_id parameter):
this hook's invariant is scoped to records belonging to THIS worker --
a coordinator's own main-thread edits (agent_id is None) and another
parallel worker's edits are NOT visible to this evaluation. Without
this, a shared track file (session_id is common to the main thread and
every subagent) would let one worker's unreviewed edit block a
completely different worker's stop, or let the coordinator's own
unreviewed edits block a clean subagent that never touched a file
itself. The coordinator's own zone belongs entirely to
tools/main_gate.py (Stop) -- dod_gate.py never looks at it, at any
agent_id. If a SubagentStop payload carries no agent_id at all
(a defensive branch -- agent_id=None passed to evaluate()/decide()),
the fallback is "every non-main record" (any non-empty agent_id):
main-thread edits are still excluded, but different workers are not
told apart from each other in this fallback path.

Doc-only exemption: if EVERY edit belonging to this agent has a
file_path ending in .md/.json/.jsonl (DOC_ONLY_EXTENSIONS), the
invariant is skipped entirely -- no run is required at all, not just
"a run after the last edit". .jsonl is included because editing
logs/routing-log.jsonl is a routine operation on every acceptance
turn, and it is data, not code -- gated by its own dedicated validator,
not by a test run. An edit record with an unknown file_path (None --
either an older track predating this field, or a malformed payload) is
treated CONSERVATIVELY as NOT doc-only -- the one fail-CLOSED branch
in an otherwise fail-open file: mistaking "unknown" for "definitely
just docs" is riskier than one extra block.

consecutive_blocks is ALSO scoped per-agent (gate_state["per_agent"][
<agent_key>]), not a single session-global counter -- see the
_FALLBACK_AGENT_KEY docstring above for the class of bug this fixes
(one worker's exhausted safety valve silently spending itself on a
different worker's first-ever block in the same session). gate_log
entries carry ts and agent_id (the deciding worker's own) -- old
entries without these fields still read back without raising
(append-only, nothing here parses gate_log entries back
programmatically).

Doc-only "whole-or-nothing" over edits AFTER the last green run of
THIS agent (edits_after_green), not over its whole filtered history in
the track -- ported from the same fix in main_gate.evaluate() (see its
docstring for the full class-of-bug rationale: an early code edit used
to void the doc-only exemption forever, even after a green run and a
purely doc-only tail).

Fail-open: a missing session_id, or an unparseable payload, exits 0
without side effects, same as the rest of this file set.
"""

import json
import sys
from datetime import datetime
from pathlib import Path


def _now_iso() -> str:
    # Same format as tools/dod_track.py._now_iso().
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")


# consecutive_blocks is counted PER-AGENT, not globally. Before this fix
# the counter was ONE per session (gate_state.consecutive_blocks) -- a
# worker X that exhausted the safety valve (2 blocks) let its own
# exhausted counter "spend itself" on a COMPLETELY DIFFERENT worker Y
# meeting a block for the first time in the same session (fail-open in
# the wrong direction -- Y would skip a verification it never actually
# passed). The counter now lives in
# gate_state["per_agent"][<agent_key>], independent per agent -- the
# same "own zone" semantics as evaluate()'s per-agent filter.
# _FALLBACK_AGENT_KEY is the key for the defensive branch (a payload
# with no agent_id at all -- see evaluate()/decide()).
_FALLBACK_AGENT_KEY = "__none__"


def _agent_state_key(agent_id) -> str:
    return agent_id if agent_id else _FALLBACK_AGENT_KEY


def _default_gate_state() -> dict:
    return {"consecutive_blocks": 0, "per_agent": {}}

BLOCK_MESSAGE = (
    "Stop blocked: there is no green verification run after the last "
    "edit. Run your DoD check (pytest / your verification command) "
    "and stop on green. Re-submission = a COMPLETE final report all "
    "over again (only your LAST message reaches the coordinator -- "
    "earlier text in this session is not delivered to it; a reference "
    "back to it is not a substitute, F-49-class hazard)."
)

SAFETY_SKIP_MESSAGE = (
    "dod_gate: safety valve triggered -- 2 consecutive blocks already "
    "happened in this session, the stop is allowed WITHOUT a green run "
    "(recorded in the track; this is not a substitute for verification)."
)

CONSECUTIVE_BLOCK_LIMIT = 2

# Extensions treated as "documentation/config, not code" -- an edit
# touching ONLY these does not require a verification run.
DOC_ONLY_EXTENSIONS = {".md", ".json", ".jsonl"}

# Known, code-less dotfiles WITHOUT a suffix (see main_gate.py for the
# full rationale and the final-list choice against this template's own
# actual files).
DOC_ONLY_DOTFILES = {".gitignore", ".gitattributes", ".editorconfig"}


def _is_doc_only_file(file_path) -> bool:
    if not isinstance(file_path, str) or not file_path:
        return False  # unknown path -- conservatively NOT doc-only
    path = Path(file_path)
    if path.name.lower() in DOC_ONLY_DOTFILES:
        return True
    return path.suffix.lower() in DOC_ONLY_EXTENSIONS


def _all_edits_doc_only(edits) -> bool:
    """True iff EVERY edit record has a known, doc-only (.md/.json/
    .jsonl) file_path. See the module docstring for the fail-closed
    rationale on an unknown file_path."""
    if not edits:
        return False
    return all(_is_doc_only_file(e.get("file_path")) for e in edits)


def _track_path(cwd: str, session_id: str) -> Path:
    return Path(cwd or ".") / ".claude" / "dod_track" / f"{session_id}.json"


def _load_track(path: Path) -> dict:
    if not path.exists():
        return {"edits": [], "runs": [], "gate_state": _default_gate_state()}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"edits": [], "runs": [], "gate_state": _default_gate_state()}
    if not isinstance(data, dict):
        return {"edits": [], "runs": [], "gate_state": _default_gate_state()}
    data.setdefault("edits", [])
    data.setdefault("runs", [])
    data.setdefault("gate_state", _default_gate_state())
    # Backward compat: an old track carries only a flat
    # "consecutive_blocks" (pre-per-agent) -- "per_agent" is added
    # ALONGSIDE it, the old field is left untouched.
    data["gate_state"].setdefault("consecutive_blocks", 0)
    data["gate_state"].setdefault("per_agent", {})
    return data


def _save_track(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def evaluate(track: dict, agent_id: str | None = None) -> tuple[bool, str]:
    """Pure invariant logic, no I/O -- directly testable.

    agent_id given (the normal SubagentStop path): edits and runs are
    filtered to records belonging to exactly this agent
    (e.get("agent_id") == agent_id) BEFORE the rest of the logic
    applies -- other workers' edits and main-thread edits (agent_id
    None/empty) are invisible to this evaluation.
    agent_id NOT given (None -- a defensive branch, a payload with no
    agent_id field): a conservative fallback, "every non-main record"
    (any non-empty agent_id) -- main-thread edits are still excluded,
    but different workers are not told apart from each other here.

    Returns (violation: bool, reason: str). reason is for debugging/
    tests only, not parsed by the caller.

    Doc-only "whole-or-nothing" over edits AFTER the last green run of
    THIS agent (edits_after_green), not over its whole filtered
    history in the track -- ported from the same fix in
    main_gate.evaluate() (see its docstring for the full class-of-bug
    rationale: an early code edit used to void the doc-only exemption
    forever, even after a green run and a purely doc-only tail)."""
    all_edits = track.get("edits") or []
    if agent_id:
        edits = [e for e in all_edits if e.get("agent_id") == agent_id]
    else:
        edits = [e for e in all_edits if e.get("agent_id")]
    if not edits:
        return False, "no-edits"

    all_runs = track.get("runs") or []
    if agent_id:
        runs = [r for r in all_runs if r.get("agent_id") == agent_id]
    else:
        runs = [r for r in all_runs if r.get("agent_id")]

    green_runs = [r for r in runs if r.get("outcome") == "green"]

    if not green_runs:
        if _all_edits_doc_only(edits):
            return False, "doc-only-edits-exempt"
        return True, "no-green-run"

    last_green_ts = max(r["ts"] for r in green_runs)
    edits_after_green = [e for e in edits if e["ts"] > last_green_ts]

    if not edits_after_green:
        return False, "green-after-last-edit"

    if _all_edits_doc_only(edits_after_green):
        return False, "doc-only-edits-exempt"

    return True, "green-before-last-edit"


def decide(track: dict, agent_id: str | None = None) -> tuple[int, str, dict]:
    """Pure decision logic after the track is loaded. agent_id passes
    straight through to evaluate() (see its docstring for the filter
    semantics) AND is used here for per-agent consecutive_blocks (see
    the _FALLBACK_AGENT_KEY docstring above). Returns (exit_code,
    stderr_message, updated_track); updated_track carries the updated
    gate_state/an appended gate_log event -- writing it to disk is
    main()'s job."""
    violation, reason = evaluate(track, agent_id)
    gate_state = track.setdefault("gate_state", _default_gate_state())
    gate_state.setdefault("per_agent", {})
    key = _agent_state_key(agent_id)
    agent_state = gate_state["per_agent"].setdefault(key, {"consecutive_blocks": 0})
    consecutive = agent_state.get("consecutive_blocks", 0)

    if not violation:
        if consecutive:
            agent_state["consecutive_blocks"] = 0
        return 0, "", track

    # gate_log entries carry ts (_now_iso) and agent_id -- backward
    # compat: old gate_log entries without these fields read back
    # without raising (append-only, nothing here parses them back).
    if consecutive >= CONSECUTIVE_BLOCK_LIMIT:
        agent_state["consecutive_blocks"] = 0
        track.setdefault("gate_log", []).append(
            {
                "action": "skipped_after_2_blocks",
                "reason": reason,
                "ts": _now_iso(),
                "agent_id": agent_id,
            }
        )
        return 0, SAFETY_SKIP_MESSAGE, track

    agent_state["consecutive_blocks"] = consecutive + 1
    track.setdefault("gate_log", []).append(
        {"action": "blocked", "reason": reason, "ts": _now_iso(), "agent_id": agent_id}
    )
    return 2, BLOCK_MESSAGE, track


def _reconfigure_stderr_utf8():
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _extract_agent_id_from_payload(payload: dict):
    """Reads the top-level "agent_id" payload field -- same "empty
    means unset" treatment as dod_track.py._extract_agent_id and
    main_gate.py's own main-entry check."""
    value = payload.get("agent_id")
    return value if isinstance(value, str) and value else None


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

    agent_id = _extract_agent_id_from_payload(payload)
    exit_code, message, updated_track = decide(track, agent_id)

    # "No edits -> pass" (see evaluate()): if the track file didn't
    # exist before AND this call adds no edits, do nothing at all --
    # a scout/critic-class subagent that never touched a file should
    # not grow an empty .claude/dod_track/<session_id>.json. If the
    # file already existed (dod_track.py created it earlier), always
    # write, to keep gate_state/gate_log consistent.
    if existed_before or updated_track.get("edits"):
        _save_track(path, updated_track)

    if message:
        sys.stderr.write(message + "\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
