"""dod_track.py -- PostToolUse hook that accumulates a session/subagent's
edits and verification runs into a per-session track file, which
tools/dod_gate.py (SubagentStop) and tools/main_gate.py (Stop) read to
decide whether to block a stop: the deterministic invariant is "the
last edit happened before the last GREEN verification run" (code
guarantees the check gets encountered; a tier above judges what the
run's output means).

Contract (PostToolUse hook stdin JSON): base fields (session_id, cwd,
agent_id?) plus hook_event_name="PostToolUse", tool_name, tool_input,
tool_response. agent_id is present only when the hook fires from
inside a subagent; it is absent for the main thread even in an
--agent session -- this is the field build_fact() uses to tell
main-thread edits/runs apart from a subagent's own (see
_extract_agent_id). session_id is the SAME value for the main session
and every one of its subagents; without agent_id there would be no
way to separate a coordinator's own edits from a worker's in the
shared track file.

Storage: .claude/dod_track/<session_id>.json in the calling session's
cwd. Format:
 {"edits": [{"ts": ISO, "tool_name": str, "agent_id": str|None,
             "file_path": str|None}, ...],
  "runs":  [{"ts": ISO, "tool_name": str, "command": str,
             "outcome": "green"|"red", "agent_id": str|None}, ...],
  "gate_state": {...}}       -- owned by tools/dod_gate.py
  "main_gate_state": {...}}  -- owned by tools/main_gate.py, a
                                 SEPARATE counter from gate_state:
                                 session_id is shared between the
                                 main thread and every subagent, so a
                                 shared counter would let a Stop block
                                 and a SubagentStop block interfere
                                 with each other.
This file never touches gate_state/main_gate_state/gate_log -- its
read-modify-write preserves any keys it doesn't know about.

is_verification_command() recognizes three forms of a witness run:
 - pytest / "python -m pytest" / "python ...test..." (VERIFICATION_COMMAND_RE).
 - a Node script run directly (node foo.js/.mjs/.cjs) -- NODE_SCRIPT_RE.
 - a browser-automation or screenshot command (playwright/puppeteer/
   selenium/screencap/screenshot) -- UI_WITNESS_RE, for tasks whose
   DoD requires driving a UI (rule 11: "a task with a UI result...
   the witness is a before/after screenshot").
determine_outcome() classifies a recognized run as "green" or "red":
an rc/exit_code-shaped field in tool_response decides unconditionally
when present; otherwise a text heuristic over stdout+stderr (failure
words beat success words; neither present defaults to "red" --
an unrecognized outcome is not a confirmed green run). This means a
witness script that only saves a file silently, with no textual
"passed"/"ok"/"failed"/"error" confirmation, is recognized as a run
but still lands on the safe "red" default -- to register as green
reliably, a witness script should print an explicit confirmation.

tool_name in {"Bash", "PowerShell"}: both are accepted as the shell
tool that ran the command -- different harness environments invoke
shell commands through one or the other, and a verification run
should be visible to the track regardless of which tool executed it.

Known limitation, not solved here: concurrent PostToolUse hook
processes (parallel tool calls in the same turn) do a read-modify-
write on the same file with no locking -- a race is possible, and the
last write can silently drop a fact from a parallel call.

This hook never blocks (always exit 0) except on unrecognized/
unrelated input, which is also exit 0 with no side effects -- the
same fail-open principle as every other hook in this file set.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

EDIT_TOOL_NAMES = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

VERIFICATION_COMMAND_RE = re.compile(
    r"pytest|python\s+-m\s+pytest|python\s+.*test", re.IGNORECASE
)

# A command that runs a .js/.mjs/.cjs file through node directly.
NODE_SCRIPT_RE = re.compile(r"\bnode\s+\S+\.(?:m?js|cjs)\b", re.IGNORECASE)

# A command that names a browser-automation/screenshot tool -- the
# most likely wrapper for a UI-driving witness run (rule 11: an
# interactive-surface task's DoD includes driving the UI). This does
# not try to cover every conceivable CLI witness form -- a deliberate,
# narrow default.
UI_WITNESS_RE = re.compile(
    r"screenshot|playwright|puppeteer|selenium|screencap", re.IGNORECASE
)

FAILURE_INDICATORS_RE = re.compile(r"failed|error|traceback", re.IGNORECASE)
SUCCESS_INDICATORS_RE = re.compile(r"passed|\bok\b", re.IGNORECASE)

NUMERIC_RC_FIELDS = ("rc", "exit_code", "returnCode", "return_code")


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")


def is_edit_tool(tool_name) -> bool:
    return tool_name in EDIT_TOOL_NAMES


def is_verification_command(command: str) -> bool:
    command = command or ""
    if VERIFICATION_COMMAND_RE.search(command):
        return True
    if NODE_SCRIPT_RE.search(command):
        return True
    if UI_WITNESS_RE.search(command):
        return True
    return False


def _extract_rc(tool_response):
    """Tries to find a numeric return code in tool_response. Several
    plausible field names are tried since not every shell-tool
    response shape carries the same one."""
    if not isinstance(tool_response, dict):
        return None
    for key in NUMERIC_RC_FIELDS:
        value = tool_response.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _extract_text(tool_response) -> str:
    """Collects text for the text heuristics. The common shell-tool
    response shape is {"stdout": str, "stderr": str, ...}; other
    shapes fall back to a full JSON dump so the regexes still have
    something to search."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        parts = []
        for key in ("stdout", "stderr", "text", "output"):
            value = tool_response.get(key)
            if isinstance(value, str):
                parts.append(value)
        if parts:
            return "\n".join(parts)
        try:
            return json.dumps(tool_response, ensure_ascii=False)
        except Exception:
            return str(tool_response)
    return str(tool_response)


