"""dispatch_gate.py -- PreToolUse hook for the Task/Agent tool that
checks the SHAPE of a dispatch before it goes out, enforcing two
CLAUDE.md rules in code rather than relying on discipline alone:
rule 11 (DoD-in-every-dispatch / dispatch-context-manifest) and rule 7
(the dispatch label starts with the worker's tier).

Contract (PreToolUse hook stdin JSON): {"tool_name": str,
"tool_input": {"subagent_type": str, "prompt": str,
"description": str}, "cwd": str, ...}. Only tool_name in
{"Task", "Agent"} is inspected -- any other tool passes silently
(exit 0, no output). Exit 2 with a message on stderr blocks the call;
exit 0 allows it. The BLOCKING gate is stateless: it never reads or
writes any file, it only inspects the payload it was given (the
given-path WARN layer added below is the one exception -- it reads the
filesystem to check path existence, but never writes anything and
never changes the exit code).

Cross-reference (a live example of a two-condition detector, see
critic.md's scoping-of-paired-conditions review norm): check 2 below
requires BOTH a given-marker and an owns-marker present in the SAME
prompt to count as a real manifest -- verifying each half in isolation
(a given-marker anywhere, an owns-marker anywhere, even from unrelated
mentions) would be exactly the false cross-match that norm warns
against.

Checks (blocking gate):
 1. subagent_type == "builder": tool_input["prompt"] must contain a
    DoD marker (DOD_MARKERS_RE). None found -> BLOCK
    (BLOCK_MESSAGE_NO_DOD).
 2. subagent_type == "builder" AND the prompt shows a write indicator
    (WRITE_INDICATORS_RE) -- a conservative heuristic: block ONLY
    when a write indicator is present AND BOTH manifest markers
    (MANIFEST_GIVEN_RE and MANIFEST_OWNS_RE) are missing -> BLOCK
    (BLOCK_MESSAGE_NO_MANIFEST). No write indicator -> check 2 is
    skipped entirely (a read-only dispatch needs no manifest).
 3. ANY subagent_type (including critic/scout): tool_input["description"],
    IF PRESENT, must start with a leading token followed by a
    separator ([ :-]) -- LABEL_MODEL_PREFIX_RE below. This is a FORM
    check only, on purpose: this template has no fixed list of tier/
    model names (a deployment configures its own bindings in
    delegation.config.yaml), so the hook can only verify that the
    label carries *some* leading tag-like token, not that the token
    names a real tier. description absent from the payload -> check 3
    is skipped.
 4. critic/scout (any subagent_type != "builder") -- checks 1 and 2 do
    not apply to them; their own DoD shape is different (rule 11
    describes it in prose, not as a prompt-text pattern).

Priority when several checks fail at once: 1 -> 2 -> 3, the first one
found is the single stderr message (the hook blocks with one message,
not a list).

Fail-open on a payload that isn't valid JSON -- same principle as
every other hook in this file set: a hook that can't parse its input
must not block an unrelated tool call.

Marker word-boundary hardening: DOD_MARKERS_RE's "DoD"/"witness"
alternatives, and MANIFEST_GIVEN_RE/MANIFEST_OWNS_RE, are \\b-bounded
so a marker only matches as a standalone word, not as a substring
inside an unrelated longer token -- three concrete classes this
closes: a bare filename mentioning "dod" in the given basket (e.g.
"tools/dod_gate.py") no longer counts as a DoD marker; a filename
mentioning "witness" (e.g. "test_witness_echo.py") no longer counts as
a witness marker; an ordinary longer Cyrillic word that merely
CONTAINS the short Cyrillic given-marker root as a substring no longer
counts as a given-marker (same class as the write-indicator fix
below), and a filename merely containing "owns" as a substring no
longer counts as an owns-marker. The multi-word Cyrillic/English
alternatives (an acceptance-criteria phrase, a verification-run
phrase) are not bounded the same way: they are phrases with an
internal space, and no realistic filename/token in this repo's basket
contains that exact sequence of words as a false-positive substring,
so adding \\b there would be an unmotivated widening of the class the
substring bugs above actually belong to.

--- Given-path WARN layer -------------------------------------------
A NEW, independent layer on top of the blocking gate above: exit-2
branches of decide() (checks 1/2/3) are NOT touched by this layer.
given_path_warn() is a SEPARATE function; decide() never calls it and
its result never participates in the exit code. main() calls it ONLY
when decide() has already returned (0, "") -- if the gate blocks for
another reason, we don't go further (no WARN is printed in that case,
by design: the blocking message already tells the dispatcher what to
fix). The result is ONLY an additionalContext JSON on stdout; main()'s
exit code stays 0 on this branch.

Extraction (extract_given_candidates -> GIVEN_ABS_WIN_PATH_RE /
GIVEN_REPO_REL_PATH_RE): two kinds of local paths --
 (a) an absolute Windows path: `[A-Za-z]:[\\\\/]...\\.ext` -- the path
     BODY (_GIVEN_PATH_BODY_CHAR) excludes whitespace/quotes/angle
     brackets/pipe and, DELIBERATELY, placeholder characters
     `<>*{}$` -- any placeholder (`<name>`, `*.py`, `{name}`, `$VAR`)
     breaks the match before the mandatory `\\.ext`, so such forms are
     simply never extracted (no separate exclusion filter is needed);
     comma/semicolon/newline are excluded from the body too -- an
     engineering choice against a match greedily crossing over
     "path1.py,path2.py" (no space) into one false single match.
 (b) a repo-relative path: ONLY with one of a small set of known
     top-level prefixes (tools|gateway|PROCESS|docs|.claude|.githooks)
     and a file extension -- bare directories/names don't match
     structurally (the regex requires a trailing `\\.ext`). A negative
     lookbehind before the prefix closes two things at once: (1) don't
     match the prefix when it's part of a larger word, (2) don't match
     it as a SUBSTRING inside an already-extracted absolute path (e.g.
     "D:/repo/tools/x.py" -- the character right before "tools" there
     is "/", the lookbehind excludes it -- the absolute and relative
     forms of the same file are never double-counted).

Known root and foreign trees: an absolute path candidate is checked
ONLY if it lies INSIDE the CURRENT dispatch's own cwd (payload["cwd"],
the same reference point this repo's other sidecar checks already
use) -- `_is_under_root`, a normcase+normpath comparison. An absolute
path OUTSIDE that root (any other drive/tree) is not checked at all,
neither warned about nor treated as an error: a dispatch can
legitimately mention absolute paths belonging to a different
deployment or a sibling repo, and this layer has no way to tell
whether those exist without walking a filesystem it has no business
walking.

Noise threshold (GIVEN_PATH_WARN_SUMMARY_THRESHOLD = 10,
format_given_path_warn): <= 10 missing paths -> the full list; > 10 ->
a summary ("N paths do not exist, first 3: ..."); both branches print
a WARN, only the FORM differs -- a silent no-op above the threshold
would make the summary-format text dead code.

Fail-open: given_path_warn() returns "" on any unrecognized payload/
tool_input/prompt; main() additionally wraps the call in try/except
(belt-and-suspenders) -- an adversarial input must not crash the
BLOCKING hook over a WARN-layer failure.
"""

