# opencode-bridge

Bridges that let **Codex, Grok Bot, and other desktop agents** drive a local coding CLI and read back only numbered user and assistant text.

- **[opencode](https://opencode.ai)** — `opencode-bridge`, a bash + jq script (below).
- **[Grok Build](https://docs.x.ai/build)** (`grok`) — `grok_mcp.py`, a small MCP server that speaks ACP to `grok agent stdio` and parks tool approvals. See [Grok Build](#grok-build).

`opencode-bridge` is a single bash + jq script. Another agent can start an opencode session, send it instructions, and read back what it said, without having to parse opencode's full JSON or spend tokens on tool calls and reasoning. It prints only user messages and assistant text, and numbers every message so the calling agent never skips anything, including messages you type into the opencode TUI yourself.

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
curl -fsSL https://raw.githubusercontent.com/devonkinghorn/opencode-bridge/main/opencode-bridge -o ~/.local/bin/opencode-bridge
chmod +x ~/.local/bin/opencode-bridge
```

## Usage

```
opencode-bridge [-C dir] <session id | title substring> [options] [message...]
opencode-bridge [-C dir] --new [--title T] [options] <message...>
```

| Command | What it does |
|---|---|
| `opencode-bridge --new --title "auth refactor" --mode plan "plan the auth refactor"` | Start a new session in plan mode and print its reply and session id |
| `opencode-bridge "auth refactor" "now implement step 1"` | Send to the session whose title matches, wait, print everything after the message |
| `opencode-bridge <id> --no-wait "run the full test suite"` | Send and return immediately; collect the reply later with `--since` |
| `opencode-bridge <id> --timeout 120 "fix the failing test"` | Wait up to 120 s; if it's still working, print what's there and say so |
| `opencode-bridge <id> --since 41` | Every message after #41 |
| `opencode-bridge <id> --tail 5` / `--tail all` | Last 5 messages (default when only reading) / the whole chat |

Options:

- `--mode NAME` sets the opencode agent (mode) for the run, such as `build` or `plan`. It's passed to `opencode run --agent`. A session keeps its last mode until you pass a different one.
- `--model provider/model` sets the model for the run.
- `--full` also shows truncated reasoning and tool calls.
- `-C dir` sets the project directory, since opencode lists sessions per project.

Every call ends with `last=#N`. Pass that back as `--since N` next time, and you'll see anything that happened in between. For long jobs, use `--no-wait` or `--timeout` and check back with `--since`. The opencode run keeps going after the bridge returns.

## Use it from Grok Bot, Codex, or another agent

[`skills/opencode-bridge/SKILL.md`](skills/opencode-bridge/SKILL.md) is a ready-made skill that teaches an agent to drive opencode through this bridge.

- **Grok Bot:** paste this into a chat with your bot:
  > Save this as a skill: https://raw.githubusercontent.com/devonkinghorn/opencode-bridge/main/skills/opencode-bridge/SKILL.md

  Then ask things like "start an opencode session in plan mode to design the retry logic" or "check what my opencode session 'auth refactor' said since we last looked."
- **Codex, Claude Code, and other agents with skills:** the file uses the standard `SKILL.md` format (YAML frontmatter plus markdown), so you can drop it into that tool's skills folder. Or just tell the agent to run `opencode-bridge --help`.

## How it works

It reads `opencode session export <id>` and numbers messages by their position, and it sends with `opencode run` (`--session` to continue, `--agent` for the mode). Background runs are detached with `setsid nohup`, and their output goes to a temp log. It was written against opencode v2.2.0's export format. If a later version changes that format, adjust the jq filter.

## Grok Build

`grok_mcp.py` is one Python 3 file (stdlib only). It is an MCP server on stdio. It spawns one `grok agent stdio` process per project directory and turns MCP tool calls into ACP. Streamed text chunks are joined into numbered user and assistant messages.

For a turn to show up live in the Grok TUI, start the TUI with `grok --leader` (or set `[cli] use_leader = true` in `~/.grok/config.toml`). The server then spawns `grok --no-auto-update agent --leader stdio`, which attaches to `~/.grok/leader.sock`. `GROK_LEADER=0` uses `--no-leader` instead. `GROK_LEADER_SOCKET` is passed as `--leader-socket`.

Register it in Cursor (or any MCP client) as:

```json
{
  "mcpServers": {
    "grok": {
      "command": "python3",
      "args": ["/absolute/path/to/grok_mcp.py"]
    }
  }
}
```

`grok` should be on `PATH`, or set `GROK_BIN` to `~/.grok/bin/grok`. Run `grok login` once. Needs Python 3.9+.

Tools: `new_session`, `load_session`, `prompt`, `wait`, `events`, `read`, `pending_approvals`, `respond_approval`, `cancel`, `list_sessions`. `prompt` with `wait_seconds=0` returns `still running` immediately. That is not finished. Call `wait` (default 30 minutes) or poll `read` until `state=idle`. `wait` also returns when an approval is unresolved (`state=waiting`) or the timeout hits (`state=running` / `still running`). `events` is a numbered one-line feed (`tool`, `tool_status`, `approval`, `assistant`) with `last=#N`. After a prompt, `events` and `run --follow` start at that prompt and skip the `session/load` replay. Tool arguments in those lines are one line, cut at 120 characters. On grok 1.0.84 an approval is answered in the TUI, not by `respond_approval`. [`skills/grok-bridge/SKILL.md`](skills/grok-bridge/SKILL.md) is the short skill for a calling agent.

To run one turn from the shell and print progress as it happens:

```sh
python3 grok_mcp.py run --session <id> --cwd <dir> --text '...' --follow
```

### herdr

herdr (`hc`, [herdr.dev](https://herdr.dev)) runs Claude Code, Grok Build, Codex, or any terminal program in a persistent terminal you can watch and take over. The bridge drives it with `herdr_list`, `herdr_start`, `herdr_send`, `herdr_read`, `herdr_wait`, `herdr_interrupt`, and `herdr_keys`. `HC_BIN` overrides the binary; otherwise it uses `hc` on `PATH` or `~/.local/bin/hc`.

```sh
python3 grok_mcp.py herdr-run --terminal <t> --text '...' --follow --timeout 1800
```

`--follow` prints status changes and new screen lines (ANSI stripped) as they appear, then the screen tail. Exit 0 when the terminal is idle or done. A blocked permission dialog exits 3 and is not answered for you: approve it in the herdr app, `hc attach <t>` (Ctrl+\\ detaches), or an explicit `herdr_keys` call such as `down enter` for a trust prompt. `hc send --timeout` is a check-in; the bridge waits again until idle, done, blocked, error, or the overall timeout. If a send comes back `not_started`, it reads the screen once and retries once, then stops. Output is scraped from the screen, so it is incomplete while the agent is still working. Terminals are not restored after a reboot. A typed draft can hold a send for up to 60 seconds.

```sh
python3 -m unittest tests.test_grok_mcp -v
```

Checked against a live grok 1.0.84 client (leader reported 1.0.87) on a Mac:

- `grok --no-auto-update agent --leader stdio` plus a TUI opened with `grok --leader` shows `session/load` + `session/prompt` live in the TUI. `~/.grok/leader.sock` comes up with the leader. `[cli] use_leader = true` enables the same sharing.
- The client received no inbound JSON-RPC requests. Approvals arrive as `session/update` notifications: `tool_call` (title, rawInput), then `pending_interaction` (`tool_call_id`, `kind: "permission"`), then `interaction_resolved` (`tool_call_id`) when the TUI answers. `pending_approvals` and `read` show the title and raw input, and mark the row resolved.
- The agent's `ext_method` list (current grok-build source) has no `x.ai/interaction` or `resolve_interaction`. `respond_approval` still answers a real `session/request_permission` if one arrives. A `pending_interaction` has to be approved in the TUI.
- Assistant text often has no `messageId` and is one word per chunk. Those chunks append to the open assistant message.
- `session/set_mode` is still best-effort. Auth uses `xai.api_key` when `XAI_API_KEY` is set, else `cached_token`. `grok.com` login is not automated.

## License

MIT
