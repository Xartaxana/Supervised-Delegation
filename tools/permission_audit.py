"""permission_audit -- reconstruct which Bash/PowerShell commands (including
subagents') LIKELY needed a manual permission prompt, and why.

Ported from HQ 2026-07-20. The heuristic logic (allowlist matching,
auto-allow, sandbox heuristics) is unchanged from the source. Two
refinements, found by an earlier pilot run of this script:

  (a) SNAPSHOT the list of transcripts and their sizes BEFORE scanning --
      a run during a live session keeps appending to the very transcript
      being scanned, so the "Scanned" count would otherwise drift between
      the start and the end of the script. Only the byte prefix fixed at
      snapshot time is read from each file, not whatever has landed there
      by the time it's actually read.
  (b) A MASKED-BY-BROAD-ALLOWLIST block -- both settings files are scanned
      for arbitrary-execution patterns (a bare interpreter / `-c` / `-e`
      before `*`, e.g. `Bash(python *)`) and an explicit warning is
      printed: such rules silently swallow part of the "no allowlist
      match" category below, without ever showing up as a suspect.

There is no direct log of "a permission dialog was shown", so the audit is
heuristic: it takes every tool_use from the current project's transcripts,
runs them through the same rules the harness itself uses (the
settings.json/settings.local.json allowlist + known auto-allow + the
"cannot be statically analyzed" sandbox heuristics), and prints the ones
that would NOT have passed without a prompt -- with a reason category and
a suggested fix.

Usage:  python tools/permission_audit.py [--minutes 120] [--all] [--session ID] [--summary]
  --minutes N  only look at commands from the last N minutes (default 180)
  --all        ignore the time-window filter
  --session S  only transcripts (main + subagents) whose path contains substring S
  --summary    a grouped summary instead of the full list
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _default_project_key(repo: Path) -> str:
    """Derives this deployment's `~/.claude/projects/<slug>` directory name
    from its own absolute repo path, instead of hardcoding one deployment's
    slug -- a hardcoded slug would silently break this script for every
    OTHER install of this toolkit, since the slug is specific to where the
    repo happens to live on disk.

    The harness builds the slug by replacing path separators, the
    drive-letter colon, and underscores with a dash -- verified
    empirically against this machine's own `~/.claude/projects` listing
    (e.g. `D:\\Some_Repo` -> `D--Some-Repo`). Untested against every
    possible path character (dots, spaces); good enough as a default,
    override CLAUDE_PROJECTS directly if your deployment's slug does not
    match this pattern."""
    raw = str(repo.resolve())
    return re.sub(r"[\\/:_]", "-", raw)


PROJECT_KEY = _default_project_key(REPO)
CLAUDE_PROJECTS = Path(os.path.expanduser("~")) / ".claude" / "projects" / PROJECT_KEY

# --- commands the harness auto-allows with no allowlist entry (a practical, trimmed list) ---
AUTO_ALLOW_ANY_ARGS = {
    "cat", "head", "tail", "wc", "stat", "ls", "cd", "echo", "sleep", "which", "diff",
    "true", "false", "seq", "basename", "dirname", "realpath", "cut", "tr", "comm",
    "readlink", "expr", "type", "uname", "df", "du", "nl", "od", "id", "date",
}
AUTO_ALLOW_VALIDATED = {"grep", "rg", "find", "sort", "uniq", "jq", "sed", "ps", "xargs",
                        "file", "tree", "hostname", "pgrep", "lsof", "printf", "man"}
GIT_RO = {"status", "log", "diff", "show", "blame", "branch", "tag", "remote", "ls-files",
          "rev-parse", "describe", "reflog", "shortlog", "cat-file", "for-each-ref",
          "worktree", "stash"}

SANDBOX_HEURISTICS = [
    (re.compile(r'export\s+\w+="[^"]*\$\{?\w+'), "export VAR referencing another variable (array-subscript heuristic)"),
    (re.compile(r"\bnohup\b"), "nohup / manual backgrounding"),
    (re.compile(r"\$\("), "command substitution $(...)"),
    (re.compile(r"\bfor\s+\w+\s+in\b.*\bdo\b", re.S), "a for...do loop in shell"),
    (re.compile(r"\buntil\b|\bwhile\b.*\bdo\b", re.S), "a while/until loop"),
    (re.compile(r"&\s*$", re.M), "background launch via &"),
]

# --- refinement (b): allowlist patterns that amount to near-arbitrary code execution ---
INTERPRETER_HEADS = {
    "python", "python3", "py", "node", "ruby", "perl", "bash", "sh", "zsh",
    "powershell", "pwsh", "osascript", "php",
}
CODE_FLAGS = {"-c", "-e", "--command"}


def _iter_allow_entries():
    """(file_name, tool, pattern) across both settings files, raw allow entries."""
    for name in ("settings.json", "settings.local.json"):
        p = REPO / ".claude" / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] could not read {name}: {e}", file=sys.stderr)
            continue
        for entry in data.get("permissions", {}).get("allow", []):
            m = re.match(r"^(\w+)\((.*)\)$", entry, re.S)
            if m:
                yield name, m.group(1), m.group(2)
            else:
                yield name, entry, ""  # a bare tool name with no pattern, e.g. WebSearch


def load_allow_patterns() -> list[tuple[str, str]]:
    """[(tool, pattern), ...] from settings.json + settings.local.json."""
    return [(tool, pat) for _name, tool, pat in _iter_allow_entries()]


def matches_allow(tool: str, cmd: str, patterns) -> bool:
    for ptool, pat in patterns:
        if ptool != tool:
            continue
        if not pat:
            return True
        if pat.endswith("*"):
            if cmd.startswith(pat[:-1]):
                return True
        elif " *" in pat:  # the "foo *" form -- a prefix up to the asterisk
            if cmd.startswith(pat.split(" *")[0]):
                return True
        elif fnmatch.fnmatch(cmd, pat) or cmd == pat:
            return True
    return False


def is_auto_allowed(cmd: str) -> bool:
    """A rough approximation of the harness's built-in auto-allow (simple
    single-line commands only)."""
    if "\n" in cmd.strip():
        return False
    # a chain -- every part must be auto-allowed
    parts = re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd.strip())
    for part in parts:
        if not part:
            continue
        tokens = part.strip().split()
        if not tokens:
            continue
        head = tokens[0].strip('"')
        base = os.path.basename(head).lower().removesuffix(".exe")
        if base == "git" and len(tokens) > 1 and tokens[1] in GIT_RO:
            continue
        if base in AUTO_ALLOW_ANY_ARGS or base in AUTO_ALLOW_VALIDATED:
            continue
        return False
    return True


def sandbox_flags(cmd: str) -> list[str]:
    flags = [reason for rx, reason in SANDBOX_HEURISTICS if rx.search(cmd)]
    if "\n" in cmd.strip():
        flags.append("a multi-line command (multiple statements in one call)")
    return flags


_ENV_ASSIGN_RE = re.compile(r"^\w+=\S*$")


def is_broad_wildcard(tool: str, pat: str) -> str | None:
    """If pat is an allowlist pattern that lets through arbitrary
    execution (a bare interpreter before `*`, an interpreter with a
    -c/-e flag before `*`, including one with an unclosed opening quote
    right after the flag, optionally behind a VAR=val prefix) -- return
    the reason as a string. Otherwise None. Example findings: Bash(python
    *), Bash(python -c ' *), Bash(PYTHONUTF8=1 python -c ' *)."""
    if tool not in ("Bash", "PowerShell"):
        return None
    p = pat.strip()
    if not p.endswith("*"):
        return None
    prefix = p[:-1].strip()
    tokens = prefix.split()
    while tokens and _ENV_ASSIGN_RE.match(tokens[0]):
        tokens = tokens[1:]  # skip VAR=val ahead of the interpreter name
    if not tokens:
        return None
    head = os.path.basename(tokens[0].strip("\"'")).lower().removesuffix(".exe")
    if head not in INTERPRETER_HEADS:
        return None
    rest = tokens[1:]
    if not rest:
        return f"a bare interpreter with no arguments -- lets arbitrary code through after '{head}'"
    if rest[0] in CODE_FLAGS:
        remainder = "".join(rest[1:]).strip("'\"")
        if not remainder:
            return f"'{head} {rest[0]}' -- arbitrary one-line code passes without a prompt"
    # `<interpreter> -m *` lets through an arbitrary MODULE (python -m
    # http.server, -m pip, ...) -- the same class as -c/-e.
    if rest[0] == "-m" and not "".join(rest[1:]).strip("'\""):
        return f"'{head} -m' -- an arbitrary module passes without a prompt"
    return None


def scan_broad_wildcards() -> list[tuple[str, str, str, str]]:
    """[(settings file, tool, pattern, reason), ...] for broad wildcard
    patterns that silently swallow the "no allowlist match" category
    (refinement b)."""
    out = []
    for fname, tool, pat in _iter_allow_entries():
        reason = is_broad_wildcard(tool, pat)
        if reason:
            out.append((fname, tool, pat, reason))
    return out


def snapshot_transcripts(session: str | None = None) -> list[tuple[Path, str, int]]:
    """[(path, agent_type, size_at_snapshot), ...] -- fix the list of
    transcripts and their sizes BEFORE scanning (refinement a): a run
    during a live session keeps appending to the very transcript being
    scanned, and without a snapshot the "Scanned" count would drift
    between the start and the end of the script. The scan below reads
    only these first size_at_snapshot bytes of each file -- anything
    appended after the snapshot is ignored."""
    files: list[tuple[Path, str]] = []
    for jl in CLAUDE_PROJECTS.glob("*.jsonl"):
        files.append((jl, "main"))
    for sub in CLAUDE_PROJECTS.glob("*/subagents/agent-*.jsonl"):
        if session and session not in str(sub):
            continue
        agent_type = "subagent"
        meta = sub.with_name(sub.name.replace(".jsonl", ".meta.json"))
        if meta.exists():
            try:
                agent_type = json.loads(meta.read_text(encoding="utf-8")).get("agentType", "subagent")
            except Exception:  # noqa: BLE001
                pass
        files.append((sub, agent_type))

    snapshot = []
    for path, source in files:
        if session and source == "main" and session not in path.name:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        snapshot.append((path, source, size))
    return snapshot


def _read_snapshot_lines(path: Path, size: int) -> list[str]:
    """Read the first `size` bytes of the file (fixed by the snapshot)
    and return complete lines; a possibly-truncated last line right at
    the boundary is dropped."""
    try:
        with open(path, "rb") as fb:
            data = fb.read(size)
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    if not text.endswith("\n") and "\n" in text:
        text = text[: text.rfind("\n") + 1]
    elif not text.endswith("\n"):
        text = ""  # the file's only line was cut off right at the snapshot boundary
    return text.splitlines()


def iter_tool_calls(minutes: float | None, session: str | None = None,
                     snapshot: list[tuple[Path, str, int]] | None = None):
    """(when, source, agent_type, tool, command) over the project's
    transcript snapshot."""
    cutoff = None if minutes is None else time.time() - minutes * 60
    if snapshot is None:
        snapshot = snapshot_transcripts(session)

    for path, source, size in snapshot:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if cutoff and mtime < cutoff:
            continue  # the file hasn't changed within the window -- skip it entirely
        for line in _read_snapshot_lines(path, size):
            line = line.strip()
            if not line or '"tool_use"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            ts = obj.get("timestamp")
            when = None
            if ts:
                try:
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except Exception:  # noqa: BLE001
                    pass
            if cutoff and when and when < cutoff:
                continue
            for item in obj.get("message", {}).get("content", []) or []:
                if isinstance(item, dict) and item.get("type") == "tool_use" \
                        and item.get("name") in ("Bash", "PowerShell"):
                    cmd = (item.get("input") or {}).get("command", "")
                    yield when, path.name, source, item["name"], cmd


def collect_suspects(minutes: float | None, session: str | None = None,
                      snapshot: list[tuple[Path, str, int]] | None = None):
    """Run every tool_use through the allowlist + sandbox heuristics.

    Returns (suspects, total), where suspects is a list of
    (when, agent, tool, cmd, reason) for commands that LIKELY needed a
    manual permission prompt. Pulled out of main() as a separate pure
    function so unit tests can check the filtering without parsing
    stdout.
    """
    patterns = load_allow_patterns()
    suspects = []
    total = 0
    for when, fname, agent, tool, cmd in iter_tool_calls(minutes, session, snapshot):
        total += 1
        allowed = matches_allow(tool, cmd, patterns)
        flags = sandbox_flags(cmd)
        if (allowed and not flags) or is_auto_allowed(cmd):
            continue
        reason = []
        if not allowed:
            reason.append("no allowlist match")
        reason += flags
        suspects.append((when, agent, tool, cmd, reason))
    return suspects, total


def main(argv=None):
    if os.name == "nt":  # some Windows console codepages choke on non-ASCII -- force utf-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=180)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--session", help="filter: only transcripts whose path contains this substring (a session id)")
    ap.add_argument("--summary", action="store_true", help="a grouped summary instead of the full list")
    args = ap.parse_args(argv)
    minutes = None if getattr(args, "all") else args.minutes

    # refinement (b): warn about broad allowlist patterns -- before the summary
    broad = scan_broad_wildcards()
    if broad:
        print("MASKED-BY-BROAD-ALLOWLIST:")
        print("  These allowlist rules let through arbitrary code execution and SILENTLY")
        print("  swallow part of the \"no allowlist match\" category below -- commands under")
        print("  them never even reach the suspects list, even though they may in fact be the wrong form:")
        for fname, tool, pat, reason in broad:
            print(f"  - {fname}: {tool}({pat}) -- {reason}")
        print()

    snapshot = snapshot_transcripts(args.session)
    suspects, total = collect_suspects(minutes, args.session, snapshot)

    print(f"Scanned Bash/PowerShell calls: {total}"
          + ("" if minutes is None else f" (in the last {minutes:g} min)")
          + (f" - session *{args.session[:8]}*" if args.session else ""))
    print(f"Likely needed confirmation: {len(suspects)}\n")

    if args.summary:
        from collections import Counter
        by_agent = Counter(a for _, a, *_ in suspects)
        by_reason = Counter(r for *_, reasons in suspects for r in reasons)
        examples: dict[str, str] = {}
        for _, agent, _tool, cmd, reasons in suspects:
            for r in reasons:
                examples.setdefault(r, " ".join(cmd.split())[:110])
        print("By agent:")
        for a, n in by_agent.most_common():
            print(f"  {n:4d}  {a}")
        print("\nBy reason:")
        for r, n in by_reason.most_common():
            print(f"  {n:4d}  {r}")
            print(f"        example: {examples[r]}")
    else:
        for when, agent, tool, cmd, reason in suspects:
            t = datetime.fromtimestamp(when, tz=timezone.utc).strftime("%H:%M:%S") if when else "--:--:--"
            one_line = " ".join(cmd.split())[:150]
            print(f"[{t}] {agent} / {tool}")
            print(f"  cmd: {one_line}")
            print(f"  reason: {'; '.join(reason)}")
            print()
    if suspects:
        print("Recommendations by category:")
        print(" - \"no allowlist match\" -> add a wildcard pattern to .claude/settings.json")
        print(" - \"multi-line/loop/nohup/substitution\" -> the allowlist will NOT help; move the logic")
        print("   into a named function/script under tools/ and forbid the pattern in .claude/agents/*.md")
        print(" - remember: settings.json is only re-read by NEW (sub)agents, not on the fly")


if __name__ == "__main__":
    main()
