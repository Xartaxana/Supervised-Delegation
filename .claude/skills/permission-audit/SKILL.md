---
name: permission-audit
description: Work through a wave of permission prompts without needing screenshots from the operator — find which commands (including subagents') needed confirmation and why, FIX the causes, and surface any wrong actions those prompts expose. Use this on a complaint about "a wave of prompts/notifications", on a request to "figure out why it keeps asking", proactively after a big dispatch run, and as an input to your own calibration routine if you run one.
---

# permission-audit — working through permission prompts

Confirmation dialogs themselves are not written into transcripts, but
every tool_use is, and the rules that gate a prompt (the allowlist plus
the harness's own auto-allow and sandbox heuristics) are knowable —
which means you can reconstruct which calls likely needed confirmation
without asking the operator to paste screenshots of the dialogs.
Script: `tools/permission_audit.py` (snapshots transcripts before
scanning, plus a MASKED-by-broad-allowlist detector).

**The audit has TWO goals of EQUAL weight, not one:**
1. **Cut the noise** — so legitimate, repeated commands stop asking every time.
2. **Surface wrong actions** — a permission prompt isn't only friction, it's a
   DETECTOR. An agent (or you yourself) did something off-pattern, and that's
   why the command didn't match the allowlist. More often than not, the prompt
   isn't "we forgot to allow this" but "this was done the wrong way": inventing
   a custom form instead of using the ready-made function/canonical command,
   editing a file around the Edit/Write tool, skipping a required journal
   field, running a manual poll loop where a check already exists for that. Do
   NOT skip straight past this to "add a pattern" — for every group, ask first:
   *why did this command take this shape at all, and was that the right call?*
   Only what was done CORRECTLY is safe to allow (silence). What was done
   wrong gets fixed at its source (the function/script/agent instruction), not
   legitimized in the allowlist.

## Steps

1. Run the audit: `python tools/permission_audit.py --minutes 180`
   (pick the window to fit the situation; `--all` for the whole history;
   `--summary` for a digest; `--session <id>` to filter by session). The
   script scans transcripts of the main session AND every subagent, runs
   the commands through the allowlist in `.claude/settings.json` /
   `settings.local.json`, the harness's built-in auto-allow, and sandbox
   heuristics, and prints the suspicious calls with a reason. The first
   block printed is **MASKED-BY-BROAD-ALLOWLIST** (step 3) — read it
   before the main list: broad rules silence some findings before you
   ever see them.

   **Snapshot caveat.** The script fixes the list of transcripts and
   their size BEFORE scanning and reads only those bytes — commands
   appended afterward (including by the audit run itself, or a parallel
   session) will not show up in the output; this is a deliberate
   trade-off (numbers don't drift mid-run).

2. **Diagnostic pass FIRST — look for wrong actions before silencing
   noise.** For every call: "silence it (done right, just not yet
   allowed)" or "fix at the source (done the wrong way)". Signal table,
   keyed to this template's own conventions (CLAUDE.md, command hygiene):

   | Signal in the transcript | Likely wrong action | Fix at the source |
   |---|---|---|
   | `python - <<EOF`, `python -c "...replace/open(...,'w')..."` on a tracked file | a file edit bypassing Edit/Write (hygiene point 4) | redo it through Edit/Write; remind the worker of the rule |
   | `printf`/`echo >>`/`cat <<EOF` into `logs/routing-log.jsonl` | a hand-built journal line instead of the Edit/Write tool (hygiene point 5); risk of missing required fields | write strictly via the Edit/Write tool; check the fields of lines already written |
   | ad-hoc `grep`/`cat`/`python -c` for reading/searching | Bash instead of a dedicated tool | Read/Grep/Glob instead of Bash |
   | a `cd X &&` prefix or a trailing ` 2>&1` on many calls | the form breaks the allowlist match (hygiene point 3) | don't prefix cwd, don't carry `2>&1` |
   | a differently-shaped `python -m pytest ...` on every run | the canonical form isn't being used | the canonical form from the repo root (point 1); a narrow target only when the task justifies it |
   | a manual poll loop (`while`/`for ... curl/sleep`) | a custom wait instead of the ready-made one | a named script under `tools/` if this repeats |
   | `git push` / installing packages / an edit outside the `owns` manifest, unasked | action outside the mandate — the prompt worked as a safety net | do NOT allow; investigate (an out-of-manifest write, the dispatch-context-manifest rule) |

   Everything in the right-hand column is a finding of its own, listed
   separately — that's what "done wrong" looks like. Only what's LEFT goes
   into the silencer in step 4.

3. **The MASKED-BY-BROAD-ALLOWLIST block is not about noise, it's a
   blind spot.** Rules shaped like `Bash(python *)`, `Bash(python -c '
   *)`, `Bash(python -m *)`, `Bash(bash -c *)` (including with an env
   prefix, `VAR=1 ...`) let through ARBITRARY execution: anything from
   the right-hand column of step 2 fits under them — and passes
   SILENTLY, never even reaching the suspects list. A rule like this is
   a finding in its own right, at the "wrong action" level: narrow it
   or remove it (narrowing means more confirmations — that's the
   operator's call, since these are their own accumulated
   Allow-always). NEVER add a pattern of this shape yourself (see
   step 4).

4. What's left — by cause, DIFFERENT fixes:
   - **"no allowlist match"**, a narrow repeated command → a wildcard in
     `permissions.allow` (`*` as a PREFIX, matched from the start of the
     string; a `cd` prefix breaks the match). NEVER an
     arbitrary-execution pattern (step 3).
   - **"multi-line / a loop / `$(...)`"** → the allowlist can't help
     (sandboxed as "cannot be statically analyzed"); move repeated
     logic into a named script under `tools/`.
   - **Your own ad-hoc coordinator reads** → Read/Grep/Glob; don't drag
     one-off commands into the allowlist.

5. **`.claude/settings.local.json` upkeep.** If your deployment keeps
   its whole `permissions.allow` list here (no versioned core list),
   working through the audit means working through this entire file:
   remove duplicate broad rules; collapse repeats into one wildcard;
   delete one-off investigation history; delete entries that legitimize
   the right-hand-column antipattern (an antipattern sitting in
   `allow` runs silently — the same blindness as MASKED, just local).
   End state: a short list of high-level rules. Confirm JSON validity
   after editing (`python -m json.tool`).

6. Summarize in TWO separate blocks (don't merge them): **Wrong actions
   (the main one)** — what, how many calls, what got fixed at the
   source; "nothing found" is also a result, say so explicitly.
   **Noise** — how many, top causes, what was added/narrowed/removed,
   entry counts before/after.

## Boundaries
- Never weaken security: no arbitrary-execution patterns, no `git
  push`, no package installs, no edits outside the `owns` manifest.
- The audit is heuristic: "Allow once" and an auto-allowed call are
  indistinguishable — the output says "likely needed confirmation," not
  an exact log.
- In a worktree session, the audit looks at the CANONICAL deployment's
  transcripts (a fixed project key), not the worktree's own — a known
  boundary of this design.

## An adjacent class this audit does NOT catch
A call-shape defect with no permission prompt at all: claiming an
object is ABSENT based on an empty/erroring output from a wrongly
invoked tool (`command not found`, an empty grep from its own bad
form). That's the "an environment negative needs verification" class
(command hygiene point 6): on spotting a negative claim in a transcript
with no positive check next to it — flag it as a right-hand-column
finding, even with no prompt attached. The primary detector for this
class is acceptance and calibration; this audit is a secondary net.
