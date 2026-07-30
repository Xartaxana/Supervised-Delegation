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
    c2) (dead-worker replacement) the SAME situation as (c) (agent
       matches, task open) but rejected is NOT required and attempt
       does not need to grow -- this is not a rule-6 retry -- is legal
       if notes contain the literal substring "replaces_worker:" with
       a non-empty handle immediately after it (the first non-
       whitespace token), AND that handle LITERALLY matches the
       worker_ref of some earlier delegated line for this SAME
       task_id (any agent). Guard against a fabricated replacement: a
       handle that appears in no earlier delegated line for this
       task_id -> FAIL (see extract_replaces_worker).
    d) anything else -- FAIL (the duplicate-dispatch pattern: same
       agent, no attempt, no rejected, no valid replaces_worker; and
       delegated on a CLOSED task -- reopening is forbidden, a
       collision counts as two tasks).
    For a new accepted/rejected/escalated/defect_found -- it must
    reference a task_id already seen earlier in the file (in HEAD or
    earlier in this same commit); unchanged otherwise.
10. ts of new lines is monotonic relative to HEAD's last line and to
    each other; not later than now+10 minutes (a narrative-future
    timestamp). No lower bound.
11. The role-vs-tier acceptance matrix (NEW lines only, accepted
    only -- rejected carries "by" with no further check, a literal
    reading of the spec). For agent=lead the matrix doesn't apply --
    presence of "by" is enough. For agent in {scout, builder, critic,
    designer} (batch B7, 2026-07-24 -- membership-in-set "basis in BASIS_VALUES"
    replaced by legality-PER-(by, agent)-PAIR, after the live AO3
    07-24 leak: two different Sonnet coordinators accepted Sonnet-class
    (builder) results via basis=queued-to-lead -- membership passed
    that basis regardless of WHICH by/agent pair it was attached to).
    PRECEDES branches (a)-(f) (t-323, port of AO3's fix 30e79c8,
    2026-07-28): an enum check "is by even in TIER_ORDER" -- an
    unknown by FAILS unconditionally, BEFORE the judge branch too --
    judge's by-independence (staff fix t-276, branch b below) applies
    only to LEGAL tier words; no basis of any kind legalizes an
    acceptance from an unknown acceptor:

    a) ok_tier -- tier(by) > tier(agent) (haiku<sonnet<opus<fable by
       agent: scout=haiku, builder=sonnet, critic=opus, designer=opus
       (designer's deployment binding, same tier as critic; see
       .claude/agents/designer.md)) -- legal at
       ANY basis (or none).
    b) basis=="judge" -- legal ONLY when the line's OWN "category" is
       ∈ {"recon", "implementation"} (a leaf-class dispatch per this
       toolkit's own CLAUDE.md, "Leaf routing": "Judge acceptance is
       legitimate ONLY for leaf-class dispatches (recon, or
       implementation to a written spec)"); unchanged from before this
       batch. CHECKED FIRST, independent of tier(by) -- a calibrated
       judge is not a coordinator-tier resolution, so it is not
       subject to the floor in (c).
    c) FLOOR "below sonnet: no coordination provided for": if tier(by)
       is KNOWN and strictly below sonnet's tier (i.e. by=="haiku")
       AND ok_tier does not hold AND basis is not "judge" -- FAIL
       unconditionally, no basis rescues it (this toolkit's own
       CLAUDE.md "Role != tier" matrix: "below Sonnet | no
       coordination is provided for | --"). Decision fork (documented
       deliberately, per the same fork the staff sibling batch names):
       the core rule's literal text ("OR the decision carries a higher
       tier's input (a critic verdict)") would read basis="critic" as
       rescuing ANY by, including haiku -- but the matrix's "below
       Sonnet" row states, without exception, "no coordination is
       provided for"; a haiku-tier session is not a functioning
       coordinator at all in this structure. The floor wins over the
       general "carries higher tier's input" allowance for
       basis="critic"/"queued-to-lead" (but NOT for basis="judge" --
       see (b), a separate non-coordinator path).
    d) basis=="critic" -- legal ONLY when the critic tier (opus,
       fixed) is strictly above tier(agent), i.e. agent is
       scout/builder-class: a critic verdict on top of the critic's
       own class (agent=="critic") is not meaningful and NOT legal.
    e) basis=="queued-to-lead" -- legal ONLY for agent=="critic"
       (critic-class, tier opus) AND by ∈ {"sonnet", "opus"} (for
       by="sonnet" the queue is legal specifically for critic-class --
       NOT builder-class: at a sonnet coordinator, builder-class is
       legal only via basis="critic" -- exactly the AO3 pattern; for
       by="opus" the queue is legal for critic-class as an equal-tier
       concession).
    f) any other basis (including none) -- FAIL with the generic
       role-vs-tier message.

    SURFACE NARROWING (t-323, critic verdict 2026-07-28): the by-shape
    enum gate applies ONLY to accepted with agent ∈ {scout, builder,
    critic, designer} (designer added to AGENT_TIER, same class);
    OUTSIDE the gate (recorded DECISIONS, not oversights):
    rejected -- by carries no matrix check at all (a literal reading
    of the spec, see the top of rule 11); agent=lead -- a non-tier by
    is legal (a live precedent, by="operator", in
    logs/routing-log.jsonl -- the operator sits at the apex of the
    hierarchy, rule 11a); agent outside AGENT_TIER -- the matrix is
    simply undefined by the spec (an early return before any branch).
    Form-validation candidates for these three neighbors are queued
    for calibration #5 (the coordinator places the queue item itself).

    NOTE (history): at the time of this port the reference validator
    (the staff repo's own tools/journal_validator.py) accepted
    basis=="judge" UNCONDITIONALLY, despite its own CLAUDE.md
    documenting the leaf-class restriction -- this port implemented
    the restriction first, from the toolkit's OWN CLAUDE.md text
    (quoted above). The staff validator was then brought to the same
    leaf-gated form the same day (staff fix t-276: LEAF_CATEGORIES,
    a dedicated R13 message) -- both siblings of the pair are now
    converged; this note stays as provenance, not as a live delta. The
    same convergence pattern repeats for batch B7 (2026-07-24): the
    pair-legality table above ported from the staff sibling's own
    same-day fix.
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
AGENT_TIER = {
    "scout": "haiku", "builder": "sonnet", "critic": "opus",
    # designer added: designer's deployment binding is opus (see
    # .claude/agents/designer.md, model: opus) -- the same tier as
    # critic. Class of the gap before this line existed: agent=
    # "designer" was outside AGENT_TIER, so the role-vs-tier
    # acceptance matrix silently did not apply to its accepted lines
    # (see the "agent not in AGENT_TIER" early return below).
    "designer": "opus",
}
# The upper-mid-tier CLASS (decision, after a critic finding): branch
# (5) basis="queued-to-lead" used to be keyed on the literal name
# agent=="critic" -- a narrowing by NAME, not by CLASS. designer
# carries the same tier (opus) and the same coordinator-facing
# semantics for branch (5) -- the queue to Lead is legal for BOTH when
# the coordinator is not strictly above. basis="critic" is NOT
# extended to designer the same way -- branch (4) already works by a
# NUMERIC tier comparison, not by name, so a same-tier critic input
# already fails to legalize designer's acceptance too, without any
# separate change (same as it already fails for agent=="critic" itself).
QUEUED_TO_LEAD_AGENTS = {"critic", "designer"}
# Known non-judge basis string values -- a PLAIN enum/reference (external
# consumers: PROCESS/WEEKLY_CALIBRATION_PROTOCOL.md, log_append.py), no
# longer used as a legality check "basis in BASIS_VALUES" -- since batch
# B7 (2026-07-24) legality is decided per (by, agent) pair in
# _matrix_d0058_violation, not bare membership (closes the AO3 07-24
# leak: basis=queued-to-lead passed membership for ANY by/agent,
# including builder-class at a sonnet coordinator).
BASIS_VALUES = {"critic", "queued-to-lead"}
JUDGE_BASIS_VALUE = "judge"
# Leaf-class categories a "judge" basis is legal for (rule 11 / this
# toolkit's own CLAUDE.md "Leaf routing" section, R13/D-0087-equivalent):
# "recon, or implementation to a written spec".
LEAF_CATEGORIES = {"recon", "implementation"}

TASK_ID_RE = re.compile(r"^t-(\d{3,})$")
# Dead-worker replacement marker (rule 9c2): the literal substring
# "replaces_worker:" plus a non-empty handle = the first non-whitespace
# token right after the colon (matches the shape of existing worker_ref
# values -- 'cli:...', 'agent:...' -- no internal whitespace).
REPLACES_WORKER_RE = re.compile(r"replaces_worker:(\S+)")
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


def extract_replaces_worker(notes) -> str | None:
    """Rule 9(c2): pulls the handle out of a "replaces_worker:<handle>"
    marker in notes -- the literal substring plus the first non-
    whitespace token right after the colon. None if there's no marker
    (notes isn't a string, or the substring is absent)."""
    if not isinstance(notes, str):
        return None
    m = REPLACES_WORKER_RE.search(notes)
    return m.group(1) if m else None


def _harvest_line_into(event, task_id, agent, worker_ref, delegated_agents: dict, closed_tasks: set,
                        rejected_tasks: set, task_worker_refs: dict) -> None:
    """Rule 9(b/c/c2) state: updates the per-task_id history by ONE line
    (used both to seed state from HEAD and, line by line, for new
    lines -- call order = line order in the file, so the state at the
    time line N is checked reflects exactly "everything earlier in
    the file"). task_worker_refs accumulates every worker_ref of every
    delegated (any agent) for this task_id -- rule 9(c2) looks for the
    claimed prior worker_ref in this same set, not only among lines by
    the same agent."""
    if not (isinstance(task_id, str) and TASK_ID_RE.match(task_id)):
        return
    if event == "delegated" and isinstance(agent, str) and agent:
        delegated_agents.setdefault(task_id, set()).add(agent)
        if isinstance(worker_ref, str) and worker_ref.strip():
            task_worker_refs.setdefault(task_id, set()).add(worker_ref.strip())
    elif event == "accepted":
        closed_tasks.add(task_id)
    elif event == "rejected":
        rejected_tasks.add(task_id)


def harvest_task_state(lines: list[str]):
    """Seeds rule 9(b/c/c2) state from the HEAD version (or any prefix
    of lines) -- (delegated_agents, closed_tasks, rejected_tasks,
    task_worker_refs)."""
    delegated_agents: dict[str, set] = {}
    closed_tasks: set = set()
    rejected_tasks: set = set()
    task_worker_refs: dict[str, set] = {}
    for line in lines:
        obj = _try_parse_obj(line)
        if obj is None:
            continue
        _harvest_line_into(obj.get("event"), obj.get("task_id"), obj.get("agent"), obj.get("worker_ref"),
                           delegated_agents, closed_tasks, rejected_tasks, task_worker_refs)
    return delegated_agents, closed_tasks, rejected_tasks, task_worker_refs


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
    """Rule 11 (batch B7, 2026-07-24: pair-legality replaces
    membership-in-set). Returns the violation text or None. Applies
    ONLY to accepted (a literal reading of the spec: "accepted is
    legal when..."; rejected carries "by" with no further tier/basis
    check).

    Check order is deliberately NOT a "first match wins, unexplained"
    if-cascade -- each branch reads against one row of the CLAUDE.md
    "Role != tier" table (see the module docstring's rule 11 for the
    full pair table and quotes):

    1) basis=="judge" -- a separate, non-coordinator acceptance path
       (a calibrated judge, R13/D-0087): legal ONLY on a leaf-class
       category, INDEPENDENT of tier(by) -- checked first so the floor
       in (3) never intercepts it (unchanged from before this batch).
    2) ok_tier: tier(by) > tier(agent) -- legal at any basis.
    3) FLOOR: tier(by) is known and strictly below sonnet (i.e.
       by=="haiku") -- FAIL unconditionally, no basis (other than the
       already-handled "judge") rescues it ("below Sonnet: no
       coordination is provided for").
    4) basis=="critic": legal only if tier("opus") is strictly above
       tier(agent) -- i.e. agent is scout/builder-class, not critic.
    5) basis=="queued-to-lead": legal ONLY for agent=="critic" AND
       by ∈ {"sonnet", "opus"}.
    6) otherwise -- FAIL with the generic role-vs-tier message."""
    if event != "accepted":
        return None
    if agent == "lead":
        return None  # Lead-tier work: presence of "by" already checked above
    if agent not in AGENT_TIER:
        return None  # unknown agent -- the matrix doesn't define one
    if by not in TIER_ORDER:
        # Port of AO3's fix (their calibration #4, commit 30e79c8): an
        # enum/shape check ("is by even a known tier") precedes ANY
        # matrix semantics, including judge (t-323) -- no basis
        # legalizes an acceptance from an unknown acceptor; ok_tier/
        # floor stayed silent at by_tier=None, and basis="critic"/
        # "judge" never looked at by at all -- the AO3 07-24 hole this
        # closes.
        return (
            f"role-vs-tier acceptance matrix: by={by!r} is not a known "
            f"tier ({sorted(TIER_ORDER)}) -- basis cannot legalize an "
            f"acceptance from an unknown acceptor (an enum/shape check "
            f"precedes the matrix's semantics; ported from AO3's fix "
            f"30e79c8)"
        )
    agent_tier_name = AGENT_TIER[agent]
    agent_tier = TIER_ORDER[agent_tier_name]
    by_tier = TIER_ORDER.get(by)
    basis = obj.get("basis")

    # (1) judge -- separate path, not a coordinator resolution; category
    # decides everything, checked before tier/floor. NARROWED (a
    # critic finding): the judge accepts ONLY leaf-class EXECUTORS
    # (agent ∈ {scout,builder}) -- specs (designer) and reviews
    # (critic) stay outside the judge's remit by policy, regardless of
    # category (before this fix, agent="designer" + basis="judge" +
    # category="implementation" wrongly passed, since only category
    # was checked, never agent). Order: the category gate runs FIRST
    # (a non-leaf category fails for ANY agent, including scout/
    # builder -- a regression pin for the pre-existing tests), the
    # agent gate runs SECOND, only once category is already leaf-class.
    if basis == JUDGE_BASIS_VALUE:
        if obj.get("category") not in LEAF_CATEGORIES:
            return (
                f"role-vs-tier acceptance matrix: basis \"judge\" is legal "
                f"only for a leaf-class dispatch (category recon/"
                f"implementation), got category={obj.get('category')!r}"
            )
        if agent not in ("scout", "builder"):
            return (
                f"role-vs-tier acceptance matrix: basis \"judge\" is legal "
                f"only for agent∈{{scout,builder}} (leaf-class EXECUTORS) -- "
                f"agent={agent!r} is not eligible for judge acceptance "
                f"regardless of category (specs and reviews stay outside "
                f"the judge's remit by policy)"
            )
        return None

    # (2) ok_tier -- strictly above, legal at any basis (or none).
    if by_tier is not None and by_tier > agent_tier:
        return None

    # (3) FLOOR: by of a known tier strictly below sonnet -- no
    # coordination provided for, no basis rescues it ("Role != tier"
    # matrix: "below Sonnet: no coordination is provided for"). The
    # fork against the core rule's literal "carries a higher tier's
    # input" text is documented and resolved in the floor's favor --
    # see the module docstring's rule 11.
    if by_tier is not None and by_tier < TIER_ORDER["sonnet"]:
        return (
            f"role-vs-tier acceptance matrix: by={by!r} -- tier below "
            f"sonnet, no coordination is provided for at any basis="
            f"{basis!r} ('below Sonnet: no coordination is provided "
            f"for'); agent={agent!r} cannot be accepted by this by"
        )

    # (4) basis=="critic" -- legal if the critic (opus) is strictly
    # above agent (agent is scout/builder-class). A NUMERIC tier
    # comparison (not a name check) -- this already covers designer
    # correctly with no separate change: designer is also opus-tier,
    # tier("opus") > tier("opus") is false, the same rejection as for
    # agent=="critic" itself today.
    if basis == "critic":
        if TIER_ORDER["opus"] > agent_tier:
            return None
        return (
            f"role-vs-tier acceptance matrix: agent={agent!r} (tier "
            f"{agent_tier_name}) accepted with basis='critic', but the "
            f"critic (opus) is not strictly above the executor's tier -- "
            f"basis='critic' does not raise acceptance above its own "
            f"opus tier; the legal path for agent={agent!r} is by "
            f"strictly above opus (by='fable'), or basis='queued-to-lead' "
            f"at by∈{{sonnet,opus}}"
        )

    # (5) basis=="queued-to-lead" -- legal for the upper-mid-tier CLASS
    # (QUEUED_TO_LEAD_AGENTS = {critic, designer}, see its own comment
    # above) at by∈{sonnet,opus} (the AO3 case: builder-class at a
    # sonnet coordinator does NOT pass -- legal only via
    # basis='critic').
    if basis == "queued-to-lead":
        if agent in QUEUED_TO_LEAD_AGENTS and by in ("sonnet", "opus"):
            return None
        return (
            f"role-vs-tier acceptance matrix: agent={agent!r} accepted "
            f"by={by!r} with basis='queued-to-lead' -- for {agent!r}-class "
            f"at a {by!r} coordinator only basis='critic' is legal (or "
            f"judge on a leaf-implementation, if category is leaf-class "
            f"AND agent∈{{scout,builder}}); the queue (queued-to-lead) is "
            f"available only for the class {sorted(QUEUED_TO_LEAD_AGENTS)}"
        )

    # (6) anything else -- generic role-vs-tier rejection.
    return (
        f"role-vs-tier acceptance matrix: agent={agent!r} accepted by={by!r} "
        f"(not strictly above the executor's tier) and no valid basis "
        f"(need critic/queued-to-lead by pair, or judge on a leaf-class "
        f"dispatch [category recon/implementation])"
    )


def validate_new_lines(new_lines: list[str], head_lines: list[str],
                        now: datetime.datetime) -> list[str]:
    violations: list[str] = []
    seen_task_ids = extract_task_ids(head_lines)
    max_num = max_task_num(seen_task_ids)
    last_ts = _last_head_ts(head_lines)
    now_limit = now + datetime.timedelta(minutes=10)
    delegated_agents, closed_tasks, rejected_tasks, task_worker_refs = harvest_task_state(head_lines)

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
                    retry_ok = valid_attempt and task_id in rejected_tasks
                    replaces_handle = extract_replaces_worker(notes)
                    if retry_ok:
                        pass  # (c) legal retry after rejected
                    elif replaces_handle is not None:
                        prior_refs = task_worker_refs.get(task_id, set())
                        if replaces_handle in prior_refs:
                            pass  # (c2) legal dead-worker replacement
                        else:
                            violations.append(
                                f"{tag}: replaces_worker={replaces_handle!r} does not match any "
                                f"earlier delegated worker_ref for task_id={task_id!r} -- a "
                                "fabricated replacement is forbidden (rule 9c2)"
                            )
                    else:
                        # (c) retry conditions not met, no replaces_worker marker -> (d)
                        violations.append(
                            f"{tag}: repeated delegated by the same agent={agent!r} on task_id={task_id!r} "
                            "without attempt>=2 and an earlier rejected -- forbidden duplicate "
                            "(the no-silent-reuse rule); a legal alternative is a "
                            "'replaces_worker:<prior worker_ref>' marker in notes when replacing "
                            "a dead worker with no verdict (rule 9c2)"
                        )
        elif event in ("accepted", "rejected", "escalated", "defect_found") and valid_tid:
            if task_id not in seen_task_ids:
                violations.append(
                    f"{tag}: task_id {task_id!r} does not reference anything existing earlier in the file"
                )

        _harvest_line_into(event, task_id, agent, obj.get("worker_ref"), delegated_agents, closed_tasks,
                           rejected_tasks, task_worker_refs)

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
