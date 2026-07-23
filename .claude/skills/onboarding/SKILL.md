---
name: onboarding
description: First-run setup for a new install — one contour question, a host-function inventory that forks greenfield/brownfield, an adoption ledger, model-to-role binding, mandatory entrance exams for every bound model (each its own dispatch), journal/hook initialization, first Boot Report. Run once via either INSTALL.md path, and again whenever a new model is bound into an existing role. Invoking this skill is the operator's authorization for the setup work below.
---

# onboarding — first-run setup

This skill orders the steps; the rules themselves live in CLAUDE.md
(auto-loaded) and in delegation.config.yaml's own comments. Nothing
here overrides either — if this skill and CLAUDE.md ever disagree,
CLAUDE.md wins and that's a bug report against this file.

## Steps

0. **Tier check.** Before doing anything else, compare the model
   actually running this skill against `roles.lead` in
   delegation.config.yaml (the subscription default ships bound to the
   top tier). If they match, proceed silently. If the running model is
   BELOW that binding, running onboarding from a lower tier is not
   forbidden (see CLAUDE.md, "Role ≠ tier" — coordination is not
   restricted to the top tier), but say so plainly: name the actual
   model and the bound lead model, and warn the operator that the
   judgment calls later in this skill (a model swap in step 4, an
   exam-override decision in step 5) are being made without top-tier
   input. Don't block on this — just surface it before continuing.

1. **The question.** Ask verbatim: "Working on a Claude Code
   subscription, or on a set of API keys from different providers?"
   ("Both" is a valid answer.) Write the answer into
   `delegation.config.yaml`'s `contour` field (`subscription` |
   `api-keys` | `both`). Nothing else changes based on this answer
   except what follows below.

   Headless / no live operator to ask (a scripted install, a
   validation run with no one to answer): default to `contour:
   subscription` and write an explicit marker alongside it — "not
   asked — environment clause" — in the same commit or in
   CURRENT_CONTEXT.md. This is the ONLY legitimate way to skip asking;
   a silent default with no marker is a question-routing violation
   (CLAUDE.md rule 11a — the apex of an underspecified requirement is
   the operator, and absorbing the question on their behalf needs an
   explicit, named exception for the environment, not a quiet guess).

2. **Intake inventory.** Before installing or binding anything, find
   out what the host project ALREADY does for the functions this kit
   provides — by function, not by this kit's own names for them (a
   host with no "scout" has no reason to recognize the word, but every
   project that ships anything has *some* answer, even an empty one,
   to "who accepts finished work"). Walk these five functions with the
   operator (or by reading the host repo yourself when there's no live
   operator to ask):

   | Function | The question |
   |---|---|
   | Acceptance | Who accepts finished work here, and by what criterion? |
   | Journal / accounting | Who and how records which tasks got done? |
   | Escalation | What happens on failure or a capability shortfall? |
   | Isolation | What keeps parallel workstreams from colliding? |
   | Calibration | Is there a periodic check of how routing/delegation performed? |

   Record the answers as a small table (blank/"—" is a valid, common
   answer, not a failure to find something). The result is a fork, not
   an extra decision to make: ALL FIVE rows blank → **greenfield** —
   there is nothing to compare against, every mechanism in the next
   step defaults to `adopt`, and this degenerate case is the kit's
   ordinary, fully-supported install shape, not a lesser path. ANY row
   non-blank → **brownfield** — that row's existing host mechanism gets
   mapped against the kit's equivalent in the ledger (step 3), instead
   of the kit's version being installed blind on top of it. Mixed
   projects (no agentic routing, but real CI/tests/journals of their
   own) fall on the same fork by the same rule — non-blank rows get
   mapped, blank rows default to adopt; there is no separate "mixed"
   procedure, only how many rows came back non-blank.

