"""hygiene_gate.py -- PreToolUse hook for command hygiene, for the
Bash|PowerShell tools. Mechanizes CLAUDE.md's "Command hygiene" points
3-5: a `cd` prefix, a trailing ` 2>&1`, a `python -c`/`python - <<`
edit bypassing Edit/Write, and a journal write bypassing Edit/Write --
catches them BEFORE the command runs.

Ported from HQ 2026-07-20 (v2 delta 2026-07-21, v3 delta 2026-07-23).

DELIVERY CHANNEL (verified empirically against the installed harness
binary, not assumed from memory): the hook's response is delivered via
`hookSpecificOutput` on stdout, exit 0:

  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                           "additionalContext": "<list of matched classes>"}}

`permissionDecision` is deliberately OMITTED on the WARN classes below:
an earlier draft set it to "allow" for every match, which would have
auto-approved the very command this hook flags, silencing the
operator's own permission prompt -- a review finding on that draft.
Leaving it out on WARN classes delivers the warning without touching
the permission path at all. Class (d) (journal bypass, see v3 below)
is the one exception: it is severe enough to warrant an actual block,
via `permissionDecision: "deny"`.

DETECTION CLASSES (checks (a)-(c) are WARN and INDEPENDENT of each
other; additionalContext lists ALL that matched, not just the first):

 (a) cd-prefix: the command starts with `cd <non-empty argument>` (a
     real path, not a bare "cd" and not "cd&&...") AND somewhere later
     there is `&&` or `;`.
 (b) the literal substring ` 2>&1`.
 (c) `python -c` or `python - <<` -- literally "python" (not "python3":
     deliberately not generalized beyond what command hygiene names),
     with \\b word boundaries so "mypython -c" does not match as a
     substring.
 (d) a journal write bypassing Edit/Write (v3: BLOCK, see below).

All classes are case-insensitive (uniform choice; hygiene points don't
call out per-class case sensitivity).

ADVERSARIAL SAFETY ON LARGE INPUT: every check is a substring test
(`in`, O(n)) or a simple \\b-anchored regex with no nested
quantifiers (no `.*...*` chains that could cause catastrophic
backtracking) -- linear in the length of the command.

Fail-open: a non-Bash/PowerShell tool, empty/malformed stdin, a
non-dict payload, or a missing/non-string/empty command all fall
through silently, with no stdout side effect. The hook never returns a
non-zero exit code on any input -- even the v3 BLOCK below signals
through the JSON body, not the process exit code.

v2 (ported from HQ 2026-07-21) -- git-statement/commit-message false
positives of class (d)
====================================================================

Two independent maskings applied BEFORE evaluating class (d)'s target/
form condition, closing a git-related false-positive class (a
`git add`/`commit`/`push` chain whose staged path or commit-message
text happens to mention the journal path -- git itself writes nothing
to the journal there):

 (1) _strip_commit_messages -- cuts the -m/--message argument text of
     a `git commit` invocation before class (d) is evaluated (all
     quoting forms: `-m "..."`, `-m '...'`, `--message="..."`,
     `--message='...'`, and the two PowerShell here-string forms
     `-m @'...'@` / `-m @"..."@`). Closes the sub-class "the journal
     path/substring sits INSIDE the commit-message text".

 (2) _mask_git_statements -- masks (replaces with a single space) a
     statement that starts with `git ` followed by one of
     add/commit/push/diff/log/show/status (either at the start of the
     command or right after a chain separator `;`/`&`/`|`/newline),
     before class (d) is evaluated. Closes the wider sub-class where
     there is no commit/-m at all -- e.g. `git diff <journal-path> >
     /tmp/out.txt`, where the journal path is a `git diff` ARGUMENT
     and the `>` redirects git's OWN output to an unrelated file, not
     the journal. Order: (1) runs first (a commit message may itself
     contain `;`/`&`/`|`, which would break a naive statement split in
     (2) if (2) ran first), then (2) runs on the already-stripped
     text.

     v3 addendum: both GIT_COMMIT_RE and GIT_STATEMENT_RE now accept
     0+ repetitions of the `-C <dir>` global option between `git` and
     the subcommand (`git -C <dir> add ...`) -- the literal `-C`
     prefix on each repetition keeps the match unambiguous (no
     catastrophic backtracking).

Known residual gap (accepted, not preemptively closed): a
git-statement for show/diff is masked WHOLLY, including any REAL `>`
inside it -- so an actual journal-write-via-plumbing bypass (`git show
HEAD:<journal-path> > <journal-path>`) is also silenced and NOT
detected. The same masking does not distinguish a syntactically broken
`git commit` (e.g. an unclosed quote in -m) from a valid one -- both
are masked alike. Tightening this is deferred to evidence of a real
leak of this shape, not done preemptively. Not ported: PowerShell
write-token set (Add-Content/Set-Content/Out-File) and sed/tee/awk
generically (v3 below closes sed -i and tee specifically; awk remains
a known sibling gap, out of scope for this port).

v3 (ported from HQ 2026-07-23) -- class (d) promoted WARN -> BLOCK
====================================================================

Rationale: WARN mode conditions the operator to ignore warnings; class
(d) (shell write to the journal, bypassing Edit/Write) is the most
dangerous of the four -- it breaks the journal's append-only guarantee
and evades journal_echo/journal_validator in the moment -- so it is
promoted to an actual BLOCK. Classes (a)/(b)/(c) are unchanged, still
pure WARN, still evaluated against the ORIGINAL (unscrubbed) command.

BLOCK MECHANISM: `hookSpecificOutput.permissionDecision = "deny"` (NOT
a non-zero exit code) -- the same forensically-confirmed JSON channel
already used above for `additionalContext`. `main()`/`decide()` still
ALWAYS return exit code 0 -- the block is carried entirely in the JSON
body, keeping one uniform invariant ("this hook never fails the
process") across all four classes.

BELT-AND-SUSPENDERS: `additionalContext` ALWAYS duplicates the block
reason (the same string as `permissionDecisionReason`) on a class-(d)
match, even when no other WARN class fired. Rationale: there is no
live precedent in this kit of the harness actually enforcing
`permissionDecision: "deny"` (the one live blocking gate,
`dispatch_gate.py`, blocks via exit code 2 + stderr, an entirely
different channel) -- if `deny` turns out to be inert on a given
harness build, the class would otherwise degrade from a WARN into
total silence. Duplicating the reason into `additionalContext` means a
dead deny-channel degrades back into a visible warning, not silence.
When other WARN classes (a)/(b)/(c) fire on the same call, their text
is appended alongside the block reason -- neither overwrites the
other.

TARGET WIDENED: class (d)'s target used to be the literal substring
"routing-log"; it now also matches any `logs/*.jsonl` path
(`JOURNAL_JSONL_UNDER_LOGS_RE`) -- covers sibling log/journal files
under the same directory, not just the routing journal by name. Either
condition alone is sufficient (`_has_journal_target`).

WRITE FORMS WIDENED: beyond redirect (`>`/`>>`) and printf/echo, class
(d) now also recognizes `sed -i` (in-place edit -- `SED_INPLACE_RE`,
a space-bounded `-i` so it doesn't match `-i` inside `--ignore-*`),
`tee` (`TEE_RE`), and Python `open(path, 'w'/'a'/'x')`
(`OPEN_WRITE_MODE_RE` -- the mode literal right after the comma inside
`open(...)`, a negative char class `[^)]*` with no nested
quantifiers, linear, stops at the first `)`). A heredoc feeding a
redirect (`cat <<EOF >> <journal>`) needs no separate handling -- the
heredoc itself is only a way to supply stdin; the `>>` it may carry is
an ordinary shell redirect already covered by the `>` check.

STATEMENT SCOPING: before this port, `_is_journal_bypass` checked the
WHOLE scrubbed command -- "the target appears SOMEWHERE" AND "a write
form appears SOMEWHERE", not necessarily in the same statement. That
produces a live false positive on a compound call where the target and
an unrelated write form land in different statements (e.g.
`cat <journal>; echo done`, or `cat <journal> | tee /tmp/out.txt` --
neither `echo` nor `tee` there writes to the journal). Now
`_is_journal_bypass` splits the scrubbed command into statements on
`;`/`&`/`|`/newline (`_statements`) and requires target AND write form
in the SAME statement.

QUOTE-AWARE REDIRECT DETECTION: a quoted `>` (e.g. the argument string
of `grep -c ">" <journal>`, a read-only call) is not a shell redirect
and must not count as a write form. `_mask_quoted_segments` blanks out
single/double-quoted segments (the double-quote pattern mirrors
`COMMIT_MESSAGE_ARG_RE`'s already-proven char class) before the `>`
check only -- the other write indicators (printf/echo/sed -i/tee/
open-write-mode) still run against the UNMASKED text. Known
limitation, not addressed here: statement-splitting (`_statements`)
happens BEFORE quote-aware masking, on the un-masked text -- a literal
`;`/`&`/`|` INSIDE quotes (e.g. `echo "a;b" > <journal>`) would be
mis-split into separate statements by that earlier layer; queued as a
known sibling gap.

`-C <dir>` GIT GLOBAL OPTION: `GIT_COMMIT_RE`/`GIT_STATEMENT_RE`
originally required the subcommand immediately after `git\\s+`; a
`git -C <dir> add/commit/...` form (the `-C` global option sitting
between `git` and the subcommand) broke the match, producing a false
WARN/BLOCK on an otherwise-innocent git compound. Fixed by allowing
0+ repetitions of `-C <dir>` between `git` and the subcommand in both
regexes. Known residual gap: `-c <key>=<value>` (git config override,
a different option from `-C`) is not recognized by this fix -- queued,
no live evidence yet of this exact form leaking.

v4 -- heredoc-body scrub for `git commit -F - <<DELIM ... DELIM`
====================================================================

`_strip_commit_messages` above only ever cut a `-m`/`--message`
ARGUMENT; a commit message supplied via a heredoc instead
(`git commit -F - <<EOF\n...\nEOF`, any quoting of the delimiter:
`<<EOF`, `<<'EOF'`, `<<"EOF"`, `<<-EOF`) was left completely
untouched -- prose inside that body (a journal path/substring, an
ASCII arrow containing `>`) could still trip class (d) even though it
is commit-message TEXT, not a real journal write.

SCOPE, deliberately narrow: only the heredoc's BODY (and its closing
delimiter line) is cut -- the opener line up through `<<DELIM` and
anything AFTER the delimiter on that same line are preserved verbatim
(`COMMIT_HEREDOC_RE`'s groups 1+4), so a real trailing write chained
onto the same line via `&&`/`;` (e.g.
`git commit -F - <<EOF\n...\nEOF && echo "{}" >> logs/routing-log.jsonl`)
stays fully visible to class (d) -- only the heredoc BODY between the
opener and the closing delimiter is replaced.

TWO conditions gate the scrub, checked per heredoc match:
 (1) `_is_python_heredoc_opener` -- a heredoc opened by
     `python -...<<...` (class (c)'s own heredoc form, `PY_HEREDOC_RE`)
     is NOT a commit message at all; scrubbing it would hide a real
     journal write happening inside a `python - <<PY` body from the
     existing statement-scoped machinery (`_statements`/
     `_is_journal_bypass`). Checked by inspecting the text immediately
     before the heredoc's own `<<` (a post-match check, not a
     lookbehind: Python's `re` has no variable-length lookbehind for
     `\\s+`).
 (2) `_heredoc_belongs_to_git_commit` -- the heredoc must actually
     belong to a STATEMENT containing `git commit`: the nearest
     preceding chain separator (`;`/`&`/`|`) before the heredoc marks
     where the current statement starts (or the command's own start,
     if there is none); that prefix must match `GIT_COMMIT_RE`. A
     heredoc belonging to some OTHER statement (e.g. a `python -
     <<'PY'` chained after `&&`, unrelated to any `git commit`) is left
     untouched -- its real write stays visible to the existing
     statement-scoped machinery.

Neither condition holds -> the heredoc is left byte-for-byte as-is.
Nested heredocs (one heredoc's body containing another heredoc opener)
are not specially handled by "nearest preceding separator" -- a
documented residual limitation, not fixed here.

RESIDUAL, DOCUMENTED, NOT FIXED BY THIS SCRUB: class (d)'s own
detectors ((a)/(b)/(c) below and the target/write-form check itself)
still run against the RAW, un-scrubbed command for classes (a)-(c) --
this heredoc scrub only feeds into `_strip_commit_messages`, which
class (d) alone consults. A literal ` 2>&1` sitting INSIDE a
git-commit heredoc body still fires the class-(b) WARN (it is
evaluated against the raw command) -- this is not a regression, it is
the same "class (b)/(c) see the raw command" invariant this file
already documents above for the `-m` argument scrub.
"""

