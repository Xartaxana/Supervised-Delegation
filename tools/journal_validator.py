"""Routing-journal validator -- a pre-commit gate for
logs/routing-log.jsonl that catches, in code, two failure patterns
that have happened for real: a duplicate task_id issued without
re-reading the log's tail, and an event whose ts was written from
narrative rather than read off the clock (a "narrative-future
timestamp"). Calling convention/structure mirror
tools/mechanism_gate.py (the same home in the enforcement chain): a
pure decide(), testable without git, and a thin main() wrapping it in
git plumbing.

Scope: ONLY the staged version of logs/routing-log.jsonl against the
HEAD version (git show :logs/... vs HEAD:logs/...). If the file isn't
staged, main() silently returns 0 (the gate emits nothing at all).
Only deterministically decidable facts are checked -- presence,
shape, typed fields; the meaning of notes is never parsed.

Checks (numbering matches this file's own spec / CLAUDE.md's "Routing
log" section):
 1. Append-only: staged must start with HEAD as a prefix.
 2. Every NEW line is a valid single-line JSON object with
    ts/event/agent/category/notes (notes non-empty).
 3. event is one of the ENUM values.
 4. model is required for delegated/escalated/accepted/rejected.
 5. task_id is required for delegated/accepted/rejected/escalated/
    defect_found, format t-NNN (3+ digits).
 6. rejected: attempt is an integer >=1; failure_class is one of the
    ENUM values.
 7. accepted with agent=builder: witness is a non-empty string.
 8. defect_found: ref is non-empty.
 9. task_id novelty/reference rules (revised after a live precedent
    showed "strictly max+1" would forbid a legal critic entry into
    acceptance, and a legal retry after rejected). For a NEW
    delegated:
    a) task_id == max(all t-NNN in the file so far)+1 -- always legal
       (a brand-new task);
    b) task_id already exists in the file AND the task is still OPEN
       (no accepted for this task_id earlier in the file) AND the new
       line's agent differs from every earlier delegated agent for
       that task_id -- legal (a continuation dispatch from a
       different tier, e.g. a critic entry into acceptance);
    c) task_id exists, task is open, agent matches one of the earlier
       delegated agents -- legal ONLY with an attempt field (integer
       >=2) AND an earlier rejected for the same task_id (a retry
       after rejection);
    d) anything else -- FAIL (the duplicate-dispatch pattern: same
       agent, no attempt, no rejected; and delegated on a CLOSED task
       -- reopening is forbidden, a collision counts as two tasks).
    For a new accepted/rejected/escalated/defect_found -- it must
    reference a task_id already seen earlier in the file (in HEAD or
    earlier in this same commit); unchanged otherwise.
10. ts of new lines is monotonic relative to HEAD's last line and to
    each other; not later than now+10 minutes (a narrative-future
    timestamp). No lower bound.
11. The role-vs-tier acceptance matrix (NEW lines only): new
    accepted/rejected lines carry a typed "by" field. For agent=lead
    the matrix doesn't apply -- presence of "by" is enough. For agent
    in {scout, builder, critic} ONLY accepted additionally requires
    tier(by) > tier(agent) (haiku<sonnet<opus<fable by agent:
    scout=haiku, builder=sonnet, critic=opus), or a typed "basis"
    field in {"critic", "queued-to-lead"}. Read literally, the spec
    requires the tier/basis check only for accepted, not rejected --
    rejected just carries "by" with no further check.
12. Every NEW delegated line carries worker_ref -- a non-empty handle
    by which the next session finds the worker/result; catches a
    phantom delegated whose worker was never launched.

13. Any FAIL -> exit 1, with the line number, event/task_id, and which
    check failed, for every violating line. A validator crash (an
    exception, not a validation FAIL) -> exit 2 with a traceback
    (fail-closed, same as mechanism_gate; see main()).
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
import sys
import traceback

JOURNAL_PATH = "logs/routing-log.jsonl"

EVENTS = {
    "delegated", "accepted", "rejected", "escalated", "decomposable",
    "dispatch_skipped", "defect_found", "lead_degraded", "lead_restored",
    "journal_created", "calibrated",
}
MODEL_REQUIRED_EVENTS = {"delegated", "escalated", "accepted", "rejected"}
TASK_ID_REQUIRED_EVENTS = {"delegated", "accepted", "rejected", "escalated", "defect_found"}
FAILURE_CLASSES = {"spec", "capability", "recon", "tooling"}
TIER_ORDER = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}
AGENT_TIER = {"scout": "haiku", "builder": "sonnet", "critic": "opus"}
BASIS_VALUES = {"critic", "queued-to-lead"}

TASK_ID_RE = re.compile(r"^t-(\d{3,})$")
# ISO without a timezone: 'YYYY-MM-DDTHH:MM:SS' with optional
# microseconds, NO 'Z'/offset -- a timezone is forbidden by the spec
# (otherwise monotonicity between lines of different offsets loses
# its unambiguous ordering).
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")


def parse_ts(ts: str):
    if not isinstance(ts, str) or not TS_RE.match(ts):
        return None
    try:
        return datetime.datetime.fromisoformat(ts)
    except ValueError:
        return None


def split_lines(text: str | None) -> list[str]:
    if not text:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _try_parse_obj(line: str):
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def extract_task_ids(lines: list[str]) -> set[str]:
    """All task_id values (valid t-NNN format) seen in ANY event of
    these lines -- used both as the "already seen earlier" set and as
    the basis for max(...)+1."""
    ids = set()
    for line in lines:
        obj = _try_parse_obj(line)
        if obj is None:
            continue
        tid = obj.get("task_id")
        if isinstance(tid, str) and TASK_ID_RE.match(tid):
            ids.add(tid)
    return ids


def max_task_num(ids: set[str]) -> int:
    nums = [int(TASK_ID_RE.match(i).group(1)) for i in ids]
    return max(nums) if nums else 0


def _harvest_line_into(event, task_id, agent, delegated_agents: dict, closed_tasks: set,
                        rejected_tasks: set) -> None:
    """Rule 9(b/c) state: updates the per-task_id history by ONE line
    (used both to seed state from HEAD and, line by line, for new
    lines -- call order = line order in the file, so the state at the
    time line N is checked reflects exactly "everything earlier in
    the file")."""
    if not (isinstance(task_id, str) and TASK_ID_RE.match(task_id)):
        return
    if event == "delegated" and isinstance(agent, str) and agent:
        delegated_agents.setdefault(task_id, set()).add(agent)
    elif event == "accepted":
        closed_tasks.add(task_id)
    elif event == "rejected":
        rejected_tasks.add(task_id)


def harvest_task_state(lines: list[str]):
    """Seeds rule 9(b/c) state from the HEAD version (or any prefix of
    lines) -- (delegated_agents, closed_tasks, rejected_tasks)."""
    delegated_agents: dict[str, set] = {}
    closed_tasks: set = set()
    rejected_tasks: set = set()
    for line in lines:
        obj = _try_parse_obj(line)
        if obj is None:
            continue
        _harvest_line_into(obj.get("event"), obj.get("task_id"), obj.get("agent"),
                           delegated_agents, closed_tasks, rejected_tasks)
    return delegated_agents, closed_tasks, rejected_tasks


def _last_head_ts(head_lines: list[str]):
    """ts of the LAST line of the HEAD version (rule 10: monotonicity
    is measured from it, not from the max over the file -- the
    journal is append-only, so HEAD's last line is chronologically the
    last of the old ones)."""
    if not head_lines:
        return None
    obj = _try_parse_obj(head_lines[-1])
    if obj is None:
        return None
    return parse_ts(obj.get("ts"))


def check_append_only(staged_lines: list[str], head_lines: list[str]):
    """Rule 1: staged must start with head as a prefix (existing lines
    are neither changed nor removed). Returns (ok, message)."""
    if len(staged_lines) < len(head_lines):
        return False, (
            f"append-only: staged has FEWER lines ({len(staged_lines)}) "
            f"than HEAD ({len(head_lines)}) -- existing lines were removed"
        )
    for i, head_line in enumerate(head_lines):
        if staged_lines[i] != head_line:
            return False, (
                f"append-only: line {i + 1} diverges from HEAD -- "
                "existing lines cannot be changed, only appended"
            )
    return True, ""


def _matrix_d0058_violation(event: str, agent, by: str, obj: dict) -> str | None:
    """Rule 11. Returns the violation text or None. Applies ONLY to
    accepted (a literal reading of the spec: "accepted is legal
    when..."; rejected carries "by" with no further tier/basis
    check)."""
    if event != "accepted":
        return None
    if agent == "lead":
        return None  # Lead-tier work: presence of "by" already checked above
    if agent not in AGENT_TIER:
        return None  # unknown agent -- the matrix doesn't define one
    agent_tier = AGENT_TIER[agent]
    by_tier = TIER_ORDER.get(by)
    ok_tier = by_tier is not None and by_tier > TIER_ORDER[agent_tier]
    basis = obj.get("basis")
    ok_basis = basis in BASIS_VALUES
    if ok_tier or ok_basis:
        return None
    return (
        f"role-vs-tier acceptance matrix: agent={agent!r} accepted by={by!r} "
        f"(not strictly above the executor's tier) and no valid basis "
        f"(need critic/queued-to-lead)"
    )


def validate_new_lines(new_lines: list[str], head_lines: list[str],
                        now: datetime.datetime) -> list[str]:
    violations: list[str] = []
    seen_task_ids = extract_task_ids(head_lines)
    max_num = max_task_num(seen_task_ids)
    last_ts = _last_head_ts(head_lines)
    now_limit = now + datetime.timedelta(minutes=10)
    delegated_agents, closed_tasks, rejected_tasks = harvest_task_state(head_lines)

    for idx, line in enumerate(new_lines):
        line_no = len(head_lines) + idx + 1
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError) as e:
            violations.append(f"line {line_no}: invalid JSON ({e})")
            continue
        if not isinstance(obj, dict):
            violations.append(f"line {line_no}: not a JSON object")
            continue

        event = obj.get("event")
        task_id = obj.get("task_id")
        agent = obj.get("agent")
        tag = f"line {line_no} event={event!r} task_id={task_id!r}"

        ts = obj.get("ts")
        category = obj.get("category")
        notes = obj.get("notes")

        if not isinstance(ts, str) or not ts:
            violations.append(f"{tag}: missing/invalid required field 'ts'")
        if not isinstance(event, str) or not event:
            violations.append(f"{tag}: missing/invalid required field 'event'")
        if not isinstance(agent, str) or not agent:
            violations.append(f"{tag}: missing/invalid required field 'agent'")
        if not isinstance(category, str) or not category:
            violations.append(f"{tag}: missing/invalid required field 'category'")
        if not isinstance(notes, str) or not notes.strip():
            violations.append(f"{tag}: missing/empty required field 'notes'")

        if isinstance(event, str) and event and event not in EVENTS:
            violations.append(f"{tag}: 'event' not in the enum ({event!r})")

        if event in MODEL_REQUIRED_EVENTS:
            model = obj.get("model")
            if not isinstance(model, str) or not model:
                violations.append(f"{tag}: 'model' is required for event={event}")

        if event in TASK_ID_REQUIRED_EVENTS:
            if not isinstance(task_id, str) or not task_id:
                violations.append(f"{tag}: 'task_id' is required for event={event}")
            elif not TASK_ID_RE.match(task_id):
                violations.append(f"{tag}: task_id {task_id!r} does not match the t-NNN format (3+ digits)")

        if event == "rejected":
            attempt = obj.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                violations.append(f"{tag}: 'attempt' must be an integer >=1")
            failure_class = obj.get("failure_class")
            if failure_class not in FAILURE_CLASSES:
                violations.append(
                    f"{tag}: 'failure_class' must be one of {sorted(FAILURE_CLASSES)}"
                )

        if event == "accepted" and agent == "builder":
            witness = obj.get("witness")
            if not isinstance(witness, str) or not witness.strip():
                violations.append(f"{tag}: 'witness' is required (non-empty string) for accepted+agent=builder")

        if event == "delegated":
            worker_ref = obj.get("worker_ref")
            if not isinstance(worker_ref, str) or not worker_ref.strip():
                violations.append(
                    f"{tag}: 'worker_ref' is required (non-empty string) for delegated"
                )

        if event == "defect_found":
            ref = obj.get("ref")
            if not isinstance(ref, str) or not ref:
                violations.append(f"{tag}: 'ref' is required (non-empty) for defect_found")

        if event in ("accepted", "rejected"):
            by = obj.get("by")
            if not isinstance(by, str) or not by:
                violations.append(f"{tag}: 'by' is required (non-empty) for {event} (the role-vs-tier acceptance matrix)")
            else:
                mv = _matrix_d0058_violation(event, agent, by, obj)
                if mv:
                    violations.append(f"{tag}: {mv}")

        valid_tid = isinstance(task_id, str) and TASK_ID_RE.match(task_id)
        if event == "delegated" and valid_tid:
            if task_id not in seen_task_ids:
                # (a) a brand-new task -- must be exactly max+1.
                expected = max_num + 1
                actual = int(TASK_ID_RE.match(task_id).group(1))
                if actual != expected:
                    violations.append(
                        f"{tag}: task_id novelty violated -- expected t-{expected:03d} (max+1), got {task_id}"
                    )
            elif task_id in closed_tasks:
                # (d) reopen forbidden -- a collision counts as two tasks.
                violations.append(
                    f"{tag}: delegated on a CLOSED task {task_id!r} (an accepted already exists above) -- "
                    "reopen forbidden, the collision counts as two tasks (the no-silent-reuse rule)"
                )
            else:
                prior_agents = delegated_agents.get(task_id, set())
                if isinstance(agent, str) and agent and agent not in prior_agents:
                    pass  # (b) continuation dispatch from a different tier -- legal
                else:
                    attempt = obj.get("attempt")
                    valid_attempt = (isinstance(attempt, int) and not isinstance(attempt, bool)
                                      and attempt >= 2)
                    if not (valid_attempt and task_id in rejected_tasks):
                        # (c) retry conditions not met -> (d) duplicate-dispatch pattern
                        violations.append(
                            f"{tag}: repeated delegated by the same agent={agent!r} on task_id={task_id!r} "
                            "without attempt>=2 and an earlier rejected -- forbidden duplicate "
                            "(the no-silent-reuse rule)"
                        )
        elif event in ("accepted", "rejected", "escalated", "defect_found") and valid_tid:
            if task_id not in seen_task_ids:
                violations.append(
                    f"{tag}: task_id {task_id!r} does not reference anything existing earlier in the file"
                )

        _harvest_line_into(event, task_id, agent, delegated_agents, closed_tasks, rejected_tasks)

        parsed_ts = parse_ts(ts) if isinstance(ts, str) else None
        if isinstance(ts, str) and ts and parsed_ts is None:
            violations.append(f"{tag}: ts {ts!r} is not ISO format without a timezone")
        if parsed_ts is not None:
            if last_ts is not None and parsed_ts < last_ts:
                violations.append(
                    f"{tag}: ts is not monotonic -- {parsed_ts.isoformat()} is earlier than the previous {last_ts.isoformat()}"
                )
            if parsed_ts > now_limit:
                violations.append(
                    f"{tag}: ts {ts!r} is later than now+10min ({now_limit.isoformat()}) -- "
                    "a narrative-future timestamp"
                )
            last_ts = parsed_ts

        if valid_tid:
            seen_task_ids.add(task_id)
            num = int(TASK_ID_RE.match(task_id).group(1))
            if num > max_num:
                max_num = num

    return violations


