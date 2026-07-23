# Install

Two paths. Both end at the onboarding skill, which asks the one
contour question and takes it from there.

## Path A — New project, from scratch

1. Use this repository as a template (GitHub's "Use this template"
   button), or clone it directly:
   `git clone <this-repo-url> my-project`
2. If you cloned instead of templating, drop the history you don't
   want to keep: `rm -rf .git && git init`.

   #### Already have an empty git-initialized folder?

   If your project folder already exists — empty, with `git init`
   already run in it — this step doesn't apply as written: clone the
   template into a temporary folder (or a subfolder) instead of
   directly into your project root, move its files into the root, and
   leave your existing `.git` alone (skip `git init`).

   #### Source is a subdirectory with no `.git` of its own?

   The template you're copying from may itself be a subdirectory of a
   larger repository (e.g. a `toolkit/` folder inside a monorepo,
   carrying no `.git` of its own) rather than a standalone clonable
   repo. In that case there is nothing to clone — copy the
   subdirectory's files directly into your project root (a plain file
   copy, same file set as step 1's list for Path B) and continue from
   here exactly as if you had cloned and stripped history.
3. Point git's hooks path at the installed hooks BEFORE your first
   commit: `git config core.hooksPath .githooks`. This mirrors Path
   B's own step 3 below, and for the same reason: the commit-msg gate
   (`tools/mechanism_gate.py`) only fires once `core.hooksPath` points
   here, so setting it after your first commit lets that commit slip
   past the gate uninspected — see "A note on your first commit"
   below, which applies identically to both paths.
4. Retitle README.md for your own project if you want to — there's no
   literal placeholder token in it, just the toolkit's own name as a
   heading, so leaving it as-is is also fine. Update the copyright
   line in LICENSE if you're forking this publicly under your own
   name.
5. Continue to Onboarding, below.

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

## A note on your first commit

Your very first commit of the installed template touches mechanism
paths — `CLAUDE.md`, `.claude/agents/`, `PROCESS/`, etc. — since it's
installing them for the first time. Once `core.hooksPath` points at
`.githooks`, the commit-msg hook (`tools/mechanism_gate.py`) will stop
that commit and ask for either an axis-verdict block (one "axis N:
<verdict>" line per axis of `docs/SIBLING_MAP.md`) or an explicit skip
line in the commit message. Don't guess the format — the gate prints
its own requirement when it rejects the commit, and `docs/
SIBLING_MAP.md` lists the current axes. For an install commit, the
skip line is the appropriate route (you're installing shipped files
as-is, not changing a mechanism): use the exact phrase the gate
recognizes, `axes: not a mechanism (<reason>)`, filling in your own
`<reason>` (e.g. "installing the template's own shipped files").

## Hook executability and liveness (both paths; D-0093)

Git runs a hook only if its file is EXECUTABLE. On Linux/macOS a hook
committed with index mode `100644` is silently ignored: the gate
looks installed, `core.hooksPath` is set, the file is on disk — and
nothing ever fires. A broken gate is indistinguishable from a working
one by observed behavior (both let commits through), so check both
things explicitly right after step 3 of either path:

1. Committed modes: `git ls-files -s .githooks` must show `100755`
   for both hooks. If you see `100644`, fix it in the index and
   commit: `git update-index --chmod=+x .githooks/pre-commit
   .githooks/commit-msg`. The filesystem bit is not the thing that
   travels — clones inherit the INDEX mode.
2. Liveness by PROBE, not by reading the file: stage a deliberately
   invalid journal line, attempt a commit, confirm the gate REJECTS
   it, then revert the probe. "hooksPath set + file present" is not
   liveness.

The same rule applies to any later delivery that changes an
executable file of the enforcement chain: the carrier ships the FULL
target content of the file (a delta line cannot express the file's
invariants — the `set -e` lesson of finding F-53), and the delivery
ends with the same probe.

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

The first Boot Report is normally emitted by the SessionStart hook the
moment a fresh Claude Code session opens with its cwd at your project
root — but the session actually running onboarding may not cross that
boundary (a continuing session already open before the files landed,
a headless/scripted install run). When that's the case, don't skip
the report: assemble it BY HAND from the same facts a live hook run
would use — read BOOT.md's own file list plus PROCESS/
BOOT_REPORT_PROTOCOL.md's template, and fill it from what's actually on
disk (git status, DECISIONS.md's entry count, CURRENT_CONTEXT.md's
queue, and so on). Note in the report itself that it was hand-assembled
rather than hook-emitted, so a later reader doesn't mistake it for
evidence the hook fired.
