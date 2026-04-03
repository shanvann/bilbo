# HANDOFF.md - Session Handoff Protocol

When Warlord says **"let's continue in tui"** or **"let's continue in telegram"** (or any variation), do the following:

## Handoff Steps

1. **Save context** to `context/<topic>.md` with:
   - Current status / what we're doing
   - Key decisions made
   - Relevant file paths, URLs, data
   - Next steps
   - Any pending actions

2. **Name the file** based on the topic (e.g., `context/babybub-listing.md`, `context/deploy-fix.md`). Keep it descriptive and short.

3. **Tell the user** how to resume:
   - If switching to TUI: `openclaw tui --message "Read context/<topic>.md and continue"`
   - If switching to Telegram: Just message me on Telegram: "Read context/<topic>.md and continue"

4. **Clean up** old context files when a task is done (ask before deleting).

## Format for Context Files

```markdown
# <Topic>

## Status
What's happening right now

## Key Info
Data, paths, decisions, etc.

## Next Steps
1. What to do next
2. ...
```

## Notes
- Context files live in `context/` directory in the workspace
- Any session (TUI, Telegram, etc.) can read them
- The receiving session should read the file, then pick up seamlessly
- Don't duplicate MEMORY.md content — context files are for active tasks, memory is for long-term
