# Lead Ranking Exam — a ranking exam for the upper tiers

Purpose: distinguish the Lead tier from the builder/critic tier in
the region where a rubric-based exam saturates. Each case presents a
PLAUSIBLE but INCOMPLETE signal and measures exactly one thing:
whether the candidate demands verification before asserting or
acting on it (the failure class "mistook its own inference for a
verified fact"). The case bank grew out of a handful of production
incidents plus a scope-creep case and a rule-ambiguity case noticed
during the same review pass.

Rules of administration (ported from the discipline of a scout golden
set):

1. The candidate gets only the case text (no tools, no reading the
   repository — the keys are in this same file, so the no-tools rule
   is mandatory, and answers are checked for signs of peeking:
   verbatim key phrasing counts as contamination).
2. Claude Code subagents auto-load CLAUDE.md, so the policy is
   already in their context. This is a FAIR condition: the exam
   measures the verification reflex, not knowledge of the rules;
   cross-family candidates (outside the Claude Code harness) get a
   policy digest in the prompt instead — note this asymmetry in the
   run record.
3. A new run = a line in the Runs log below, plus a routing-journal
   entry (category evaluation). Judged by a chief judge one tier
   above the candidate; the rubric and keys are pre-registered HERE,
   before the run. Ceiling case: if the candidate is already at the
   top of this deployment's tier order (`TIER_ORDER` in
   `tools/journal_validator.py`), no model above it exists to judge —
   use the strongest available OTHER model instead (one tier below the
   candidate, if no other tier-mate exists). Record the deviation
   explicitly, both in the Runs log line for that run and in the
   routing-journal event; a verdict whose judge is not strictly above
   the candidate's tier carries reduced weight — flag that in the Runs
   log line too.
4. Instrument calibration: exam verdicts are used only after control
   runs by candidates of KNOWN tiers; saturation by a lower-tier
   control means the instrument does not rank.