import json
import os
import re
import sys

DOD_MARKERS_RE = re.compile(
    r"\bDoD\b|acceptance criteria|критери[ия] приёмки|\bwitness\b|"
    r"verification run|проверочн\w+ прогон",
    re.IGNORECASE,
)
# \b-bounded so a marker only matches as a whole word -- otherwise a
# short Cyrillic root like "правь" would also match as a substring
# inside unrelated longer words (e.g. "поправь", "исправь").
WRITE_INDICATORS_RE = re.compile(
    r"\bowns\b|\bwrite file\b|\bcreate file\b|\bedit file\b|\bmodify file\b|"
    r"\bзапиши\b|\bсоздай файл\b|\bправь\b|\bизмени файл\b",
    re.IGNORECASE,
)
# \b-bounded: an ordinary, longer Cyrillic word that merely CONTAINS
# the short given-marker root as a substring (e.g. an unrelated word
# meaning "sold out") must not count as a given-marker, same class as
# the write-indicator fix above.
MANIFEST_GIVEN_RE = re.compile(r"\bgiven\b|\bдано\b", re.IGNORECASE)
# \b-bounded: a filename merely containing "owns" as a substring in the
# given basket must not count as an owns-marker.
MANIFEST_OWNS_RE = re.compile(r"\bowns\b", re.IGNORECASE)
# Portable form check (see module docstring, check 3): a leading
# non-whitespace token followed by a separator. Deliberately NOT a
# fixed list of model/tier names -- this template doesn't know a
# deployment's actual bindings.
LABEL_MODEL_PREFIX_RE = re.compile(r"^\S+[ :-]")