import json
import re
import sys

CD_PREFIX_START_RE = re.compile(r"^\s*cd\s+\S", re.IGNORECASE)
PY_DASH_C_RE = re.compile(r"\bpython\s+-c\b", re.IGNORECASE)
PY_HEREDOC_RE = re.compile(r"\bpython\s+-\s*<<", re.IGNORECASE)
PRINTF_ECHO_RE = re.compile(r"\b(printf|echo)\b", re.IGNORECASE)

# --- v3: additional shell-WRITE indicators for class (d) -- sed -i
# (in-place), tee (duplicates stdout into a file argument), python
# open(path, 'a'/'w'/'x') -- all linear (simple \b-regexes / one
# negative char class with no nested quantifiers, same hygiene as the
# rest of the file).
SED_INPLACE_RE = re.compile(r"\bsed\b[^\n]*\s-i(?:\s|$)", re.IGNORECASE)
TEE_RE = re.compile(r"\btee\b", re.IGNORECASE)
OPEN_WRITE_MODE_RE = re.compile(r"open\s*\([^)]*,\s*[\"'][wax]", re.IGNORECASE)

# v3: single/double quotes -- their contents are masked before the `>`
# redirect check, see _mask_quoted_segments. The double-quote branch
# is the same escape-aware char class COMMIT_MESSAGE_ARG_RE already
# uses for the -m value (proven, linear, no nested quantifiers); the
# single-quote branch is a plain `[^']*` (bash has no escaping inside
# '...').
QUOTED_SEGMENT_RE = re.compile(
    r"'[^']*'" r'|"(?:[^"\\]|\\.)*"',
    re.DOTALL,
)