def determine_outcome(tool_response) -> str:
    """"green" | "red". rc, when available, decides unconditionally
    (rc==0 -> green, otherwise red); otherwise text heuristics.
    Neither a failure nor a success indicator present (an ambiguous
    output, e.g. "no tests collected") defaults to "red": an
    unrecognized output is not a confirmed green run, and the whole
    point of the gate is to withhold acceptance without one."""
    rc = _extract_rc(tool_response)
    if rc is not None:
        return "green" if rc == 0 else "red"

    text = _extract_text(tool_response)
    has_failure = bool(FAILURE_INDICATORS_RE.search(text))
    has_success = bool(SUCCESS_INDICATORS_RE.search(text))

    if has_failure:
        return "red"
    if has_success:
        return "green"
    return "red"


def _extract_agent_id(payload: dict):
    """Distinguishes a main-thread event from a subagent one:
    agent_id is a base hook field, present only when the hook fires
    from inside a subagent, absent (None) for the main thread.
    Returns str (subagent) | None (main thread); an empty string is
    also treated as None (guards against a degenerate payload)."""
    value = payload.get("agent_id")
    return value if isinstance(value, str) and value else None


def build_fact(payload: dict):
    """Pure logic: decides what fact (if any) to record from an
    event's payload. Returns ("edit", {...}) | ("run", {...}) | None
    (the event isn't relevant to the DoD track). No side effects --
    directly testable without I/O.

    Every fact carries "agent_id" (str | None); None means main
    thread. tools/main_gate.py (Stop) filters to main-only records;
    tools/dod_gate.py (SubagentStop) reads every record and filters to
    its own agent_id (see that file for the per-agent-filter logic)."""
    tool_name = payload.get("tool_name")
    agent_id = _extract_agent_id(payload)

    if is_edit_tool(tool_name):
        tool_input = payload.get("tool_input") or {}
        file_path = tool_input.get("file_path")
        return "edit", {
            "ts": _now_iso(),
            "tool_name": tool_name,
            "agent_id": agent_id,
            "file_path": file_path if isinstance(file_path, str) else None,
        }

    if tool_name in ("Bash", "PowerShell"):
        tool_input = payload.get("tool_input") or {}
        command = tool_input.get("command") or ""
        if is_verification_command(command):
            outcome = determine_outcome(payload.get("tool_response"))
            return "run", {
                "ts": _now_iso(),
                "tool_name": tool_name,
                "command": command,
                "outcome": outcome,
                "agent_id": agent_id,
            }
        return None

    return None


def _track_path(cwd: str, session_id: str) -> Path:
    return Path(cwd or ".") / ".claude" / "dod_track" / f"{session_id}.json"


def _load_track(path: Path) -> dict:
    if not path.exists():
        return {"edits": [], "runs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # A corrupt track file must not take down the hook -- start
        # fresh (fail open); edits/runs are lost, but that's better
        # than a gate stuck for the whole session over broken JSON.
        return {"edits": [], "runs": []}
    if not isinstance(data, dict):
        return {"edits": [], "runs": []}
    data.setdefault("edits", [])
    data.setdefault("runs", [])
    return data


def _save_track(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _reconfigure_stderr_utf8():
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    _reconfigure_stderr_utf8()

    # Raw-byte stdin read, decoded explicitly as UTF-8 -- see
    # dispatch_gate.py's main() for the platform-encoding rationale.
    raw_bytes = sys.stdin.buffer.read()
    raw = raw_bytes.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except Exception:
        return 0

    fact = build_fact(payload)
    if fact is None:
        return 0

    session_id = payload.get("session_id")
    if not session_id:
        # No session_id -- nowhere to write the track (the file is
        # named by session_id) -- fail open, the fact is lost but the
        # hook does not crash.
        return 0

    cwd = payload.get("cwd") or "."
    path = _track_path(cwd, session_id)
    data = _load_track(path)

    kind, entry = fact
    data.setdefault(kind + "s", []).append(entry)
    _save_track(path, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
