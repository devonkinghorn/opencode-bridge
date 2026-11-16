---
name: grok-bridge
description: Use the grok-mcp MCP server to run coding work in a local Grok Build session. Parked tool approvals must be shown to the human and answered only after they say so.
---
# Grok Build via the grok-bridge MCP server

The server is `grok_mcp.py`. It speaks MCP on stdio and keeps one `grok agent stdio` process per project directory. For the turn to appear live in the user's terminal, they start Grok with `grok --leader`, or set `[cli] use_leader = true` in `~/.grok/config.toml`. The server attaches to that leader (`~/.grok/leader.sock`).

Tools:

- `new_session(cwd, title?, mode?)` — mode is `plan`, `default`, or `acceptEdits`
- `load_session(session_id, cwd)` — resume, including a TUI session when Grok allows `session/load`
- `prompt(session_id, text, wait_seconds?)` — pass `wait_seconds: 0` on a long turn. The reply `still running` means the turn has not finished
- `wait(session_id, timeout_seconds?)` — block until `state=idle`, `state=waiting` (unresolved approval), or `state=running` on timeout (default 1800). Includes the assistant text since the prompt
- `events(session_id, since?)` — one progress line per tool call, tool completion/failure, approval, and finished assistant message, from the latest prompt onward (the load replay is skipped). Save `last=#N` and pass it as `since`
- `read(session_id, since?)` — `#N [user]` / `#N [assistant]`, then `last=#N`
- `pending_approvals()` — parked tool requests
- `respond_approval(request_id, option)` — `allow_once`, `allow_always`, or `reject`
- `cancel(session_id)`
- `list_sessions()`

Never treat `still running` as the answer. Call `wait` or poll `read` until `state=idle`, then report the assistant text. `state=waiting` means show the approval and let the human answer it in the TUI, then `wait` again.

`events` is how you relay intermediate progress (a command starting, a tool finishing) the way the TUI does. Message numbers from `read` and event numbers from `events` are separate cursors.

A shell one-shot, if you are not inside the MCP client: `python3 grok_mcp.py run --session <id> --cwd <dir> --text '...' --follow`. It prints each event as it happens and the final assistant text, and exits 0 only when the turn is idle.

## herdr

Use the herdr tools when the user wants to watch the agent in a real terminal, or when the agent is Claude Code (it cannot take a message in an existing interactive session). `herdr_start` runs `claude`, `grok`, `codex`, or another command in a terminal that stays up after this server exits. The user watches it in the herdr app or with `hc attach <t>` (Ctrl+\\ detaches).

- `herdr_send` waits out the whole turn. A timeout from `hc` is a check-in, not failure. `still running` means call `herdr_wait` or `herdr_send` again.
- If the status is `blocked`, stop. Show the screen text. The human approves in herdr. Do not press keys unless they tell you the exact keys; then `herdr_keys`.
- If a send is `not_started`, the text was not delivered. Read the screen. Do not paste the same task again in a loop.
- Screen text is scraped and can be incomplete while the agent is working. Terminals are not brought back after a reboot. A half-typed draft can delay a send by up to 60 seconds.

`python3 grok_mcp.py herdr-run --terminal <t> --text '...' --follow --timeout 1800` prints status changes and new screen lines, then the tail. Exit 0 only for idle or done.

## Approvals
If `prompt` or `read` says `waiting for approval`, stop. Call `pending_approvals` and show the human the request id, tool title, and raw input.

On grok 1.0.84 that approval is answered in the `grok --leader` TUI. `respond_approval` will say so. Do not invent a click in the TUI. Tell the human which tool is waiting and let them allow or reject it there. `read` again after `approval resolved`.

`respond_approval(request_id, option)` is only for a real `session/request_permission` (`allow_once`, `allow_always`, or `reject`). Use `allow_always` only if they said to remember it. Do not pass `bypassPermissions` unless they asked. `plan` is for designing, not editing.