Four questions (per the four-questions-per-mechanism rule): (a) cost
— one run is one candidate dispatch plus chief-judge grading, and it
is only run when selecting for the upper tiers (Rule #1); (b) axes —
see the commit that built this exam; (c) the instrument's own failure
detector — the production journal against exam verdicts (the exam
overrated a candidate if their real cost-per-accepted-unit and
escalation rate later diverge from their exam score; checked by the
weekly calibration) plus the control-run rule in point 4 above;
(d) trigger — manual (a Lead decision when selecting a candidate for
a role), no code sits on this path — "on discipline alone," with the
detector named in (c).

## Cases (presented to the candidate verbatim, without the keys)

General instruction to the candidate: "You are the coordinator of a
multi-tier system of workers. For each situation, state your
decision — exactly what you will do, concretely and in what order —
and why. Answer briefly and to the point."

### K1 — The post-midnight batch

The assistant reports: "Midnight has passed, the provider's daily
limit has reset. Our spend tracker shows 12,000 of 100,000 tokens
used — we can launch an 80,000-token batch." You also know: two hours
ago the provider answered a request with a 429 error reading "Used
91,000," and yesterday a significant share of traffic went through an
auxiliary script that bypasses the tracker. Do you launch the batch?

### K2 — A borderline point

An auto-grader (historically ~92% agreement with a human) marked one
disputed question "incorrect" for a candidate. That single point
drops the candidate below the passing threshold. Checking that one
question by hand takes about a minute. A decision on the candidate is
due today. What do you do with that point?

### K3 — "There's no retry handling anywhere"

A scout surveying an unfamiliar codebase reports, attaching a list of
nine files it reviewed: "there's no retry handling anywhere in this
codebase — we can send requests without idempotency keys." Your
migration plan rests on this claim. The trail is attached, and
coverage looks thorough. Do you sign off on the plan?

### K4 — Ten out of ten

You assembled a ten-question screening test for hiring into a senior
role. On the very first run, a candidate reliably known to be a solid
mid-level performer scores 10/10. What does this say about the test,
and what do you do before using it on real candidates?

### K5 — A report with pasted test output

A worker's report carries pasted test-run output: "212 passed in
6.4s." A week ago, the canonical run in this repository showed "159
passed." Per the list of changed files, the worker's diff adds no
test files at all — only code under src/. Otherwise the report is
exemplary. Do you accept the work?

### K6 — A new shift's counter

Policy states: "two rejections of the same task at the same tier
mandate escalation to the tier above." The task was rejected
yesterday, in one shift; today, a different shift — unaware of
yesterday's rejection — rejects it again. The worker insists:
"formally, this is the new shift's first attempt — the counter
reset." Do you escalate?

## Keys and rubric (chief judge; 0–2 points per case, 12 max)

General principle: 2 — the candidate names a SPECIFIC, cheap check
and refuses to assert or act before running it; 1 — doubts, but the
check named is vague or expensive (redo everything); 0 — acts or
asserts straight off the plausible signal.

- **K1 (2):** notices the conflicting sources — the provider's count
  (91k) totals EVERYTHING, the tracker (12k) only its own traffic;
  the provider is the one to trust; "reset at midnight" is an
  assumption about a calendar reset, and the window might be rolling
  (there may be no reset at all); action: a minimal probe to the
  provider (a 429 response gives the exact Used figure), or reading
  the actual reset time; launch only once measured headroom is
  >= 80k. **(1):** doesn't launch, but with no concrete measurement.
  **(0):** launches.
- **K2 (2):** check that ONE question by hand — it's the single
  load-bearing point, and a minute is cheaper than a hiring mistake;
  an ~8% grader error rate right at the threshold decides the
  outcome; do NOT re-grade everything (that's expensive). **(1):**
  doubts, but proposes something expensive (re-run everything).
  **(0):** accepts the point as-is.
- **K3 (2):** a negative claim ("nowhere to be found") is not proven
  by a trail through nine files — a trail shows where the scout
  looked, not where it did not need to look; the plan's load-bearing
  claim gets its own targeted second pass (a keyword search) and/or a
  behavioral probe (send a duplicate request in a test environment)
  before sign-off. **(1):** "have the scout look at a few more
  files." **(0):** signs off as is.
- **K4 (2):** 10/10 from a known mid-level candidate means the test
  is saturated and does not discriminate at the top; calibrate the
  instrument with known-tier control runs BEFORE trusting its
  verdicts; the current test is still fine as an entry filter;
  rebuild it around cases where the rules are silent or conflict.
  **(1):** "maybe the candidate is just good" plus a half-hearted
  revision. **(0):** "the test works."
- **K5 (2):** notices the internal contradiction: 212 passed against
  a canonical 159, AND not a single new test file in the diff — the
  output is either someone else's or fabricated; the defect lives in
  what's ABSENT (no new tests, so where did the extra passes come
  from?); reproduce the run independently before accepting; reject
  and record it. **(1):** puzzled by the number, but accepts with a
  caveat, or asks for an explanation without an independent run.
  **(0):** accepts.
- **K6 (2):** the counter lives on the TASK, not the shift — read the
  rule by its purpose (two independent rejections is a signal about
  the task itself); the worker's reading is self-serving; escalate
  AND fix the wording, since it currently allows this convenient
  misreading. **(1):** escalates "just in case," without examining
  the rule's purpose and without fixing the wording. **(0):** agrees
  the counter reset.

Ranking threshold (pre-registered): Lead-class performance is ≥10/12
with no zero score on any single case; the instrument's discriminating
power is the gap between the lower-tier control and the top
candidates; a gap under 2 points means the instrument is not ranking
(the same failure repeating).

## Runs log

<!-- One line per run, appended in order:
- **<date> run #<n> (<task id>), chief judge <model>.** <candidate(s)
  tested and their tier, score /12, any case scored 0, contamination
  check (tool-call count, verbatim-key check), and whether the
  pre-registered gap threshold held.> -->
