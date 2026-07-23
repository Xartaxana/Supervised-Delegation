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

DETECTION CLASSES (all checks are INDEPENDENT; additionalContext lists
ALL that matched, not just the first):

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
     printf/echo token).

     v2 (ported from HQ 2026-07-21) -- two independent maskings applied
     BEFORE evaluating class (d)'s condition above, closing a live
     git-related false-positive class (a `git add`/`commit`/`push`
     chain whose staged path or commit-message text happens to mention
     "routing-log" -- git itself writes nothing to the journal there):

     (1) _strip_commit_messages -- cuts the -m/--message argument text
         of a `git commit` invocation before class (d) is evaluated
         (all quoting forms: `-m "..."`, `-m '...'`, `--message="..."`,
         `--message='...'`, and the two PowerShell here-string forms
         `-m @'...'@` / `-m @"..."@`). Closes the sub-class "the
         journal path/substring sits INSIDE the commit-message text".

     (2) _mask_git_statements -- masks (replaces with a single space) a
         statement that starts with `git ` followed by one of
         add/commit/push/diff/log/show/status (either at the start of
         the command or right after a chain separator `;`/`&`/`|`/
         newline), before class (d) is evaluated. Closes the wider
         sub-class where there is no commit/-m at all -- e.g.
         `git diff logs/routing-log.jsonl > /tmp/out.txt`, where the
         journal path is a `git diff` ARGUMENT and the `>` redirects
         git's OWN output to an unrelated file, not the journal.
         Message-stripping alone cannot help there (there is no
         message). Order: (1) runs first (a commit message may itself
         contain `;`/`&`/`|`, which would break a naive statement split
         in (2) if (2) ran first), then (2) runs on the
         already-stripped text.

     Known residual gap (accepted, not preemptively closed -- WARN mode
     is not a security boundary): a git-statement for show/diff is
     masked WHOLLY, including any REAL `>` inside it -- so an actual
     journal-write-via-plumbing bypass (`git show
     HEAD:logs/routing-log.jsonl > logs/routing-log.jsonl`, which truly
     overwrites the journal through git plumbing) is also silenced and
     NOT detected. The same masking does not distinguish a
     syntactically broken `git commit` (e.g. an unclosed quote in -m)
     from a valid one -- both are masked alike. Tightening this is
     deferred to evidence of a real leak of this shape, not done
     preemptively. Also not ported: AO3's PowerShell write-token set
     (Add-Content/Set-Content/Out-File) -- this detector's class (d)
     does not know PowerShell write cmdlets at all (out of scope for
     this port; PRINTF_ECHO_RE also does not know sed/tee/awk -- both
     pre-existing gaps, unchanged by this port).

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

# --- v2 -- port (1): strip -m/--message of git commit ------------------
# All supported forms of the -m/--message value; DOTALL is needed only
# by the branches with `.` (the here-string forms) -- the plain-quote
# branches already match newlines via their negated char class.
GIT_COMMIT_RE = re.compile(r"\bgit\s+commit\b", re.IGNORECASE)

COMMIT_MESSAGE_ARG_RE = re.compile(
    r"-m\s+\"(?:[^\"\\]|\\.)*\""
    r"|-m\s+'[^']*'"
    r"|--message=\"(?:[^\"\\]|\\.)*\""
    r"|--message='[^']*'"
    r"|-m\s+@'.*?'@"
    r"|-m\s+@\".*?\"@",
    re.DOTALL,
)

# --- v2 -- port (2): mask a git statement --------------------------------
# A statement starting with `git ` plus one of the listed subcommands
# (at the start of the command, or right after a chain separator
# `;`/`&`/`|`/newline). Group 1 is the separator itself (or an empty
# string at the start) -- kept UNTOUCHED in the replacement so adjacent
# statements are not glued together; group 2 (the statement body up to
# the next separator) is replaced with a single space. A simple negated
# char class `[^;&|\n]*` with no nested quantifiers -- linear in the
# length of the command, same hygiene as the other regexes in this
# file.
GIT_STATEMENT_RE = re.compile(
    r"(^|[;&|\n])(\s*git\s+(?:add|commit|push|diff|log|show|status)\b[^;&|\n]*)",
    re.IGNORECASE,
)

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


def _strip_commit_messages(command: str) -> str:
    """v2 port (1) -- strips the -m/--message argument of a `git commit`
    invocation before classes (a)/(b)/(d) are evaluated: commit-message
    TEXT (journal paths/substrings in prose, ASCII arrows containing
    `>`) must not trigger detection on its own. Only applied when the
    command contains `git commit`; the git add/commit paths themselves
    are untouched -- only the message argument is stripped. An unclosed
    quote does not match and is left as-is (fail-safe toward detection,
    see the class (d) discussion in the module docstring)."""
    if not GIT_COMMIT_RE.search(command):
        return command
    return COMMIT_MESSAGE_ARG_RE.sub(" ", command)


def _mask_git_statements(command: str) -> str:
    """v2 port (2) -- masks `git add/commit/push/diff/log/show/status
    ...` statements (git is not a journal writer) before class (d) is
    evaluated; see the module docstring for ordering relative to
    _strip_commit_messages and the known residual gap (show/diff with a
    redirect that REALLY overwrites the journal via git plumbing --
    accepted, not preemptively closed)."""
    return GIT_STATEMENT_RE.sub(lambda m: m.group(1) + " ", command)


def _is_journal_bypass(command: str) -> bool:
    scrubbed = _mask_git_statements(_strip_commit_messages(command))
    if "routing-log" not in scrubbed.lower():
        return False
    has_redirect = ">" in scrubbed
    has_printf_echo = bool(PRINTF_ECHO_RE.search(scrubbed))
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
