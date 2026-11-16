---
name: grok-bridge
description: Use the grok-mcp MCP server to run coding work in a local Grok Build session. Parked tool approvals must be shown to the human and answered only after they say so.
---
# Grok Build via the grok-bridge MCP server

The server is `grok_mcp.py`. It speaks MCP on stdio and keeps one `grok agent stdio` process per project directory. For the turn to appear live in the user's terminal, they start Grok with `grok --leader`, or set `[cli] use_leader = true` in `~/.grok/config.toml`. The server attaches to that leader (`~/.grok/leader.sock`).

Tools:

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
If `prompt` or `read` says `waiting for approval`, stop. Call `pending_approvals` and show the human the request id, tool title, and raw input.

On grok 1.0.84 that approval is answered in the `grok --leader` TUI. `respond_approval` will say so. Do not invent a click in the TUI. Tell the human which tool is waiting and let them allow or reject it there. `read` again after `approval resolved`.

`respond_approval(request_id, option)` is only for a real `session/request_permission` (`allow_once`, `allow_always`, or `reject`). Use `allow_always` only if they said to remember it. Do not pass `bypassPermissions` unless they asked. `plan` is for designing, not editing.
