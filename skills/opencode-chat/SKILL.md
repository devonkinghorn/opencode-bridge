---
name: opencode-chat
description: Use this to read from or send a message to a local opencode coding-agent session, like another chat, while using few tokens (only user and assistant text, numbered so no message is skipped).
---
# Talk to an opencode session with `oc-chat`

`oc-chat` (https://github.com/devonkinghorn/opencode-tail) prints only the user messages and assistant text from an opencode session. Each message is shown as `#N [user]` or `#N [assistant]`, and the output ends with `last=#N session=<id>`.

## Setup (once)
1. Find where the user's `opencode` runs. It might be their own computer, a VM (for example `limactl shell <vm> -- bash -lc '...'`), or your own computer.
2. If `oc-chat` isn't installed there, install it, with the user's go-ahead if it's their machine:
   `curl -fsSL https://raw.githubusercontent.com/devonkinghorn/opencode-tail/main/oc-chat -o ~/.local/bin/oc-chat && chmod +x ~/.local/bin/oc-chat`
   It needs `jq` and bash.
3. opencode lists sessions per project, so run it from the project directory or pass `-C <dir>`.

## Usage
```
oc-chat [-C dir] <session id | title substring> [--tail N|all] [--since N] [--full] [message...]
```
- To read recent messages, run `oc-chat "<title words>" --tail 5`. Use `--tail all` for the whole chat.
- To catch up, run `oc-chat <id> --since <last N you saw>`. Always save the `last=#N` from each call and pass it back next time. That way you won't miss messages the user typed into the session in between.
- To send, run `oc-chat <id> "message"`. It waits for the opencode run to finish, then prints everything after the message, including opencode's reply.
- `--full` also shows truncated reasoning and tool calls. Only use it when you need to see what opencode actually did.

## Steps
1. Find the session. Run `opencode session list` in the project dir, or pass a title substring (it matches case-insensitively, and the first hit wins). Switch to the `ses_...` id once you know it.
2. Read before you send, using `--since <last>` or `--tail 3`.
3. Send only what the user asked you to relay. Keep the message short and self-contained, and say who it's from, for example "Message from <bot> on behalf of <user>: ...".
4. Report opencode's reply to the user in plain words, and remember the new `last=#N`.
5. Don't pass opencode's `--auto` or other permission-skipping flags unless the user asks.
