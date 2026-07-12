---
name: onboarding
description: First-run setup for a new install — one contour question, model-to-role binding, mandatory entrance exams for every bound model, journal/hook initialization, first Boot Report. Run once via either INSTALL.md path, and again whenever a new model is bound into an existing role. Invoking this skill is the operator's authorization for the setup work below.
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
   judgment calls later in this skill (a model swap in step 2, an
   exam-override decision in step 3) are being made without top-tier
   input. Don't block on this — just surface it before continuing.

1. **The question.** Ask verbatim: "Working on a Claude Code
   subscription, or on a set of API keys from different providers?"
   ("Both" is a valid answer.) Write the answer into
   `delegation.config.yaml`'s `contour` field (`subscription` |
   `api-keys` | `both`). Nothing else changes based on this answer
   except what follows below.

2. **Binding.** Walk the `roles` block of `delegation.config.yaml`
   with the operator, one role at a time (`lead`, `critic`, `builder`,
   `scout`, plus `judge` and `analyst` if the contour includes
   `api-keys` — those two are api-keys-only per the file's own
   comments): confirm the shipped default or let the operator replace
   it.
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

3. **Exams.** `delegation.config.yaml`'s `exam` block marks this
   mandatory, with failure overridable by explicit operator word.

   Exam dispatches are ordinary dispatches, and the journal duty
   starts here, not at the first "real" task: initialize the journal
   first (replace the `{SET_AT_INSTALL}` placeholder — the same init
   detailed in step 5), then record every exam dispatch as a
   `delegated` event and its scoring as `accepted`/`rejected` in
   `logs/routing-log.jsonl`. A dispatch discovered unjournaled later
   is repaired by the retroactive-entry rule in CLAUDE.md's journal
   section (append a marked retro pair now), never by inserting lines
   into the past.

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
   - **judge** (api-keys contour only): `gateway/judge_calibration.json`
     scored per `PROCESS/JUDGE_CALIBRATION_PROTOCOL.md`.
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

4. **Symmetry map seeding (existing projects — Path B installs).**
   The shipped `docs/SIBLING_MAP.md` carries only the toolkit's own
   two axes; an existing project arrives with symmetries of its own,
   and this is the one cheap moment to harvest them. Dispatch scout
   (the bound recon model) with the concrete question: "which
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

5. **Init.**
   - `git config core.hooksPath .githooks`.
   - Replace the `{SET_AT_INSTALL}` placeholder timestamp in
     `logs/routing-log.jsonl`'s seed `journal_created` line with the
     real install time (read the clock, don't narrate it) — if not
     already done before the exams in step 3.
   - Produce the first Boot Report per `BOOT.md`.
   - Show the operator, in one paragraph, "what pings you" — the four
     cases from `README.md`: two failed top-tier acceptances with
     nowhere to escalate; a budget/quota breach; a failed exam (at
     onboarding or on a later model swap); the weekly calibration
     digest. Everything else about this system runs in the background
     without a ping.

## Failure detector

The next Boot Report sees an unfilled `delegation.config.yaml` (blank
`api` fields under an `api-keys`/`both` contour, or a `lead` binding
never walked through step 2) or a journal whose `journal_created` line
still carries the literal `{SET_AT_INSTALL}` placeholder — either one
means this skill's steps were skipped or left unfinished. Two quieter
leaks with the same meaning: exam scores present in a Runs log while
the journal holds only `journal_created` (step 3's journaling was
skipped — repair by retro entries), and a Path B install whose
`docs/SIBLING_MAP.md` carries neither a project axis nor the explicit
dated "no confirmed project axes" line (step 4 was skipped).
