# Weekly Calibration Protocol

An executable checklist for the weekly calibration run. Loaded only
for the duration of the run, not at boot. Without a single canonical
checklist, checks tend to scatter across separate decision entries
covering rules 1–10, and the calibrator ends up re-deriving them each
time; this file is their canonical home. A new mechanism that
promises "calibration will catch X" is obligated to add a check here
in the same commit — otherwise the promise is empty (a mechanism with
no registered detector is not a mechanism, it's a wish).

Input data: logs/routing-log.jsonl (across every deployment you run),
usage records (cc_usage) via tools/usage_report.py, the git history
for the period, and diffs to DECISIONS.md.

## Checklist (mechanism → violation → how to check)

1. **Rules 1/2/8 — silent self-exclusion.** Main sessions in the
   usage records with a large volume of reads/edits, for which the
   same period shows neither a `delegated` nor a `dispatch_skipped`
   event. Source: usage report + journal.
2. **Rule 3 — critic on large diffs.** Commits in the period with
   over roughly 100 changed lines, accepted with no
   `delegated(critic)` and no "critic: skipped, <reason>" note in the
   accepting `accepted` event. Source: `git log --stat` + journal.
3. **Rule 6 / the escalation rule.** Two `rejected` events for the
   same task at the same tier with no subsequent `escalated` = a
   violation. An `escalated` with no `rejected` trail = a question
   about the self-declaration. This is checked mechanically: events
   for one task are linked by `task_id`, attempts by the `attempt`
   field; an event from the task_id-mandatory set with no `task_id`
   is a violation in its own right (the typed-fields rule).
4. **The self-declared-model discrepancy check.** The `model` field
   on delegated/escalated/accepted/rejected against the actual
   execution models in the usage records; a discrepancy is itself a
   calibration event.
5. **Degradation.** Every `lead_restored` carries, in its notes, a
   review of the window (what was reviewed, the verdict; an empty
   window is noted explicitly). A `lead_degraded` with no matching
   `lead_restored` by the session boundary — cross-check against the
   journal's last event. Cross-check the actual models of Lead
   sessions in the period (main chains in the usage records, NOT
   sidechains) against window coverage by matched
   lead_degraded/lead_restored pairs: Lead-tier activity on a model
   below the top tier, outside a declared window, is silent
   degradation — a failure of both in-session checkpoints (this
   extends check 4 from workers to Lead itself; retroactive pairs are
   found via a note in notes).
6. **Acceptance.** Every completed `delegated` has a matching
   `accepted` or `rejected`. `accepted` for scout dispatches carries a
   note of a targeted spot-check (negative claims — mandatory).
   Acceptance from above: for every `accepted`, cross-check the tier
   of the accepting session (usage records, main chain at the time of
   the event) against the tier of `agent` — strictly above: fine;
   equal or below: notes must carry input from a tier above (a critic
   verdict) or a "queued for the full Lead" note, otherwise it's a
   violation (the session self-certifying). The "critic: skipped"
   waiver is legitimate only for an acceptor above the executor —
   read this together with check 5 (degradation windows).
   Environment-negative claims: negative claims ABOUT THE ENVIRONMENT
   in reports/verdicts of the window (no service/command/file) are
   valid only with a positive check using the canonical form (command
   hygiene, point 6).
7. **Journal vs. transcripts, both directions.** A sidechain in the
   usage records with no matching `delegated` event = an
   undocumented dispatch; a `delegated` with no matching sidechain =
   a phantom event.
8. **The four-questions-per-mechanism rule.** New DECISIONS.md
   entries for the period contain answers to (a)/(b)/(c)/(d), and the
   (c)-answer is REGISTERED: a check in this protocol added in the
   same commit, or an explicit external detector named in the
   decision's own text. An unregistered detector is a wish dressed as
   a mechanism — write it up as a finding. The (d)-answer names the
   enforcement layer: what triggers the mechanism and what code sits
   on the execution path, or an explicit "on discipline alone" line
   with a named leak detector; a missing (d)-line, or a
   discipline-only answer with no detector, is a finding. A
   recognition audit: commits for the period, across every repo you
   run this in, are scanned for mechanism-shaped edits with no
   four-questions block and no matching decision entry (a duty or
   machine check added outside DECISIONS.md is an "unrecognized
   mechanism"); the verdict is a judgment call for the calibrating
   Lead — the gate's net only supplies candidates; anything found is
   a finding, plus a retroactive four-questions writeup. The
   (b)-answer is an axis-by-axis ENUMERATION against the current
   SIBLING_MAP (prose saying "axes are covered" is not an answer);
   gate liveness = `git config core.hooksPath` pointing at
   `.githooks` in every repo you run this in; mechanism-shaped commits
   for the period are spot-checked for the axis block (the only way
   around the gate is `--no-verify` or an explicit waiver line);
   "axes: not a mechanism" lines for the period are audited for
   honesty — a waiver used on a substantive mechanism is a violation.
   Tier declarations: `tier:` lines on mechanism commits for the
   period are reconciled against the sessions' actual models per your
   usage records/transcripts (same reference as check 5) — a lead-tier
   declaration written by a below-lead session is a violation (the
   recognized-then-did-it-anyway class: the gate forces the
   declaration, this check judges its truth); "axes: not a mechanism"
   lines are also audited as a potential bypass of the tier
   requirement (a substantive mechanism relabeled to avoid declaring
   the tier).
9. **SIBLING_MAP — class-wide completeness.** Commits that close a
   finding name the axes it touches; "new axis for SIBLING_MAP" lines
   noted anywhere in your logs get folded into the map.
10. **Growth of live files.** Line counts of the boot-path files (the
    files BOOT.md's boot sequence reads, plus CLAUDE.md and
    CURRENT_CONTEXT.md), plus docs/SIBLING_MAP.md (the map is
    supposed to stay small); compare against the previous run (the
    numbers go in the `calibrated` event's notes). Monotonic growth of
    a live file = a violation (closed material not being archived).
11. **Rule #1 applied to the routing machinery itself.** Routing
    overhead measured from data, not estimates: the share of Lead-tier
    tokens spent on journal/acceptance/spot-checks/specs, against the
    cost of the delegated work itself (usage records: main session vs.
    sidechains for the period). If the mechanism's overhead is
    systematically comparable to the work it routes, that's evidence
    for simplifying the mechanism — by a decision, not by quietly
    ignoring it. Also here — the synchronous-dispatch check: window
    dispatches run synchronously are spot-checked against a
    justification in the delegating event's notes (rule 7: synchronous
    only when the next step depends on the result AND no other work is
    pending); a sync dispatch with no justification is a finding.
    Also here — the size of the SessionStart hook's output (Rule #1
    applied to "reality piped into context"): measure
    `echo '{}' | python tools/session_context.py` with a byte/line
    count, both numbers going in the `calibrated` event's notes next
    to the check-10 counts; monotonic growth run over run with no
    decision behind it is evidence for simplification — the same
    class as the overhead check above.
12. **SIBLING_MAP liveness.** Every concrete path named in the map
    exists; the rules/mechanisms it names are still live. A dead path
    is a violation of the map's own same-commit-maintenance rule.
    Repeats in the period (a defect of an already-fixed class
    recurring outside the map's listed spots) get written up as
    findings about the map itself, and the axis gets corrected. If
    you keep decisions as an index plus a full-text companion file,
    the count of entries in each must match; if you keep a single
    DECISIONS.md, this half of the check doesn't apply.
13. **Evidence-based acceptance (both directions), typed fields.**
    (a) Every `accepted` for a builder dispatch carries a `witness`
    field — the actual output of the verification run (command +
    result); `accepted` for builder work with no witness is a
    violation. (b) `defect_found` events reference the task_id of the
    original `accepted` via the `ref` field; the false-accept rate by
    tier (defect_found / accepted for the window) is computed and
    written into the `calibrated` event's notes; a systematic
    false-accept rate for a tier is evidence for moving that tier's
    table status DOWN (Update Rule 1). (c) Every `rejected` carries a
    `failure_class` (spec / capability / recon / tooling); a missing
    class is a violation (check together with check 3). Counts for
    checks 3 and 13 are produced by `tools/calibration_counts.py`: the
    script prints CANDIDATES, and the verdict is left to the
    calibrating Lead. The counting script's own failure detector:
    tests in the canonical suite run; a baseline cross-check (the
    numbers from your first manual count are recorded in a
    `calibrated` event's notes and reproduced by the script on later
    runs); on every run, Lead spot-checks 1–2 counts against the
    journal by hand; schema drift is caught by a test that keeps the
    counting script's constants in sync with the journal validator.
    (d) A systematic `failure_class=spec` for a tier signals
    dispatches with no DoD; check the Lead's recent specs for
    acceptance criteria plus a verification run.
    (e) Task_id integrity: no duplicate task_ids between unrelated
    tasks, across every journal you run. A known-duplicate id
    recorded in the journal counts as TWO tasks in every check-3/13
    count from then on (note it on the following event; don't rewrite
    history).
    (f) Timestamp honesty: spot-check event timestamps for the window
    against an external clock (a request database / usage records /
    git log — sources written by code, not narrative); an
    out-of-order or non-monotonic timestamp within a session is a
    violation (the `ts` field is read from the clock right before
    writing, never from the session's own narrative). A known past
    timestamp error stays un-rewritten; for any timeline count that
    spans it, take the real times from the correcting event's notes.
    (g) SessionStart hook liveness: the hook fails open (if it breaks,
    it warns and exits 0, so sessions keep running without "reality
    piped in"). Check: 1–2 transcripts in the window show a
    "NOW: ... (local system clock)" line at session start; its
    absence means the hook is broken or unregistered — a violation.
    The same check catches a failed startup preflight: a new
    `rejected`/`failure_class=tooling` from a provider quota error
    while `tools/preflight_quota.py` exists means either something
    bypassed the script, or the script's own math has a leak — work
    out which.
14. **A golden set for recon, and a regression rule for prompt
    edits.** (a) Git log for the window on `.claude/agents/*.md`:
    every edit to a tier's role file that has an exam set (scout —
    PROCESS/SCOUT_GOLDEN_SET.md; critic — PROCESS/CRITIC_EXAM.md,
    both generated at onboarding), and every change to its `model:`
    frontmatter, is accompanied by a Runs-log line in that set's
    file in the same commit; an edit with no run is a violation. (b) Key
    liveness: run the verify commands for at least 2 questions in the
    set; a stale key is a bug in the eval itself — fix it BEFORE
    drawing any conclusion about scout degrading. (c) A rise in
    `failure_class=recon` for the window (check together with check
    3), with no out-of-cycle set run, means one should be scheduled.
    (d) Edits to tier role files with NO exam set (builder — by
    design: execution-based acceptance covers every task) get a note
    in the `calibrated` event. (e) A fabrication guard for any exam or
    entrance run that goes through a non-Claude harness: every such
    run in the window is checked by `tools/pi_run_guard.py` and
    carries a guard verdict (a Runs-log line / journal event) BEFORE
    it's graded against the key; an accepted or graded run with no
    guard line is a leak of the discipline-only trigger (evidence for
    promoting it into a code gate). Guard liveness: a known-bad replay
    fed to the guard must come back REJECTED.
15. Reserved for deployment-specific checks (register yours here —
    see the mechanism rule: every mechanism registers its failure
    detector).
16. **Two-pass external recon.** New RELATED_WORK entries for the
    period (and queue entries referencing an external survey): does
    each one name Lead's own second-pass trail — which files of the
    external repo Lead read itself, beyond the scout's digest? A
    section or plan resting on the digest alone, with no second-pass
    trail, is a finding; mechanisms drawn from such a plan freeze in
    the queue until the second pass happens.
17. Reserved for deployment-specific checks (register yours here —
    see the mechanism rule: every mechanism registers its failure
    detector).
18. **Economic trend (Rule #1 applied to the whole system) — "are we
    actually saving, and which way is the trend going."** Every run:
    `python tools/savings_report.py --until <end of window>` (full
    list-price API rates from usage_report.py, cache discounts
    counted, no batch pricing). In the `calibrated` event's notes,
    record four numbers against the previous data point: (a) $/day for
    the routed window; (b) gross savings versus the delegation
    counterfactual (in $ and %: sidechains as they actually ran,
    priced against that same token profile at top-tier rates); (c)
    cost per accepted unit of delegated work (sidechain actuals /
    accepted count across your journals for the window — check 13
    supplies the count); (d) API-track accounting: a rollup by
    traffic_kind plus the real-traffic share. Your first run records
    the baseline — note the method caveats alongside it (a baseline
    taken while still building mechanisms is censored, and the
    coordination premium isn't separable from non-delegable Lead
    work); read the trend only with those caveats until you've
    accumulated weeks without active mechanism-building. This check's
    own failure detector: missing economic numbers in a `calibrated`
    event's notes are visible to the next run and to the operator (the
    same class as a partial count silently passing as complete); the
    script's own failure mode is caught by tests in the canonical
    suite run.

## Closing out a run

DELEGATION_TABLE.md statuses move only on this data (Update Rule 1;
full cost including retries — Update Rule 4). On completion, write a
`calibrated` event to the journal: notes carry the period, the number
of events reviewed, table status changes, the check-10 line counts,
the check-11 hook output size, the check-13 false-accept rate by tier,
and the check-18 economic numbers (against the previous data point).
No `calibrated` event for more than 7 days while routed traffic is
live is itself a violation, visible as a Last Calibration line in
every session's Boot Report (the detector lives outside this protocol
on purpose: calibration cannot be the detector of its own absence).