BLOCK_MESSAGE_NO_DOD = (
    "A builder dispatch with no DoD does not go out (rule 11): add "
    "acceptance criteria and a verification run whose output becomes "
    "the witness."
)
BLOCK_MESSAGE_NO_MANIFEST = (
    "A writing dispatch with no context manifest (given/owns) does not "
    "go out (rule 11, dispatch-context-manifest rule)."
)
BLOCK_MESSAGE_NO_LABEL = (
    "The dispatch label starts with the worker's tier (rule 7): "
    "e.g. 'sonnet: ...'."
)


def decide(payload: dict) -> tuple[int, str]:
    """Pure decision logic, no I/O -- directly testable. Returns
    (exit_code, stderr_message); "" means "write nothing to stderr"."""
    tool_name = payload.get("tool_name")
    if tool_name not in ("Task", "Agent"):
        return 0, ""

    tool_input = payload.get("tool_input") or {}
    subagent_type = tool_input.get("subagent_type")
    prompt = tool_input.get("prompt") or ""
    description = tool_input.get("description")

    if subagent_type == "builder":
        if not DOD_MARKERS_RE.search(prompt):
            return 2, BLOCK_MESSAGE_NO_DOD

        if WRITE_INDICATORS_RE.search(prompt):
            has_manifest = bool(MANIFEST_GIVEN_RE.search(prompt)) and bool(
                MANIFEST_OWNS_RE.search(prompt)
            )
            if not has_manifest:
                return 2, BLOCK_MESSAGE_NO_MANIFEST

    if description is not None:
        if not LABEL_MODEL_PREFIX_RE.search(description):
            return 2, BLOCK_MESSAGE_NO_LABEL

    return 0, ""


# --- Given-path WARN layer ------------------------------------------------
# See the module docstring, "Given-path WARN layer", for the full
# design rationale. This layer does NOT participate in decide() and
# does not change the exit code -- a separate function, called ONLY
# from main(), ONLY when decide() has already returned (0, "").

# Characters excluded from the "body" of a path candidate: whitespace/
# quotes/angle brackets/pipe, placeholder characters `<>*{}$` (see the
# module docstring, "Extraction") and comma/semicolon/newline (list
# separators -- an engineering choice against greedily crossing over a
# no-space list like "path1.py,path2.py" into one false match).
_GIVEN_PATH_BODY_CHAR = r'[^\s"\'<>|?*{}$,;\n]'

# Critic finding: the greedy `*` on the path body made BOTH regexes
# QUADRATIC on long strings with no trailing dot-extension (measured:
# 9.8s on an 80KB pathological prompt, 89.5s on 240KB -- "C:/"*20000 +
# "a"*20000 -- a hang, not an exception, so the try/except around
# given_path_warn()/main() does not catch it). FIX: the body is bounded
# to {0,300} instead of `*` -- a linear upper bound on backtracking
# relative to the body length, not quadratic in the whole prompt's
# length. CONSEQUENCE (documented, not a bug): a path whose body is
# longer than 300 characters before the extension is simply NOT
# EXTRACTED by this regex (a truncation, not a partial match) -- no
# GIVEN-PATH WARN is promised for such a path; 300 is chosen with a
# generous margin over any realistic path length in this repo.
GIVEN_ABS_WIN_PATH_RE = re.compile(
    r"(?<!\w)[A-Za-z]:[\\/]" + _GIVEN_PATH_BODY_CHAR + r"{0,300}\.[A-Za-z0-9]{1,10}\b"
)

_GIVEN_REPO_REL_PREFIX = r"(?:tools|gateway|PROCESS|docs|\.claude|\.githooks)"
GIVEN_REPO_REL_PATH_RE = re.compile(
    r"(?<![\w/\\])"
    + _GIVEN_REPO_REL_PREFIX
    + r"/"
    + _GIVEN_PATH_BODY_CHAR
    + r"{0,300}\.[A-Za-z0-9]{1,10}\b"
)

GIVEN_PATH_WARN_SUMMARY_THRESHOLD = 10