# v3: class (d)'s target widened from the literal "routing-log" to
# ALSO include any `logs/*.jsonl` path -- covers other journal/log
# files under the same directory, not just routing-log.jsonl itself.
# Linear (negative char class, no nested quantifiers).
JOURNAL_JSONL_UNDER_LOGS_RE = re.compile(r"logs/[\w./-]*\.jsonl", re.IGNORECASE)

# --- v2 -- port (1): strip -m/--message of git commit ------------------
# All supported forms of the -m/--message value; DOTALL is needed only
# by the branches with `.` (the here-string forms) -- the plain-quote
# branches already match newlines via their negated char class.
# v3: extended with a `-C <dir>` (0+ repetitions) allowance between
# "git" and "commit" -- see the v3 "-C <dir> git global option"
# section in the module docstring. The literal "-C" before each
# repetition keeps it unambiguous (no catastrophic backtracking).
GIT_COMMIT_RE = re.compile(r"\bgit\b(?:\s+-C\s+\S+)*\s+commit\b", re.IGNORECASE)

COMMIT_MESSAGE_ARG_RE = re.compile(
    r"-m\s+\"(?:[^\"\\]|\\.)*\""
    r"|-m\s+'[^']*'"
    r"|--message=\"(?:[^\"\\]|\\.)*\""
    r"|--message='[^']*'"
    r"|-m\s+@'.*?'@"
    r"|-m\s+@\".*?\"@",
    re.DOTALL,
)

