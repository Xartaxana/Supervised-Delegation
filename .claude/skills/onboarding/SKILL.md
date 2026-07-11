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
   Run the exam that matches each bound role:
   - **scout**: first run the `scout-exam-gen` skill (it writes a
     golden set tailored to this repo at `PROCESS/SCOUT_GOLDEN_SET.md`
     — see that skill for the method), then dispatch the resulting
     question set as an ordinary, unmarked task — don't tell the
     worker it's an exam, since that changes the behavior being
     measured — and score the answers against the key it produced.
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

4. **Init.**
   - `git config core.hooksPath .githooks`.
   - Replace the `{SET_AT_INSTALL}` placeholder timestamp in
     `logs/routing-log.jsonl`'s seed `journal_created` line with the
     real install time (read the clock, don't narrate it).
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
means this skill's steps were skipped or left unfinished.
