---
name: opencode-bridge
description: Use this to do coding work through a local opencode coding agent, by starting a session or reading from and sending messages to an existing one, while using few tokens (only user and assistant text, numbered so no message is skipped).
---
# Drive opencode through `opencode-bridge`

`opencode-bridge` (https://github.com/devonkinghorn/opencode-bridge) prints only the user messages and assistant text from an opencode session. Each message is shown as `#N [user]` or `#N [assistant]`, and the output ends with `last=#N session=<id>`.

## Setup (once)
1. Find where the user's `opencode` runs. It might be their own computer, a VM (for example `limactl shell <vm> -- bash -lc '...'`), or your own computer.
2. If `opencode-bridge` isn't installed there, install it, with the user's go-ahead if it's their machine:
   `curl -fsSL https://raw.githubusercontent.com/devonkinghorn/opencode-bridge/main/opencode-bridge -o ~/.local/bin/opencode-bridge && chmod +x ~/.local/bin/opencode-bridge`
   It needs `jq` and bash.
3. opencode lists sessions per project, so run it from the project directory or pass `-C <dir>`.

## Usage
- To start a session, run `opencode-bridge --new --title "<short title>" [--mode plan|build] "<instructions>"`. Note the `session=` id it prints.
- To send to an existing session, run `opencode-bridge <id or title words> [--mode NAME] "message"`. It waits for the run, then prints everything after the message.
- For long jobs, add `--no-wait` to return right away, or `--timeout <seconds>` to wait a bounded time. Then check back later with `--since <last N>`. The run keeps going in the background.
- To catch up, run `opencode-bridge <id> --since <last N you saw>`. Always save the `last=#N` from each call and pass it back next time, so you don't miss messages the user typed into the session.
- `--tail 5` and `--tail all` show recent messages or the whole chat.
- `--full` also shows truncated reasoning and tool calls. Only use it when you need to see what opencode actually did.

## Steps
1. Decide whether you're starting a new session or continuing one. To find existing sessions, run `opencode session list` in the project dir.
2. Pick a mode: `plan` to design or review without editing files, or `build` to make changes. Use whichever mode the user asked for. A session keeps its last mode, so pass `--mode build` explicitly when you switch out of plan.
3. Read before you send to an existing session (`--since <last>` or `--tail 3`).
4. Send only what the user asked for. Keep the message short and self-contained, and say who it's from, for example "Message from <bot> on behalf of <user>: ...".
5. If your shell commands have a short time limit, use `--no-wait` or `--timeout` and poll with `--since`.
6. Report opencode's reply to the user in plain words, and remember the new `last=#N`.
7. Don't pass opencode's `--auto` or other permission-skipping flags unless the user asks.