# --- v4 -- heredoc-body scrub for `git commit -F - <<DELIM ... DELIM` --
# See the module docstring, "v4 -- heredoc-body scrub", for the full
# design. Group 1 is the opener up through `<<DELIM` (kept verbatim in
# the replacement); group 2/3 are the optional quote char and the
# delimiter name; group 4 is anything AFTER `<<DELIM` on the SAME line
# (also kept verbatim -- a chained `&& echo ... >> journal` on that
# line stays visible); group 5 is the body plus the closing delimiter
# line (this is what gets cut).
COMMIT_HEREDOC_RE = re.compile(
    r"(<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2)([^\n]*)(\n.*?^\3\s*$)",
    re.DOTALL | re.MULTILINE,
)

# v4: a heredoc opener belonging to class (c) (`python - <<...`) is
# excluded from the commit scrub entirely (see COMMIT_HEREDOC_RE's
# docstring reference above). Checked by a POST-MATCH scan (the text
# immediately before this heredoc match's own "<<"), not a lookbehind
# -- Python's `re` has no variable-length lookbehind for `\s+`.
_PY_HEREDOC_PREFIX_RE = re.compile(r"python\s+-\s*$", re.IGNORECASE)


def _is_python_heredoc_opener(text: str, match) -> bool:
    """v4: True when the literal "<<" of this heredoc match is
    immediately preceded by "python -" (the same form PY_HEREDOC_RE
    matches as a whole) -- such a heredoc is not a commit message; the
    commit scrub must not touch it."""
    pos = match.start(1)  # the position of "<<" itself (group 1 starts there)
    return bool(_PY_HEREDOC_PREFIX_RE.search(text[:pos]))


