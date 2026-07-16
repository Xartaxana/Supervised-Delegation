"""critic_snapshot.py -- PreToolUse hook that records a tree snapshot
every time a critic dispatch fires, so acceptance can tell whether the
final state of the repo was actually reviewed (rule 3: critic is a
mandatory acceptance gate). The hook itself never blocks anything --
it only writes a fact; the accepting session (or a grader) is the one
that judges the fact's meaning (code guarantees the check gets
encountered, a tier above judges what it means).

On every Task/Agent dispatch where tool_input["subagent_type"] ==
"critic", this hook computes a hash of the whole working tree and
writes it to .claude/critic_snapshot.json as
{"ts": ISO, "tree_hash": str, "files_count": int}, overwriting any
previous snapshot. Comparing the LATEST snapshot against the tree at
the end of a session tells you whether anything changed AFTER the
last critic dispatch -- a mismatch means "the final state was not
reviewed", a fact worth surfacing at acceptance, not a block by
itself.

tree_hash is sha256 over the sorted list of "{rel_path}:{sha256}" for
every file in the tree, excluding .claude/.git/__pycache__/
.pytest_cache (by directory name, at any depth) and
logs/routing-log.jsonl (the routing log changes on essentially every
turn and would make the hash useless as a "did the reviewed code
change" signal).

Known limitation, documented rather than solved here: if a
coordinator keeps talking to an ALREADY-dispatched critic agent
through a continuation/follow-up channel (not a new Task/Agent call),
this hook does not fire again -- its matcher is registered only on
Task/Agent tool calls. A critic that re-reviews a diff several times
within one continued conversation, without a new dispatch, will look
to a grader like "the snapshot is stale" even though the re-review
genuinely happened. This is a limitation of the snapshot as a
measuring instrument, not evidence that no review took place; whether
to widen the hook's matcher to also catch continuation calls is a
judgment call for whoever owns this deployment's hook configuration,
not something this file decides for you.

Fail-open: on unparseable stdin, or any error while computing/writing
the snapshot, the hook exits 0 without side effects -- it must never
block a dispatch just because its own bookkeeping failed.
"""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

EXCLUDED_DIR_NAMES = {".claude", ".git", "__pycache__", ".pytest_cache"}
EXCLUDED_REL_FILES = {Path("logs") / "routing-log.jsonl"}
SNAPSHOT_REL_PATH = Path(".claude") / "critic_snapshot.json"


def compute_tree_hash(root: Path) -> tuple[str, int]:
    entries = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        if rel in EXCLUDED_REL_FILES:
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        entries.append(f"{rel.as_posix()}:{digest}")
    tree = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return tree, len(entries)


def main() -> int:
    # Raw-byte stdin read, decoded explicitly as UTF-8 -- see
    # dispatch_gate.py's main() for why this matters on platforms
    # whose locale encoding isn't UTF-8.
    raw_bytes = sys.stdin.buffer.read()
    raw = raw_bytes.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except Exception:
        return 0  # fail open: not our format, don't get in the way

    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if tool_name not in ("Task", "Agent"):
        return 0
    if tool_input.get("subagent_type") != "critic":
        return 0

    cwd = Path(payload.get("cwd") or ".")
    try:
        tree_hash, files_count = compute_tree_hash(cwd)
        snap = cwd / SNAPSHOT_REL_PATH
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(
            json.dumps(
                {
                    "ts": datetime.now().isoformat(),
                    "tree_hash": tree_hash,
                    "files_count": files_count,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        return 0  # the snapshot is a measuring instrument, not a gate
    return 0


if __name__ == "__main__":
    sys.exit(main())
