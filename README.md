# opencode-tail

`oc-chat` is a tiny bash + jq wrapper that lets you (or another AI agent) talk to an existing [opencode](https://opencode.ai) session like a chat. It prints only user messages and assistant text, numbered, so a reader never skips anything and spends few tokens.

Example output (illustrative):

```
#41 [user]
run the tests and tell me what fails

#42 [assistant]
All 118 tests pass except `lock.test.ts > retries on timeout`, which expects 3 attempts and gets 2.

last=#43 session=ses_abc123
```

## Install

Requires `opencode`, `jq`, and bash.

```sh
curl -fsSL https://raw.githubusercontent.com/devonkinghorn/opencode-tail/main/oc-chat -o ~/.local/bin/oc-chat
chmod +x ~/.local/bin/oc-chat
```

## Usage

```
oc-chat [-C dir] <session id | title substring> [--tail N|all] [--since N] [--full] [message...]
```

| Command | What it does |
|---|---|
| `oc-chat "my feature" --tail 5` | Last 5 messages of the session whose title matches "my feature" (default when only reading) |
| `oc-chat <id> --tail all` | The whole chat |
| `oc-chat <id> --since 330` | Every message after #330 |
| `oc-chat <id> "run the tests"` | Send a message, wait for the run to finish, print everything after it |
| `--full` | Also show truncated reasoning and tool calls |
| `-C dir` | Project directory (opencode lists sessions per project) |

Every call ends with `last=#N`. Pass that back as `--since N` next time, and you'll see any messages typed into the session in between too.

## Use it from Grok Bot (or another AI agent)

[`skills/opencode-chat/SKILL.md`](skills/opencode-chat/SKILL.md) is a ready-made skill that teaches an agent to read and reply to your opencode sessions through `oc-chat`.

To add it in Grok Bot, paste this into a chat with your bot:

> Save this as a skill: https://raw.githubusercontent.com/devonkinghorn/opencode-tail/main/skills/opencode-chat/SKILL.md

Then just ask, for example "check what my opencode session 'auth refactor' said since we last looked" or "tell my opencode session to run the tests." The bot installs `oc-chat` where opencode runs if it isn't there yet. It keeps track of the last message number so it never skips anything you typed in between, and it only relays what you ask it to.

The skill file uses the standard `SKILL.md` format (YAML frontmatter plus markdown), so other agents that support skills, such as Claude Code (`~/.claude/skills/`), can use it as-is.

## How it works

It reads `opencode session export <id>` and numbers messages by their position, then uses `opencode run --session <id>` to send. It was written against opencode v2.2.0's export format. If a later version changes that format, adjust the jq filter.

## License

MIT