def _heredoc_belongs_to_git_commit(text: str, match) -> bool:
    """v4: True when this heredoc genuinely belongs to a STATEMENT
    containing `git commit` -- finds the nearest preceding chain
    separator (`;`, `&`, `|`) before the START of the heredoc match;
    the text from there (or from the start of the command, if there is
    no separator) up to the heredoc -- "the current statement so far"
    -- must contain `git commit` (GIT_COMMIT_RE). Does not account for
    heredocs nested inside one another (see the module docstring's
    "v4" section, "residual limitation")."""
    start = match.start()
    prefix = text[:start]
    sep_idx = max(prefix.rfind(";"), prefix.rfind("&"), prefix.rfind("|"))
    statement_prefix = prefix[sep_idx + 1:] if sep_idx != -1 else prefix
    return bool(GIT_COMMIT_RE.search(statement_prefix))

# --- v2 -- port (2): mask a git statement --------------------------------
# A statement starting with `git ` plus one of the listed subcommands
# (at the start of the command, or right after a chain separator
# `;`/`&`/`|`/newline). Group 1 is the separator itself (or an empty
# string at the start) -- kept UNTOUCHED in the replacement so adjacent
# statements are not glued together; group 2 (the statement body up to
# the next separator) is replaced with a single space. A simple negated
# char class `[^;&|\n]*` with no nested quantifiers -- linear in the
# length of the command, same hygiene as the other regexes in this
# file. v3: same `-C <dir>` (0+ repetitions) allowance as GIT_COMMIT_RE
# above.
GIT_STATEMENT_RE = re.compile(
    r"(^|[;&|\n])(\s*git\s+(?:-C\s+\S+\s+)*(?:add|commit|push|diff|log|show|status)\b[^;&|\n]*)",
    re.IGNORECASE,
)

