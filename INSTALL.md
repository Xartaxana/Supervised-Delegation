# Install

Two paths. Both end at the onboarding skill, which asks the one
contour question and takes it from there.

## Path A — New project, from scratch

1. Use this repository as a template (GitHub's "Use this template"
   button), or clone it directly:
   `git clone <this-repo-url> my-project`
2. If you cloned instead of templating, drop the history you don't
   want to keep: `rm -rf .git && git init`.

   If your project folder already exists — empty, with `git init`
   already run in it — this step doesn't apply as written: clone the
   template into a temporary folder (or a subfolder) instead of
   directly into your project root, move its files into the root, and
   leave your existing `.git` alone (skip `git init`).
3. Retitle README.md for your own project if you want to — there's no
   literal placeholder token in it, just the toolkit's own name as a
   heading, so leaving it as-is is also fine. Update the copyright
   line in LICENSE if you're forking this publicly under your own
   name.
4. Continue to Onboarding, below.

## Path B — Into an existing project

1. Copy these paths into your project as-is: `SYSTEM_PROMPT.md`,
   `DECISIONS.md`, `DELEGATION_TABLE.md`, `CURRENT_CONTEXT.md`,
   `BOOT.md`, `delegation.config.yaml`, `.claude/agents/`,
   `.claude/skills/`, `.claude/settings.json`, `.githooks/`, `tools/`,
   `PROCESS/`, `logs/routing-log.jsonl`, `docs/SIBLING_MAP.md`. Add
   `gateway/` only if your contour answer (below) includes API keys.
2. If you already have a `CLAUDE.md`: don't overwrite it. Add the
   routing policy as a clearly marked block instead, so your existing
   instructions and the routing policy both survive:

   ```markdown
   <!-- BEGIN supervised-delegation policy (do not hand-edit below this
        line; re-sync by replacing the whole block) -->
   ...contents of this template's CLAUDE.md...
   <!-- END supervised-delegation policy -->
   ```

   If you have no `CLAUDE.md` yet, copy this template's file in as-is.
3. Point git's hooks path at the installed hooks:
   `git config core.hooksPath .githooks`
4. Initialize the routing journal: `logs/routing-log.jsonl` ships with
   a single seed line (`journal_created`) carrying a placeholder
   timestamp, `{SET_AT_INSTALL}`. Replace that placeholder with the
   real install time when you copy the file in. **Do this before your
   first commit of the journal**: once `core.hooksPath` points at
   `.githooks` (step 3, above), the pre-commit gate treats committed
   journal lines as append-only, so a placeholder that slips into a
   commit stops being an easy hand-edit.
5. Continue to Onboarding, below.

## Onboarding

Both paths converge here: run the onboarding skill
(`.claude/skills/onboarding/`). Invoking it is your authorization for
the setup work it performs. It asks one question — "Working on a
Claude Code subscription, or on a set of API keys from different
providers?" — writes the answer into `delegation.config.yaml`, runs
the entrance exam for each bound model, and produces your first Boot
Report. A failed exam doesn't block you — swap the model, or keep it
anyway; exam failures land in your decision log (`DECISIONS.md`), not
the routing journal.
