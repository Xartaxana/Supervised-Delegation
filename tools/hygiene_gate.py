"""hygiene_gate.py -- PreToolUse hook for command hygiene, in WARN MODE
(non-blocking) for the Bash|PowerShell tools. Mechanizes CLAUDE.md's
"Command hygiene" points 3-5: a `cd` prefix, a trailing ` 2>&1`, a
`python -c`/`python - <<` edit bypassing Edit/Write, and a routing-log
write bypassing Edit/Write -- catches them BEFORE the command runs and
surfaces a warning with the canonical alternative, but NEVER blocks the
call (unlike a blocking gate that exits non-zero + stderr: this hook
always exits 0, its only side effect is stdout on a match).

Ported from HQ 2026-07-20.

DELIVERY CHANNEL (verified empirically against the installed harness
binary, not assumed from memory): the hook's response is delivered via
`hookSpecificOutput.additionalContext` on stdout, exit 0:

  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                           "additionalContext": "<list of matched classes>"}}

`permissionDecision` is deliberately OMITTED: an earlier draft set it to
"allow", which would have auto-approved the very command this hook
flags, silencing the operator's own permission prompt -- a review
finding on that draft. Leaving it out delivers the warning without
touching the permission path at all.

DETECTION CLASSES (independent checks; additionalContext lists ALL that
matched, not just the first):

 (a) cd-prefix: the command starts with `cd <non-empty argument>` (a
     real path, not a bare "cd" and not "cd&&...") AND somewhere later
     there is `&&` or `;`.
 (b) the literal substring ` 2>&1`.
 (c) `python -c` or `python - <<` -- literally "python" (not "python3":
     deliberately not generalized beyond what command hygiene names),
     with \\b word boundaries so "mypython -c" does not match as a
     substring.
 (d) a routing-log write bypassing Edit/Write: the class header is
     "write to the journal outside Edit/Write" (not "any redirect to a
     file outside Edit/Write" -- that is command hygiene point 4 in
     general; this class is specifically about point 5, the journal).
     Condition: the substring "routing-log" (case-insensitive) is
     present in the command AND (there is a `>` redirect OR a
     printf/echo token) -- requiring the "routing-log" substring for
     BOTH forms keeps this class about the journal specifically, not
     about any redirect whatsoever (`ls > out.txt` does not match).

All classes are case-insensitive (uniform choice; hygiene points don't
call out per-class case sensitivity).

ADVERSARIAL SAFETY ON LARGE INPUT: every check is a substring test
(`in`, O(n)) or a simple \\b-anchored regex with no nested
quantifiers (no `.*...*` chains that could cause catastrophic
backtracking) -- linear in the length of the command.

Fail-open: a non-Bash/PowerShell tool, empty/malformed stdin, a
non-dict payload, or a missing/non-string/empty command all fall
through silently, with no stdout side effect. The hook never returns a
non-zero exit code (WARN mode: never block, never crash non-zero on
any input)."""

import json
import re
import sys

CD_PREFIX_START_RE = re.compile(r"^\s*cd\s+\S", re.IGNORECASE)
PY_DASH_C_RE = re.compile(r"\bpython\s+-c\b", re.IGNORECASE)
PY_HEREDOC_RE = re.compile(r"\bpython\s+-\s*<<", re.IGNORECASE)
PRINTF_ECHO_RE = re.compile(r"\b(printf|echo)\b", re.IGNORECASE)

MSG_CD_PREFIX = "don't prefix cd, invoke from the repo root (command hygiene point 3)"
MSG_REDIRECT_STDERR = "don't append 2>&1 (command hygiene point 3)"
MSG_PYTHON_DASH_C = "edits/scripts go through the Edit/Write tool or a named script (command hygiene point 4)"
MSG_JOURNAL_BYPASS = "the journal is written only via Edit/Write (command hygiene point 5)"


def _is_cd_prefix(command: str) -> bool:
    if not CD_PREFIX_START_RE.match(command):
        return False
    return "&&" in command or ";" in command


def _is_python_dash_c(command: str) -> bool:
    return bool(PY_DASH_C_RE.search(command) or PY_HEREDOC_RE.search(command))


def _is_journal_bypass(command: str) -> bool:
    if "routing-log" not in command.lower():
        return False
    has_redirect = ">" in command
    has_printf_echo = bool(PRINTF_ECHO_RE.search(command))
    return has_redirect or has_printf_echo


def decide(payload: dict) -> tuple[int, dict | None]:
    """Pure logic, no I/O -- directly testable. exit_code is ALWAYS 0
    (WARN mode). Returns (0, None) on a silent pass, or (0, dict) where
    dict is ready for json.dumps on stdout when at least one class
    matched."""
    if not isinstance(payload, dict):
        return 0, None

    tool_name = payload.get("tool_name")
    if tool_name not in ("Bash", "PowerShell"):
        return 0, None

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0, None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return 0, None

    triggered = []
    if _is_cd_prefix(command):
        triggered.append(MSG_CD_PREFIX)
    if " 2>&1" in command:
        triggered.append(MSG_REDIRECT_STDERR)
    if _is_python_dash_c(command):
        triggered.append(MSG_PYTHON_DASH_C)
    if _is_journal_bypass(command):
        triggered.append(MSG_JOURNAL_BYPASS)

    if not triggered:
        return 0, None

    context = "Command hygiene (WARN, does not block): " + "; ".join(triggered)
    # permissionDecision is deliberately absent here -- "allow" would
    # auto-approve the very (dirty) command this hook flags, silencing
    # the operator's own permission prompt; additionalContext still
    # reaches the model without it, and the permission decision itself
    # stays on the normal path.
    return 0, {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }


def _reconfigure_stdout_utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    _reconfigure_stdout_utf8()

    # Byte-safe stdin read: sys.stdin.buffer.read() bypasses the
    # platform text-mode encoding of sys.stdin, with an explicit
    # utf-8 decode (errors="replace") that fails open on bad bytes.
    raw_bytes = sys.stdin.buffer.read()
    raw = raw_bytes.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except Exception:
        return 0

    exit_code, output = decide(payload)
    if output is not None:
        sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