MSG_CD_PREFIX = "don't prefix cd, invoke from the repo root (command hygiene point 3)"
MSG_REDIRECT_STDERR = "don't append 2>&1 (command hygiene point 3)"
MSG_PYTHON_DASH_C = "edits/scripts go through the Edit/Write tool or a named script (command hygiene point 4)"
# v3: class (d) promoted WARN -> BLOCK; this message now goes into
# permissionDecisionReason (and, belt-and-suspenders, additionalContext
# too), not a plain WARN line. Renamed BYPASS -> BLOCK to match the new
# status; all references updated in the test twin.
MSG_JOURNAL_BLOCK = (
    "the journal is written only via Edit/Write (command hygiene point 5); "
    "shell write to the journal blocked"
)


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
    see the class (d) discussion in the module docstring).

    v4: AFTER the -m/--message argument is stripped, ALSO strips a
    commit-message HEREDOC body (`git commit -F - <<EOF ... EOF`, any
    delimiter quoting) via COMMIT_HEREDOC_RE -- see the module
    docstring's "v4" section for the full design: (1) the remainder of
    the opener line AFTER `<<DELIM` is preserved verbatim
    (`_heredoc_sub` keeps groups 1+4), only the body+closing-delimiter
    line (group 5) is cut; (2) a heredoc opened by `python -...<<...`
    (class (c)) is excluded from the scrub (`_is_python_heredoc_opener`);
    (3) the scrub is scoped to a heredoc that genuinely belongs to a
    STATEMENT containing `git commit` (`_heredoc_belongs_to_git_commit`)
    -- a heredoc of some other statement (e.g. `python - <<'PY'` after
    `&&`) is left untouched, its real write staying visible to the
    existing statement-scoped machinery (`_statements`/
    `_is_journal_bypass`)."""
    if not GIT_COMMIT_RE.search(command):
        return command
    stripped = COMMIT_MESSAGE_ARG_RE.sub(" ", command)

    def _heredoc_sub(m):
        if _is_python_heredoc_opener(stripped, m):
            return m.group(0)
        if not _heredoc_belongs_to_git_commit(stripped, m):
            return m.group(0)
        return m.group(1) + m.group(4) + " "

    return COMMIT_HEREDOC_RE.sub(_heredoc_sub, stripped)


def _mask_git_statements(command: str) -> str:
    """v2 port (2) -- masks `git [-C <dir>] add/commit/push/diff/log/
    show/status ...` statements (git is not a journal writer) before
    class (d) is evaluated; see the module docstring for ordering
    relative to _strip_commit_messages and the known residual gap
    (show/diff with a redirect that REALLY overwrites the journal via
    git plumbing -- accepted, not preemptively closed). v3: also
    recognizes `-C <dir>` between "git" and the subcommand."""
    return GIT_STATEMENT_RE.sub(lambda m: m.group(1) + " ", command)


_STATEMENT_SPLIT_RE = re.compile(r"[;&|\n]")


def _statements(scrubbed: str) -> list[str]:
    """v3 -- splits the already-scrubbed (git-masked) command into
    shell statements on the same separators GIT_STATEMENT_RE uses
    (`;`/`&`/`|`/newline), see the "STATEMENT SCOPING" section of the
    module docstring. `&&`/`||` produce an empty element between the
    two separators -- harmless (matches none of the checks below)."""
    return _STATEMENT_SPLIT_RE.split(scrubbed)


def _has_journal_target(text: str) -> bool:
    """v3 -- class (d)'s target, widened: the literal substring
    "routing-log" (case-insensitive, as before) OR any `logs/*.jsonl`
    path (case-insensitive, new -- see the "TARGET WIDENED" section of
    the module docstring)."""
    return "routing-log" in text.lower() or bool(JOURNAL_JSONL_UNDER_LOGS_RE.search(text))


def _mask_quoted_segments(text: str) -> str:
    """v3 -- a quoted `>` (e.g. the argument string of
    `grep -c ">" <journal>`, read-only) is not a shell redirect and
    must not count as a write form. Blanks out single/double-quoted
    segments (the double-quote branch mirrors COMMIT_MESSAGE_ARG_RE's
    already-proven char class) before the redirect check only -- the
    other write indicators (printf/echo/sed -i/tee/open-write-mode)
    still run on the UNMASKED text. An unclosed quote does not match
    and is left unmasked (fail-safe toward detection, same principle
    as the rest of the file)."""
    return QUOTED_SEGMENT_RE.sub(lambda m: " " * len(m.group(0)), text)


def _has_write_form(text: str) -> bool:
    """v3 -- shell WRITE forms: redirect `>`/`>>`, printf/echo (as
    before), + sed -i (in-place), tee, python open(...,'w'/'a'/'x')
    (new, see the module docstring). The redirect `>` check runs on
    text with quotes masked (_mask_quoted_segments) -- a quoted `>`
    (argument data, not a shell redirect) no longer counts."""
    redirect_check_text = _mask_quoted_segments(text)
    return bool(
        ">" in redirect_check_text
        or PRINTF_ECHO_RE.search(text)
        or SED_INPLACE_RE.search(text)
        or TEE_RE.search(text)
        or OPEN_WRITE_MODE_RE.search(text)
    )


def _is_journal_bypass(command: str) -> bool:
    """v3 -- STATEMENT-SCOPED (was: checked the whole scrubbed command
    without regard to separators, see the "STATEMENT SCOPING" section
    of the module docstring). Triggers only when ONE AND THE SAME
    statement carries both the target (_has_journal_target) and a
    write form (_has_write_form)."""
    scrubbed = _mask_git_statements(_strip_commit_messages(command))
    return any(
        _has_journal_target(stmt) and _has_write_form(stmt)
        for stmt in _statements(scrubbed)
    )


def _collect_warn_classes(command: str) -> list[str]:
    """Classes (a)/(b)/(c) -- pure WARN, evaluated on the ORIGINAL
    (unscrubbed) command, unchanged by the v3 port."""
    triggered = []
    if _is_cd_prefix(command):
        triggered.append(MSG_CD_PREFIX)
    if " 2>&1" in command:
        triggered.append(MSG_REDIRECT_STDERR)
    if _is_python_dash_c(command):
        triggered.append(MSG_PYTHON_DASH_C)
    return triggered


def decide(payload: dict) -> tuple[int, dict | None]:
    """Pure logic, no I/O -- directly testable. exit_code is ALWAYS 0,
    including on a v3 class-(d) BLOCK: the block is signalled entirely
    via hookSpecificOutput.permissionDecision="deny" in the JSON on
    stdout, never through the process return code (see the "BLOCK
    MECHANISM" section of the module docstring). Returns (0, None) on
    a silent pass, or (0, dict) where dict is ready for json.dumps on
    stdout when at least one class matched."""
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

    # v3: class (d) -- BLOCK, checked FIRST and independently of the
    # WARN classes (a)/(b)/(c), which stay as they were.
    #
    # BELT-AND-SUSPENDERS (see module docstring): additionalContext
    # ALWAYS duplicates the block reason -- if permissionDecision:
    # "deny" turns out to be inert on a given harness build, the class
    # degrades back into a visible warning instead of total silence.
    # Other independently-triggered WARN classes of the same call are
    # appended alongside (never overwrite the block reason).
    if _is_journal_bypass(command):
        other_warn = _collect_warn_classes(command)
        context_parts = [MSG_JOURNAL_BLOCK] + other_warn
        return 0, {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": MSG_JOURNAL_BLOCK,
                "additionalContext": "Command hygiene: " + "; ".join(context_parts),
            }
        }

    triggered = _collect_warn_classes(command)
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