def decide(staged_text: str | None, head_text: str | None,
           now: datetime.datetime | None = None) -> tuple[int, list[str]]:
    """Pure gate decision -- tested without git (see tools/test_journal_validator.py)."""
    now = now or datetime.datetime.now()
    staged_lines = split_lines(staged_text)
    head_lines = split_lines(head_text)
    ok, msg = check_append_only(staged_lines, head_lines)
    if not ok:
        return 1, [msg]
    new_lines = staged_lines[len(head_lines):]
    violations = validate_new_lines(new_lines, head_lines, now)
    if violations:
        return 1, violations
    return 0, []


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")


def is_journal_staged(journal_path: str = JOURNAL_PATH) -> bool:
    proc = _git("diff", "--cached", "--name-only")
    return journal_path in proc.stdout.splitlines()


def get_staged_text(journal_path: str = JOURNAL_PATH) -> str:
    proc = _git("show", f":{journal_path}")
    return proc.stdout if proc.returncode == 0 else ""


def get_head_text(journal_path: str = JOURNAL_PATH) -> str:
    proc = _git("show", f"HEAD:{journal_path}")
    return proc.stdout if proc.returncode == 0 else ""


def _main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not is_journal_staged():
        return 0  # rule: file not staged -> silent exit 0
    staged_text = get_staged_text()
    head_text = get_head_text()
    now = datetime.datetime.now()
    code, violations = decide(staged_text, head_text, now)
    if code:
        print(f"journal_validator: {JOURNAL_PATH} FAILED validation:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    """Outer boundary: any exception (not a validation FAIL, but a crash
    of the validator itself) -> exit 2 with a traceback, fail-closed,
    same as mechanism_gate."""
    try:
        return _main(argv)
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
