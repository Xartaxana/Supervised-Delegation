# Session Protocol

## Session Start

1. Connect the repository.
2. Follow BOOT.md.
3. Produce a Boot Report (PROCESS/BOOT_REPORT_PROTOCOL.md).
4. STOP; wait for the operator's confirmation before starting work.

## Session End

Before ending a session:

- Record new decisions.
- Update the roadmap if necessary.
- Update the architecture doc if necessary.
- Update current context; archive closed items out of it (a task
  report, an archive folder, or delete them if they carry no lasting
  value — see CURRENT_CONTEXT.md's own maintenance rule).
- Commit directly to git; fall back to a manually produced patch/diff
  only when direct repository access is unavailable.
- Run the session-handoff check (.claude/skills/session-handoff/):
  git clean and pushed, journal closed, boot budget measured, boot
  chain alive. Its report is the session's last output; the final
  commit+push is the session's last action.

No important knowledge should remain only inside chat history.
