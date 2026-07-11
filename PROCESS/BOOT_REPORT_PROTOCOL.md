# Boot Report Protocol

## Purpose

Every new LLM session must produce the same structured report after executing BOOT.md.

## Boot Report Template

```
BOOT REPORT

Repository Loaded: YES/NO

Working Tree at Boot: CLEAN / DIRTY (n files) / UNPUSHED (n commits)

Constitution Loaded: YES/NO

Decisions Loaded: YES/NO

Current Context Loaded: YES/NO

Current Phase:

Last Calibration:

Current Objective:

Next Required Action:

Confidence:
```

## Rules

1. The very first visible output of a new session is an announcement
   that the boot sequence is starting (one line, e.g. "Executing
   BOOT.md"), before any file is read.
2. The Boot Report is emitted as a separate block immediately after
   the BOOT.md documents are loaded — before any reasoning about,
   or execution of, the current task.
3. The report must be generated before proposing new work.
4. After the report, STOP and wait for the operator's explicit
   confirmation before starting any task. Boot recovery is not work
   authorization; neither BOOT.md's queue nor an unblocked task in
   CURRENT_CONTEXT.md overrides this stop.

5. Last Calibration = the timestamp of the most recent `calibrated`
   event in logs/routing-log.jsonl, or NONE. If routed traffic exists
   and more than 7 days have passed since that event (or since routed
   traffic began, when NONE), mark the line OVERDUE. This is the
   external detector for the calibration loop itself — the one
   mechanism whose absence calibration cannot detect.
6. Working Tree at Boot = `git status --short` plus the unpushed
   commit count at session start. DIRTY or UNPUSHED means the
   previous session ended without running the session-handoff check:
   record it as a finding, do not silently absorb it into the new
   session's work.

Rationale for 1–2: a session that starts with a silent series of
file reads buries the report in tool noise; the operator could not
tell whether context recovery had happened.

Rationale for 4: a session began executing the next queued task
immediately after its Boot Report; the operator wants to review the
recovered state and explicitly greenlight the task first. Autonomy
applies to executing a confirmed task, not to choosing when to start
one.
