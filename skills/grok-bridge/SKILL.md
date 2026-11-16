---
name: grok-bridge
description: Use the grok-mcp MCP server to run coding work in a local Grok Build session. Parked tool approvals must be shown to the human and answered only after they say so.
---
# Grok Build via the grok-bridge MCP server

The server is `grok_mcp.py`. It speaks MCP on stdio and keeps one `grok agent stdio` process per project directory. Tools:

- `new_session(cwd, title?, mode?)` — mode is `plan`, `default`, or `acceptEdits`
- `load_session(session_id, cwd)` — resume, including a TUI session when Grok allows `session/load`
- `prompt(session_id, text, wait_seconds?)` — assistant text, or a line starting with `still running` or `waiting for approval`
- `read(session_id, since?)` — `#N [user]` / `#N [assistant]`, then `last=#N`
- `pending_approvals()` — parked tool requests
- `respond_approval(request_id, option)` — `allow_once`, `allow_always`, or `reject`
- `cancel(session_id)`
- `list_sessions()`

Save `last=#N` and pass it as `since` next time.

## Approvals
If `prompt` or `read` says `waiting for approval`, stop. Call `pending_approvals` and show the human the request id, tool, title, kind, and options. Call `respond_approval` only after they clearly accept or refuse that action. Use `allow_always` only if they said to remember it. A later tool is a new request; ask again.

Do not pass `bypassPermissions` unless they asked. `plan` is for designing, not editing.
