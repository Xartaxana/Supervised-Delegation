# Current Context

## Maintenance Rule

This file holds LIVE state only. When a task or workstream closes,
move its detail out (a task report, an archive, or delete it if it
carries no lasting value) and leave at most a one-line pointer here.
This file is read on every boot — keep it short.

## Current Milestone

<!-- One line: the phase or goal currently driving decisions. -->

## Current Task

<!-- The single authoritative task in flight right now, with enough
     detail for a fresh session to resume it without asking. -->

## Queue

<!-- Next tasks in priority order, one line each. -->

## Lead Queue

<!-- Lead-tier work waiting for a lead-tier session. A coordinator
     whose actual model is BELOW the lead binding (see "Role ≠ tier"
     in CLAUDE.md) PUTS Lead-class work here instead of doing it:
     mechanism changes, decision-log entries, table statuses, symmetry
     axes for docs/SIBLING_MAP.md, acceptances it may not perform.
     One line each: what, why it's Lead-class, where the draft lives.
     Recognizing "this is Lead-tier" and then doing it yourself anyway
     is the recorded failure mode this section exists to prevent — the
     handoff needs a place to land, and this is that place. The next
     lead-tier session (or the operator's explicit word) works this
     queue and deletes served lines. -->

## Environment Notes

<!-- Machine- or account-specific facts a fresh session needs, so it
     doesn't have to re-discover them: local services, keys, quirks. -->