def extract_given_candidates(prompt: str) -> list:
    """Returns [(path_as_written, is_absolute), ...] -- deduplicated,
    order of first appearance. See the module docstring, "Extraction"."""
    if not isinstance(prompt, str) or not prompt:
        return []
    seen_set = set()
    candidates = []
    for m in GIVEN_ABS_WIN_PATH_RE.finditer(prompt):
        tok = m.group(0)
        if tok not in seen_set:
            seen_set.add(tok)
            candidates.append((tok, True))
    for m in GIVEN_REPO_REL_PATH_RE.finditer(prompt):
        tok = m.group(0)
        if tok not in seen_set:
            seen_set.add(tok)
            candidates.append((tok, False))
    return candidates


def _is_under_root(path_str: str, root: str) -> bool:
    """True when path_str lies inside root (root itself included) --
    compared via normcase(normpath(...)) (case-insensitive on Windows,
    separators normalized). See the module docstring, "Known root and
    foreign trees"."""
    try:
        norm_path = os.path.normcase(os.path.normpath(path_str))
        norm_root = os.path.normcase(os.path.normpath(root))
    except Exception:
        return False
    return norm_path == norm_root or norm_path.startswith(norm_root + os.sep)


def find_missing_given_paths(prompt: str, repo_root: str) -> list:
    """Returns the paths (as written) from extract_given_candidates(prompt)
    that do NOT exist -- an absolute path OUTSIDE repo_root (a foreign
    tree) is skipped entirely, never counted as "missing" (see the
    module docstring, "Known root and foreign trees")."""
    missing = []
    for tok, is_abs in extract_given_candidates(prompt):
        if is_abs:
            if not _is_under_root(tok, repo_root):
                continue
            exists = os.path.exists(tok)
        else:
            exists = os.path.exists(os.path.join(repo_root, tok))
        if not exists:
            missing.append(tok)
    return missing


def format_given_path_warn(missing: list) -> str:
    """"" on an empty list; otherwise the full form (<=10) or a
    summary (>10) -- see the module docstring, "Noise threshold"."""
    if not missing:
        return ""
    if len(missing) <= GIVEN_PATH_WARN_SUMMARY_THRESHOLD:
        listed = ", ".join(missing)
        return (
            "GIVEN-PATH WARN: the dispatch text names paths that do not "
            f"exist: {listed} -- check the spec's facts against their carrier."
        )
    head = ", ".join(missing[:3])
    return (
        f"GIVEN-PATH WARN: {len(missing)} paths do not exist, first 3: "
        f"{head} -- check the spec's facts against their carrier."
    )


def given_path_warn(payload: dict) -> str:
    """"" -- nothing to warn about (payload isn't Task/Agent, no
    prompt, every candidate exists/is foreign/there are none). Otherwise
    the ready-made WARN text (see format_given_path_warn)."""
    if not isinstance(payload, dict):
        return ""
    tool_name = payload.get("tool_name")
    if tool_name not in ("Task", "Agent"):
        return ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return ""

    repo_root = payload.get("cwd")
    if not isinstance(repo_root, str) or not repo_root:
        repo_root = os.getcwd()

    missing = find_missing_given_paths(prompt, repo_root)
    return format_given_path_warn(missing)


def _reconfigure_stderr_utf8():
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _reconfigure_stdout_utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    _reconfigure_stderr_utf8()

    # Read stdin as raw bytes and decode explicitly as UTF-8 rather
    # than through the text-mode sys.stdin.read(): the latter decodes
    # with the platform's locale encoding, which on some systems (e.g.
    # Windows with a non-UTF-8 code page) is NOT UTF-8 and would
    # mangle any non-ASCII payload before the regexes above ever see
    # it. errors="replace" keeps this fail-open on malformed bytes.
    raw_bytes = sys.stdin.buffer.read()
    raw = raw_bytes.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except Exception:
        # Unparseable input -- fail open, same principle as every
        # other hook in this file set.
        return 0

    exit_code, message = decide(payload)
    if exit_code == 2:
        sys.stderr.write(message + "\n")
        return 2

    # Given-path WARN layer: only considered when the gate itself did
    # NOT block (see the module docstring, "Given-path WARN layer");
    # try/except is belt-and-suspenders -- this layer must never crash
    # the blocking hook with a traceback.
    try:
        warn = given_path_warn(payload)
    except Exception:
        warn = ""
    if warn:
        _reconfigure_stdout_utf8()
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": warn,
            }
        }
        sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