3. **Adoption ledger.** Copy `ADOPTION_LEDGER.template.md` into your
   project (e.g. `docs/ADOPTION_LEDGER.md`) and fill one row per kit
   mechanism, using step 2's inventory as the fork: a **greenfield**
   project (all five functions blank) fills every row `adopt` — the
   ledger is still created even though it's degenerate, because it's
   the future home for whatever this project grows into once it
   diverges from the shipped kit (see the template's own greenfield
   note for why the ledger's form makes that possible before any file
   copy even lands). A **brownfield** project maps each mechanism
   against whichever host function (step 2) it serves, and lands on
   one of the template's four statuses (`adopt` /
   `native-equivalent` / `deferred(<trigger>)` / `rejected`) per row —
   never a blank status.

   Before marking any row `adopt`, check it against this table: a
   mechanism installed WITHOUT its prerequisite is forbidden by
   construction — a missing prerequisite forces `deferred(<trigger>)`
   or `native-equivalent` instead, never a bare `adopt`.

   | Kit mechanism | Prerequisite | If missing |
   |---|---|---|
   | Leaf-routing judge acceptance (rule 13) | a calibrated judge — the labeled set in gateway/judge_calibration.json reproduced in full, per PROCESS/JUDGE_CALIBRATION_PROTOCOL.md, BEFORE any `accepted` carries `basis: "judge"` | `native-equivalent` (the host already has its own deterministic acceptance gate for that leaf class) or `deferred(trigger: calibration run)` |
   | Any hook-backed gate (mechanism_gate, dispatch_gate, dod_gate, main_gate, hygiene_gate, journal_echo) | `git config core.hooksPath` pointing at `.githooks`, wired into `.claude/settings.json` | `deferred(trigger: hooksPath + settings.json wired)` or `native-equivalent` (the host's own CI/lint gate already closes the same function) |
   | Escape-allowlist pre-commit check (escape_check) | a `tools/escape_allowlist.json` MUST exist before the first commit once hooksPath is wired — the check fails closed on a missing file. Minimum viable: `{"entries": []}` (passes, exit 0). The shipped `escape_allowlist.template.json` contains a deliberately-failing example: copy it only to REPLACE the example with real entries, or start from empty entries. Note: a terse one-line-per-decision log has no `## D-NNNN` sections to pin against — real entries need a sectioned decision document (yours, or a cross-repo absolute path) | `deferred(trigger: sectioned decision log exists)` with the empty-entries allowlist in place so the hook stays green |
   | Calibration / usage accounting (usage_report, savings_report, calibration_counts) | a carrier for prices/usage the tooling can read (cc_usage or an equivalent cost record) | `deferred(trigger: usage carrier configured)` |
   | Non-Claude worker guard (pi_run_guard) | an actual non-Claude worker in the contour (`api-keys`/`both`) | moot under `subscription`-only — `deferred(trigger: api-keys contour adopted)` |
   | Gateway / api-keys contour (judge, analyst, shadow-eval, budgets) | contour includes `api-keys` | `deferred(trigger: contour changed)` under `subscription`-only |

   A row with no plausible native-equivalent, no conflict, and no
   missing prerequisite is simply `adopt` — most rows, in most
   installs, land there; the table above exists to catch the ones that
   don't, not to make every row a negotiation.

4. **Binding.** Walk the `roles` block of `delegation.config.yaml`
   with the operator, one role at a time (`lead`, `critic`, `builder`,
   `scout`, plus `judge` and `analyst` if the contour includes
   `api-keys` — those two are api-keys-only per the file's own
   comments): confirm the shipped default or let the operator replace
   it. Skip binding a role whose step-3 ledger row came back anything
   other than `adopt` (a `deferred`/`native-equivalent`/`rejected`
   mechanism has no business being bound and exercised yet).
   - `subscription` (or `both`): roll the confirmed model into the
     `model:` frontmatter field of the matching `.claude/agents/*.md`
     profile. CLAUDE.md and the role profiles speak in function names
     only (lead/critic/builder/scout) — never hardcode a model name
     into their body text, only into that one frontmatter field.
   - `api-keys` (or `both`): generate `gateway/config.yaml` from its
     template, using the confirmed model names as the aliases for each
     role; remind the operator that each provider needs its
     `api_key_env` variable actually exported in the shell — this
     skill does not read or store API keys itself.
   Don't touch anything else in `delegation.config.yaml` — the file's
   comments are the spec for its own shape; if a role or field you
   expect to bind isn't there, that's a question back to the
   operator/Lead, not something to invent.

5. **Exams.** `delegation.config.yaml`'s `exam` block marks this
   mandatory, with failure overridable by explicit operator word.

   Exam dispatches are ordinary dispatches, and the journal duty
   starts here, not at the first "real" task: initialize the journal
   first (replace the `{SET_AT_INSTALL}` placeholder — the same init
   detailed in step 7), then record every exam dispatch as a
   `delegated` event and its scoring as `accepted`/`rejected` in
   `logs/routing-log.jsonl`. A dispatch discovered unjournaled later
   is repaired by the retroactive-entry rule in CLAUDE.md's journal
   section (append a marked retro pair now), never by inserting lines
   into the past.

   EACH exam below is its OWN coordinator dispatch — never all run
   inside the single session performing this skill. A worker session
   cannot launch other workers (flat delegation, CLAUDE.md rule 5), and
   every exam here requires actually dispatching a separate worker (a
   fresh scout/critic/judge run) to be measured honestly; a session
   that tried to "run the exams" as part of its own turn either faked
   the dispatch or skipped the exam and called it done — both are a
   finding, not a shortcut (a validation run hit exactly this: exams
   left unrun because they were structurally out of a single builder
   session's reach). If you are that single session and can't dispatch
   workers yourself, hand the exam list to the coordinator/operator as
   an explicit queued step — don't mark onboarding complete over an
   unrun exam.

   Retry discipline: a failed exam may be re-run unchanged from a
   fresh context (a re-roll), or re-run after a fix to the ROLE FILE
   (a regression re-run — mind the agent-definition cache: fresh
   session or inline delivery, noted). Adding hints to the dispatch
   ("watch for negative claims") invalidates the run as a measure of
   working behavior: a hinted run may guide your fix, and its result
   is recorded in the Runs log as `conditioned` — it is never the
   exam's PASS, and the keep-or-replace decision below uses the last
   unconditioned score.

   Run the exam defined for each role below; roles listed without one
   state the reason:
   - **scout**: first run the `scout-exam-gen` skill (it writes a
     golden set tailored to this repo at `PROCESS/SCOUT_GOLDEN_SET.md`
     — see that skill for the method), then dispatch the resulting
     question set as an ordinary, unmarked task — don't tell the
     worker it's an exam, since that changes the behavior being
     measured — and score the answers against the key it produced.
   - **builder**: no entrance exam, by design. Every builder diff is
     checked per-task by execution-based acceptance instead (the
     witness rule, CLAUDE.md rule 2) — a real verification run against
     that task's own spec, which is a stronger check than any one-time
     golden set.
   - **critic**: first run the `critic-exam-gen` skill (it writes a
     seeded-diff exam tailored to this repo at `PROCESS/CRITIC_EXAM.md`
     — see that skill for the method), then dispatch the resulting
     diff to the critic-bound model as an ordinary, unmarked review —
     don't tell it that it's an exam, since that changes the behavior
     being measured — and score the verdict against the key the skill
     produced.
   - **lead candidate** (when binding or swapping the model bound to
     `lead`): the vignettes in `PROCESS/LEAD_RANKING_EXAM.md`, scored
     against that protocol's own threshold.
   - **judge** (api-keys contour, OR a subscription judge-subagent
     under rule 13's second judge form): `gateway/judge_calibration.json`
     scored per `PROCESS/JUDGE_CALIBRATION_PROTOCOL.md` — full
     agreement (13/13 at the set's current count) before the judge is
     trusted for any real `accepted(basis: "judge")`; see that
     protocol's own subscription-subagent procedure for the
     verbatim-prompt requirement.
   - **any non-Claude worker**, on its first real run in a role: gate
     that first run through `tools/pi_run_guard.py` before trusting
     its output at all — a worker that fabricates tool calls is a
     capability failure, not a scoring nuance.

   On FAILURE, show this warning verbatim (fill in the brackets):
   "Model <model> failed the [<role>] exam (<score>). A stronger model
   is recommended." Then ask directly: replace the model, or keep it
   anyway. "Keep" requires the operator's own explicit word — never a
   silent default.

   If the operator says keep, exam failures land in your decision log,
   not the routing journal: append a line to `DECISIONS.md` (next free
   `D-NNNN`; `D-0001` on a virgin log), in that log's own one-line
   format:

   `- D-NNNN — Model <model> kept for role <role> despite a failed
   entrance exam (score <score>); explicit operator decision at
   onboarding.`

6. **Symmetry map seeding (existing projects — Path B installs).**
   The shipped `docs/SIBLING_MAP.md` carries only the toolkit's own
   two axes; an existing project arrives with symmetries of its own,
   and this is the one cheap moment to harvest them. This is a
   complement to step 2's inventory, not a repeat of it: step 2 asks
   about ROUTING FUNCTIONS (who accepts, who journals); this step asks
   about FILE-LEVEL pairs (which files must change together). Dispatch
   scout (the bound recon model) with the concrete question: "which
   genuinely PAIRED or MIRRORED structures exist in this repository —
   files/directories that must change together, where editing one
   side silently breaks the other?" Accept the digest by its trail.
   Do not ask the operator to enumerate symmetries from memory — the
   owner of a vibe-coded project may never have looked inside; the
   repository is surveyed, the operator JUDGES. Ask the operator only
   the one question scout cannot answer by construction: symmetries
   invisible from inside this repository (a second repository, an
   external deployment, a mirrored copy of this policy elsewhere).

   Tier split (Role ≠ tier, CLAUDE.md): DRAFTING candidate axes from
   the digest is coordinator work at any tier; DECIDING which
   candidates become tracked axes — and writing them into
   `docs/SIBLING_MAP.md` — is Lead-tier judgment, because every axis
   taxes every future mechanism commit with an enumeration line. If
   the model running this skill is below the lead binding, put the
   drafted candidates into CURRENT_CONTEXT.md's "Lead Queue" section
   and stop; the map write belongs to a lead-tier session or the
   operator's explicit word. Only pairs confirmed as real recurring
   duties become axes; zero confirmed project axes is a legal
   outcome, recorded as an explicit dated line in the map. Fresh
   empty projects (Path A): skip this step — nothing to map yet;
   axes arrive with the first real symmetry.

7. **Init.**
   - `git config core.hooksPath .githooks` (Path A installs: see
     INSTALL.md's own step 3, which asks for this BEFORE your first
     commit — don't leave it for this step if you already committed
     the installed files unguarded).
   - Replace the `{SET_AT_INSTALL}` placeholder timestamp in
     `logs/routing-log.jsonl`'s seed `journal_created` line with the
     real install time (read the clock, don't narrate it) — if not
     already done before the exams in step 5.
   - The operator's OWN session applies `.claude/settings.json`'s hook
     wiring by its own hand, in the target project — a worker session
     writing another project's hook config across a session/directory
     boundary runs into the harness's own permission boundary and gets
     blocked (a validation run hit exactly this: a cross-session write
     of a host's hook-config file was refused). Don't route this
     particular edit through a delegated session; do it from the
     session whose cwd is actually the target project.
   - Produce the first Boot Report per `BOOT.md` (see INSTALL.md's
     Onboarding section for the hand-assembly procedure when this
     session never crosses a real SessionStart boundary).
   - Show the operator, in one paragraph, "what pings you" — the four
     cases from `README.md`: two failed top-tier acceptances with
     nowhere to escalate; a budget/quota breach; a failed exam (at
     onboarding or on a later model swap); the weekly calibration
     digest. Everything else about this system runs in the background
     without a ping.

## Upgrade mode (existing deployment; D-0091)

Re-running this skill over a deployment that already carries an
adoption ledger is an UPGRADE, not a re-install. The unit of delivery
is the REVISION DELTA, never the full kit:

1. Read the kit snapshot revision recorded in the host's ledger
   (see the template's revision field). No revision recorded =
   pre-versioning install: record the current kit revision now and
   treat the whole ledger as the delta, once.
2. Build the delta: kit mechanisms new or changed since that
   revision — including the CONTENT of role files
   (`.claude/agents/*.md`), skills, tools and PROCESS docs, not just
   `model:` frontmatter. Check the host ledger's row set against the
   CURRENT template's nomenclature: a template row missing from the
   host ledger entirely is part of the delta (completeness, not just
   staleness — a dropped row hides forever otherwise).
3. Every delta item gets a ledger decision (adopt /
   native-equivalent / deferred / rejected) — at least deferred;
   silence is not a decision. An ADOPTED role-file content edit
   passes the host's own exam gate for that role BEFORE the commit
   (critic — seeded-diff exam; scout — golden set run; builder —
   witness-based acceptance by recorded kit decision).
4. Update the ledger's recorded kit revision in the same move. The
   cost is bounded by the delta and paid by the host (Rule #1); a
   full re-scan is the fallback only when no revision was recorded.
5. Sealed delivery for the enforcement chain (D-0093): a delta item
   that changes an EXECUTABLE control-chain file (a git hook, a hook
   script) ships the FULL target content of that file, never a
   "add this line" delta — a delta cannot carry the file's
   invariants (the `set -e` lesson of finding F-53). Applying it
   ends with a liveness PROBE: a known-invalid input is rejected by
   the gate, then the probe is reverted; the probe's witness goes
   into the batch's acceptance. Check hook executability while
   you're there: `git ls-files -s .githooks` must show `100755`
   (`100644` = a silently dead gate on Linux clones).

## Failure detector

The next Boot Report sees an unfilled `delegation.config.yaml` (blank
`api` fields under an `api-keys`/`both` contour, or a `lead` binding
never walked through step 4) or a journal whose `journal_created` line
still carries the literal `{SET_AT_INSTALL}` placeholder — either one
means this skill's steps were skipped or left unfinished. Two quieter
leaks with the same meaning: exam scores present in a Runs log while
the journal holds only `journal_created` (step 5's journaling was
skipped — repair by retro entries), and a Path B install whose
`docs/SIBLING_MAP.md` carries neither a project axis nor the explicit
dated "no confirmed project axes" line (step 6 was skipped). Three more,
specific to this version: no `ADOPTION_LEDGER.md` copy anywhere in the
project (step 3 was skipped entirely — including on a greenfield
install, where it's still mandatory); a ledger row marked `adopt` whose
own prerequisite (step 3's table) was never actually installed; and a
`contour` field with no accompanying "not asked — environment clause"
marker in a project where no operator was ever actually asked (step 1's
headless branch used silently instead of explicitly).
