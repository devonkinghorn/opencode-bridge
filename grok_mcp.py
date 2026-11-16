#!/usr/bin/env python3
"""MCP stdio server for Grok Build via ACP (`grok agent stdio`).

One grok process per cwd. Default spawn is `grok --no-auto-update agent --leader stdio`
so a TUI started with `grok --leader` shows the same turn. GROK_LEADER=0 uses
--no-leader. GROK_LEADER_SOCKET is passed as --leader-socket. GROK_BRIDGE_AGENT
replaces the argv (tests).

Observed on grok 1.0.84: no inbound session/request_permission. Approvals are
session/update pending_interaction / interaction_resolved, answered in the TUI.
The ext_method list has no x.ai/interaction or resolve_interaction. request_permission
is still handled if a build sends it.
"""

import argparse, json, os, re, shlex, shutil, subprocess, sys, threading, time
from pathlib import Path

MODES = ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan")
TOOLS = [
    ("new_session", "Create a Grok Build session.", ["cwd", "title", "mode"], ["cwd"]),
    ("load_session", "Load a session by id (TUI sessions too, if ACP allows).", ["session_id", "cwd"], ["session_id", "cwd"]),
    ("prompt", "Send text. wait_seconds=0 returns 'still running' immediately. Otherwise assistant text, 'still running', or 'waiting for approval'.", ["session_id", "text", "wait_seconds"], ["session_id", "text"]),
    ("wait", "Block until the turn is idle, waiting for approval, or timeout_seconds (default 1800). Returns state and assistant text since the prompt. 'still running' is not finished.", ["session_id", "timeout_seconds"], ["session_id"]),
    ("events", "One-line progress events since N: tool, tool status, approval, assistant. Ends with last=#N.", ["session_id", "since"], ["session_id"]),
    ("read", "Numbered user/assistant messages. since keeps n > since.", ["session_id", "since"], ["session_id"]),
    ("pending_approvals", "Parked approvals. Show these to the human before answering.", [], []),
    ("respond_approval", "Answer one approval: allow_once, allow_always, or reject.", ["request_id", "option"], ["request_id", "option"]),
    ("cancel", "Cancel the in-flight turn.", ["session_id"], ["session_id"]),
    ("list_sessions", "Sessions created or loaded in this process.", [], []),
    ("herdr_list", "List live herdr terminals (hc ls).", [], []),
    ("herdr_start", "Start claude, grok, codex, or a shell command in a herdr terminal. The first task is typed by hc run.", ["agent", "cwd", "name", "task"], ["agent", "cwd"]),
    ("herdr_send", "Paste text into a herdr terminal and wait. timeout_seconds is the overall budget (default 1800); hc check-ins are not failure. Does not auto-approve a blocked dialog.", ["terminal", "text", "wait", "timeout_seconds", "force"], ["terminal", "text"]),
    ("herdr_read", "Screen and history for a herdr terminal (ANSI stripped). Incomplete while it is working.", ["terminal", "lines"], ["terminal"]),
    ("herdr_wait", "Wait until a herdr terminal is idle, done, blocked, or error, or timeout_seconds (default 1800).", ["terminal", "timeout_seconds"], ["terminal"]),
    ("herdr_interrupt", "Send the stop key to a herdr terminal (hc interrupt).", ["terminal"], ["terminal"]),
    ("herdr_keys", "Press named keys in a herdr terminal. Only after the human says which keys.", ["terminal", "keys"], ["terminal", "keys"]),
]


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if isinstance(content, list):
        return "".join(_text(part) for part in content)
    return ""


def _argv(mode):
    if os.environ.get("GROK_BRIDGE_AGENT"):
        return shlex.split(os.environ["GROK_BRIDGE_AGENT"])
    binary = os.environ.get("GROK_BIN") or str(Path.home() / ".grok" / "bin" / "grok")
    if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        binary = "grok"
    chosen = mode if mode in MODES else "default"
    leader = os.environ.get("GROK_LEADER", "1") != "0"
    argv = [binary, "--no-auto-update", "--permission-mode", chosen, "agent", "--leader" if leader else "--no-leader", "stdio"]
    sock = os.environ.get("GROK_LEADER_SOCKET")
    if leader and sock:
        argv.extend(["--leader-socket", sock])
    return argv


class Acp:
    def __init__(self, cwd, mode, hub):
        self.cwd, self.mode, self.hub = cwd, mode if mode in MODES else "default", hub
        self.proc = subprocess.Popen(_argv(self.mode), cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.lock, self.next_id, self.wait = threading.Lock(), 1, {}
        threading.Thread(target=self._read, daemon=True).start()

    def alive(self):
        return self.proc.poll() is None

    def request(self, method, params, timeout=30, before=None):
        _rid, event, box = self.send(method, params, before=before)
        if not event.wait(timeout):
            raise TimeoutError(method)
        if "error" in box:
            err = box["error"]
            raise RuntimeError(err.get("message") if isinstance(err, dict) else err)
        return box.get("result")

    def send(self, method, params, before=None):
        with self.lock:
            rid, event, box = self.next_id, threading.Event(), {}
            self.next_id += 1
            self.wait[rid] = (event, box)
        if before:
            before(rid)
        self.write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid, event, box

    def notify(self, method, params):
        self.write({"jsonrpc": "2.0", "method": method, "params": params})

    def write(self, payload):
        data = (json.dumps(payload) + "\n").encode()
        with self.lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _read(self):
        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("method") and msg.get("id") is not None:
                self.hub.on_request(self, msg)
            elif msg.get("method"):
                self.hub.on_note(msg)
            elif "id" in msg:
                with self.lock:
                    pending = self.wait.pop(msg["id"], None)
                if pending:
                    event, box = pending
                    box["error" if "error" in msg else "result"] = msg.get("error", msg.get("result"))
                    event.set()
                self.hub.on_result(self, msg.get("id"))

    def handshake(self):
        result = self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "grok-mcp", "version": "0.1.0"},
        }) or {}
        methods = [item.get("id") if isinstance(item, dict) else item for item in result.get("authMethods") or []]
        choice = "xai.api_key" if os.environ.get("XAI_API_KEY") and "xai.api_key" in methods else None
        choice = choice or ("cached_token" if "cached_token" in methods else None)
        if not choice and methods == ["grok.com"]:
            raise RuntimeError("run `grok login` first (grok.com OAuth is interactive)")
        if choice:
            self.request("authenticate", {"methodId": choice})


class Session:
    def __init__(self, sid, cwd, title, agent):
        self.id, self.cwd, self.title, self.agent = sid, cwd, title, agent
        self.messages, self.by_id, self.tools, self.events = [], {}, {}, []
        self.state, self.prompting, self.prompt_rid, self.pending = "idle", False, None, []
        self.turn_start, self.live_from, self.hub, self.cv = 0, 0, None, threading.Condition()
        self.replaying, self.load_rid = False, None

    def add(self, role, text, mid):
        # Chunks with no messageId append to the open message of the same role
        # (grok 1.0.84 streams one word per agent_message_chunk).
        if mid and mid in self.by_id:
            self.by_id[mid]["text"] += text
            return
        if not mid and self.messages and self.messages[-1].get("open") and self.messages[-1]["role"] == role:
            self.messages[-1]["text"] += text
            return
        self.close_assistant()
        msg = {"n": (self.messages[-1]["n"] + 1) if self.messages else 1, "role": role, "text": text, "open": not mid, "replay": bool(self.replaying)}
        self.messages.append(msg)
        if mid:
            self.by_id[mid] = msg

    def close_assistant(self):
        if not self.messages:
            return
        last = self.messages[-1]
        if last["role"] != "assistant" or last.get("done"):
            return
        last["open"], last["done"] = False, True
        snippet = " ".join(str(last["text"]).split())
        if len(snippet) > 160:
            snippet = snippet[:160] + "…"
        if snippet and self.hub:
            self.hub._event(self, "assistant " + snippet, replay=bool(last.get("replay")))

    def last_n(self):
        return self.messages[-1]["n"] if self.messages else 0


class Hub:
    def __init__(self):
        self.agents, self.sessions, self.perms = {}, {}, {}
        self.perm_n, self.map = 0, threading.Lock()

    def call(self, name, args):
        if name not in {t[0] for t in TOOLS}:
            raise RuntimeError(f"unknown tool {name}")
        if name.startswith("herdr_"):
            return _herdr_tool(name, args or {})
        fn = getattr(self, name, None)
        if not fn:
            raise RuntimeError(f"unknown tool {name}")
        return fn(**(args or {}))

    def new_session(self, cwd, title="", mode=""):
        agent = self.agent(cwd, mode)
        result = agent.request("session/new", {"cwd": agent.cwd, "mcpServers": [], "_meta": {"title": title} if title else {}}, 60) or {}
        sid = str(result.get("sessionId") or "")
        if not sid:
            raise RuntimeError("session/new returned no sessionId")
        self.sessions[sid] = Session(sid, agent.cwd, title, agent)
        self.sessions[sid].hub = self
        note = ""
        if mode:
            try:
                agent.request("session/set_mode", {"sessionId": sid, "modeId": mode}, 10)
            except (RuntimeError, TimeoutError) as exc:
                note = f" (session/set_mode failed: {exc})"
        return f"session={sid} cwd={agent.cwd} title={title or '-'} state=idle{note}"

    def load_session(self, session_id, cwd):
        got = self.sessions.get(session_id)
        if got:
            return f"session={got.id} cwd={got.cwd} state={got.state} last=#{got.last_n()}"
        agent = self.agent(cwd, "")
        self.sessions[session_id] = Session(session_id, agent.cwd, "", agent)
        self.sessions[session_id].hub = self
        sess = self.sessions[session_id]
        sess.replaying = True
        try:
            agent.request(
                "session/load",
                {"sessionId": session_id, "cwd": agent.cwd, "mcpServers": []},
                60,
                before=lambda rid: setattr(sess, "load_rid", rid),
            )
        finally:
            sess.replaying = False
        return f"session={session_id} cwd={agent.cwd} state=idle last=#{self.sessions[session_id].last_n()}"

    def prompt(self, session_id, text, wait_seconds=None):
        sess = self._sess(session_id)
        wait = 30.0 if wait_seconds is None else float(wait_seconds)
        with sess.cv:
            if sess.prompting:
                raise RuntimeError("session is already running")
            sess.add("user", text, f"user-{time.time_ns()}")
            sess.prompting, sess.state = True, "running"
            start = sess.turn_start = sess.last_n()
            sess.live_from = sess.events[-1]["n"] if sess.events else 0
        _rid, event, box = sess.agent.send(
            "session/prompt", {"sessionId": sess.id, "prompt": [{"type": "text", "text": text}]},
            before=lambda rid: setattr(sess, "prompt_rid", rid),
        )
        if wait <= 0:
            return "still running"
        deadline = time.time() + wait
        while True:
            with sess.cv:
                if sess.pending:
                    return self._waiting(sess, start)
                if event.is_set():
                    break
                if time.time() >= deadline:
                    return "still running" + (("\n" + self._since(sess, start)) if self._since(sess, start) else "")
                sess.cv.wait(0.2)
        if "error" in box:
            raise RuntimeError(box["error"])
        return self._since(sess, start) or "(no assistant text)"

    def wait(self, session_id, timeout_seconds=None):
        """Block until idle, an unresolved approval, or timeout. 'still running' is not done."""
        sess = self._sess(session_id)
        deadline = time.time() + (1800.0 if timeout_seconds is None else float(timeout_seconds))
        while True:
            with sess.cv:
                if sess.pending:
                    return "state=waiting\n" + self._waiting(sess, sess.turn_start)
                if not sess.prompting and sess.state != "running":
                    text = self._since(sess, sess.turn_start)
                    return "state=idle" + (f"\n{text}" if text else "")
                if time.time() >= deadline:
                    text = self._since(sess, sess.turn_start)
                    return "state=running\nstill running" + (f"\n{text}" if text else "")
                sess.cv.wait(0.2)

    def events(self, session_id, since=None):
        sess = self._sess(session_id)
        # Default cursor is the prompt, so a load replay is not the turn.
        floor = sess.live_from if since in (None, "") else int(since)
        lines = [f"#{e['n']} {e['line']}" for e in sess.events if e["n"] > floor and not e.get("replay")]
        last = sess.events[-1]["n"] if sess.events else 0
        lines.append(f"last=#{last}")
        return "\n".join(lines)

    def read(self, session_id, since=None):
        sess, floor = self._sess(session_id), int(since) if since not in (None, "") else 0
        lines = [f"#{m['n']} [{m['role']}]\n{m['text'].strip()}" for m in sess.messages if m["n"] > floor and str(m["text"]).strip()]
        for perm in self.perms.values():
            if perm["session"] != sess.id:
                continue
            if perm.get("resolved"):
                lines.append(f"approval resolved: {perm['id']} {perm['title']}")
            elif perm["id"] in sess.pending:
                lines.append(f"waiting for approval: {perm['id']} {perm['title']}")
        lines.append(f"last=#{sess.last_n()} session={sess.id}")
        return "\n".join(lines)

    def pending_approvals(self):
        if not self.perms:
            return "no pending approvals"
        blocks = []
        for perm in self.perms.values():
            opts = ", ".join(f"{o.get('kind')}={o.get('optionId')}" for o in perm["options"]) or "-"
            raw = perm.get("raw")
            raw_s = raw if isinstance(raw, str) else (json.dumps(raw) if raw is not None else "-")
            if len(raw_s) > 300:
                raw_s = raw_s[:300] + "…"
            state = "resolved" if perm.get("resolved") else "pending"
            blocks.append(
                f"{perm['id']} status={state} session={perm['session']} tool={perm['tool'] or '-'} "
                f"kind={perm['kind'] or '-'} title={perm['title'] or '-'}\n  raw: {raw_s}\n  options: {opts}"
            )
        return "\n".join(blocks)

    def respond_approval(self, request_id, option):
        perm = self.perms.get(request_id)
        if not perm:
            raise RuntimeError(f"no pending approval {request_id}")
        if perm.get("resolved"):
            return f"{request_id} already resolved"
        if not perm.get("rpc"):
            return (
                f"{request_id} ({perm['title']}) is answered in the grok --leader TUI. "
                "No x.ai/interaction or resolve_interaction method is in the agent ext_method list."
            )
        kind = {"allow_once": "allow_once", "allow_always": "allow_always", "reject": "reject_once"}.get(option)
        if not kind:
            raise RuntimeError("option must be allow_once, allow_always, or reject")
        option_id = next((o.get("optionId") for o in perm["options"] if o.get("kind") == kind), None)
        if option_id:
            result = {"outcome": {"outcome": "selected", "optionId": option_id}}
        elif option == "reject":
            result, option_id = {"outcome": {"outcome": "cancelled"}}, "cancelled"
        else:
            raise RuntimeError(f"{request_id} has no {kind} option")
        self._drop(perm)
        perm["agent"].write({"jsonrpc": "2.0", "id": perm["rpc"], "result": result})
        return f"answered {request_id} {option_id}"

    def cancel(self, session_id):
        sess = self._sess(session_id)
        try:
            sess.agent.notify("session/cancel", {"sessionId": sess.id})
        except OSError:
            pass
        for pid in list(sess.pending):
            perm = self.perms.get(pid)
            if perm:
                self._drop(perm)
                if perm.get("rpc") is not None:
                    perm["agent"].write({"jsonrpc": "2.0", "id": perm["rpc"], "result": {"outcome": {"outcome": "cancelled"}}})
        with sess.cv:
            sess.prompting, sess.state = False, "idle"
            sess.cv.notify_all()
        return f"cancelled session={sess.id}"

    def list_sessions(self):
        if not self.sessions:
            return "no sessions"
        return "\n".join(f"{s.id}\t{s.state}\tlast=#{s.last_n()}\t{s.cwd}\t{s.title}" for s in self.sessions.values())

    def on_note(self, msg):
        if msg.get("method") != "session/update":
            return
        params, update = msg.get("params") or {}, (msg.get("params") or {}).get("update") or msg.get("params") or {}
        sess = self.sessions.get(str(params.get("sessionId") or update.get("sessionId") or ""))
        if not sess:
            return
        kind, mid = update.get("sessionUpdate") or "", update.get("messageId")
        with sess.cv:
            if kind == "agent_message_chunk":
                sess.add("assistant", _text(update.get("content")), str(mid) if mid else None)
            elif kind == "user_message_chunk" and not sess.prompting:
                sess.add("user", _text(update.get("content")), str(mid) if mid else None)
            elif kind == "tool_call":
                sess.close_assistant()
                self._remember_tool(sess, update)
                tid = str(update.get("toolCallId") or update.get("tool_call_id") or "")
                info = sess.tools.get(tid, {})
                self._event(sess, f"tool {info.get('title') or 'tool'} {self._short(info.get('raw'))}".rstrip())
            elif kind == "tool_call_update":
                status = str(update.get("status") or "").lower()
                if status in ("completed", "failed"):
                    tid = str(update.get("toolCallId") or update.get("tool_call_id") or "")
                    title = (sess.tools.get(tid) or {}).get("title") or tid or "tool"
                    self._event(sess, f"tool_status {title} {status}")
            elif kind == "pending_interaction":
                self._park_notice(sess, update)
            elif kind == "interaction_resolved":
                self._resolve_notice(sess, update)
            sess.cv.notify_all()

    def on_request(self, agent, msg):
        if msg.get("method") != "session/request_permission":
            agent.write({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": "not implemented"}})
            return
        params, tool = msg.get("params") or {}, (msg.get("params") or {}).get("toolCall") or {}
        meta = ((tool.get("_meta") or {}).get("x.ai/tool") or {}) if isinstance(tool, dict) else {}
        raw = tool.get("rawInput") if isinstance(tool, dict) else None
        name = str(meta.get("name") or (raw.get("name") if isinstance(raw, dict) else "") or "")
        self.perm_n += 1
        public = f"req-{self.perm_n}"
        perm = {
            "id": public, "rpc": msg.get("id"), "session": str(params.get("sessionId") or ""), "agent": agent,
            "tool": name, "kind": str((tool.get("kind") if isinstance(tool, dict) else "") or meta.get("kind") or ""),
            "title": str((tool.get("title") if isinstance(tool, dict) else "") or name or "tool"),
            "raw": raw, "options": [o for o in (params.get("options") or []) if isinstance(o, dict)],
        }
        self.perms[public] = perm
        sess = self.sessions.get(perm["session"])
        if sess:
            with sess.cv:
                sess.pending.append(public)
                sess.state = "waiting"
                sess.cv.notify_all()

    def on_result(self, agent, request_id):
        for sess in list(self.sessions.values()):
            if sess.agent is not agent:
                continue
            with sess.cv:
                if getattr(sess, "load_rid", None) == request_id:
                    sess.replaying = False
                    sess.load_rid = None
                if sess.prompt_rid == request_id:
                    sess.close_assistant()
                    sess.prompting, sess.prompt_rid = False, None
                    sess.state = "waiting" if sess.pending else "idle"
                sess.cv.notify_all()

    def agent(self, cwd, mode):
        path = str(Path(cwd).expanduser().resolve())
        with self.map:
            found = self.agents.get(path)
            if found and found.alive():
                return found
            proc = Acp(path, mode, self)
            self.agents[path] = proc
        try:
            proc.handshake()
        except Exception:
            proc.proc.kill()
            self.agents.pop(path, None)
            raise
        return proc

    def _event(self, sess, line, replay=None):
        line = " ".join(str(line).split())
        if replay is None:
            replay = bool(sess.replaying)
        n = sess.events[-1]["n"] + 1 if sess.events else 1
        sess.events.append({"n": n, "line": line, "replay": replay})

    def _short(self, raw):
        if isinstance(raw, dict):
            for key in ("command", "cmd", "path"):
                if raw.get(key):
                    raw = raw[key]
                    break
            else:
                raw = json.dumps(raw, separators=(",", ":"))
        return " ".join(str(raw or "").split())[:120]

    def _sess(self, session_id):
        sess = self.sessions.get(session_id)
        if not sess:
            raise RuntimeError(f"no session {session_id}")
        return sess

    def _remember_tool(self, sess, update):
        tid = str(update.get("toolCallId") or update.get("tool_call_id") or "")
        meta = ((update.get("_meta") or {}).get("x.ai/tool") or {}) if isinstance(update.get("_meta"), dict) else {}
        sess.tools[tid] = {
            "title": str(update.get("title") or ""),
            "raw": update.get("rawInput") if "rawInput" in update else update.get("raw_input"),
            "name": str(meta.get("name") or ""),
            "kind": str(update.get("kind") or meta.get("kind") or ""),
        }

    def _park_notice(self, sess, update):
        tid = str(update.get("tool_call_id") or update.get("toolCallId") or "")
        info = sess.tools.get(tid, {})
        if any(p.get("tool_call_id") == tid and not p.get("resolved") for p in self.perms.values()):
            return
        self.perm_n += 1
        public = f"req-{self.perm_n}"
        self.perms[public] = {
            "id": public, "rpc": None, "session": sess.id, "agent": sess.agent, "tool_call_id": tid,
            "tool": info.get("name") or "", "kind": str(update.get("kind") or "permission"),
            "title": info.get("title") or tid or "tool", "raw": info.get("raw"), "options": [],
        }
        sess.pending.append(public)
        sess.state = "waiting"
        self._event(sess, f"approval {self.perms[public]['title']}")

    def _resolve_notice(self, sess, update):
        tid = str(update.get("tool_call_id") or update.get("toolCallId") or "")
        for perm in self.perms.values():
            if perm.get("session") == sess.id and perm.get("tool_call_id") == tid and not perm.get("resolved"):
                perm["resolved"] = True
                sess.pending = [item for item in sess.pending if item != perm["id"]]
                sess.state = "running" if sess.prompting else "idle"
                self._event(sess, f"approval_resolved {perm['title']}")

    def _drop(self, perm):
        self.perms.pop(perm["id"], None)
        sess = self.sessions.get(perm["session"])
        if not sess:
            return
        with sess.cv:
            sess.pending = [item for item in sess.pending if item != perm["id"]]
            sess.state = "running" if sess.prompting else "idle"
            sess.cv.notify_all()

    def _since(self, sess, start):
        return "\n".join(m["text"].strip() for m in sess.messages if m["role"] == "assistant" and m["n"] > start and str(m["text"]).strip())

    def _waiting(self, sess, start):
        bits = [f"waiting for approval: {pid} {self.perms[pid]['title']}" for pid in sess.pending if pid in self.perms]
        extra = self._since(sess, start)
        return "\n".join(bits + ([extra] if extra else []))


_HC_STATES = ("idle", "working", "blocked", "error", "done")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _hc_bin():
    override = os.environ.get("HC_BIN")
    if override:
        return override
    found = shutil.which("hc")
    if found:
        return found
    home = Path.home() / ".local" / "bin" / "hc"
    return str(home) if home.is_file() else "hc"


def _hc(args, timeout=None):
    proc = subprocess.run(
        [_hc_bin(), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=None if timeout is None else timeout + 15,
    )
    out = proc.stdout or ""
    data = None
    raw = out.strip()
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
    if proc.returncode != 0 and data is None:
        raise RuntimeError((proc.stderr or out or f"hc exited {proc.returncode}").strip())
    return data if data is not None else out


def _hc_state(data):
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if isinstance(status, dict):
        state = status.get("state") or status.get("status")
    elif isinstance(status, str):
        state = status
    else:
        state = data.get("state") if data.get("state") in _HC_STATES else None
    if state in _HC_STATES:
        return state
    for key in ("terminal", "record"):
        if isinstance(data.get(key), dict):
            found = _hc_state(data[key])
            if found:
                return found
    return None


def _hc_not_started(data):
    if isinstance(data, dict):
        if data.get("result") == "not_started" or data.get("phase") == "not_started":
            return True
        return "not_started" in json.dumps(data)
    return "not_started" in str(data or "")


def _hc_checkin(data, state):
    if state in ("idle", "done", "blocked", "error"):
        return False
    if isinstance(data, dict) and (data.get("checked_in") or data.get("check_in") or data.get("timed_out")):
        return True
    return state in ("working", None)


def _strip_ansi(text):
    return _ANSI.sub("", text or "")


def _hc_name(name):
    cleaned = re.sub(r"[^a-z0-9_-]", "-", str(name or "agent").lower()).strip("-")[:32]
    return cleaned or "agent"


def _herdr_read_text(terminal, lines):
    data = _hc(["read", str(terminal), "--lines", str(int(lines or 80))])
    return _strip_ansi(data if isinstance(data, str) else json.dumps(data))


def _herdr_status(terminal):
    return _hc_state(_hc(["get", str(terminal), "--json"]))


def _blocked_text(terminal, state):
    screen = _herdr_read_text(terminal, 80)
    return (
        f"status={state} terminal={terminal}\n"
        "The human should approve this in the herdr app, or say which keys to press. "
        "Do not guess a permission dialog.\n"
        + screen
    )


def herdr_list():
    data = _hc(["ls", "--json"])
    rows = data.get("terminals") if isinstance(data, dict) else data
    if isinstance(data, dict) and rows is None:
        rows = data.get("items") or []
    if not isinstance(rows, list):
        rows = []
    if not rows:
        return "no terminals"
    lines = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        addr = row.get("address") or row.get("name") or "?"
        lines.append(
            f"{addr}\tname={row.get('name') or '-'}\tstatus={_hc_state(row) or '-'}\tcwd={row.get('cwd') or '-'}"
        )
    return "\n".join(lines) or "no terminals"


def herdr_start(agent, cwd, name="", task=""):
    known = {"claude": ["claude"], "grok": ["grok"], "codex": ["codex"]}
    argv = list(known.get(agent, shlex.split(agent)))
    if task:
        argv.append(task)
    if not argv:
        raise RuntimeError("herdr_start needs an agent or command")
    term_name = _hc_name(name or agent)
    data = _hc([
        "run", "--bg", "--json", "--outlive-parent", "--no-parent",
        "--name", term_name, "--cwd", str(Path(cwd).expanduser().resolve()), *argv,
    ])
    if not isinstance(data, dict):
        raise RuntimeError(f"hc run returned no JSON: {data}")
    terminal = str(data.get("address") or data.get("name") or term_name)
    first = data.get("first_message") if isinstance(data.get("first_message"), dict) else {}
    sent = first.get("sent")
    note = ""
    if task and sent is False:
        screen = _herdr_read_text(terminal, 40)
        retry = _hc(["send", terminal, task, "--json"])
        if _hc_not_started(retry):
            note = f"\nfirst_message.sent=false; send was not_started. Read the screen before trying again.\n{screen}"
        else:
            note = "\nfirst_message.sent=false; retried once with hc send"
    state = _hc_state(data) or _herdr_status(terminal) or "unknown"
    if state == "blocked":
        return _blocked_text(terminal, state) + note
    return f"terminal={terminal} name={term_name} status={state} first_message.sent={sent}{note}"


def herdr_send(terminal, text, wait=True, timeout_seconds=1800, force=False, on_checkin=None):
    if isinstance(wait, str):
        wait = wait.lower() not in ("0", "false", "no")
    deadline = time.time() + float(1800 if timeout_seconds is None else timeout_seconds)
    if not force and _herdr_status(terminal) == "blocked":
        return _blocked_text(terminal, "blocked")
    attempts = 0
    delivered = False
    while time.time() < deadline:
        if on_checkin:
            on_checkin()
        remaining = max(1, int(deadline - time.time()))
        if not delivered:
            cmd = ["send", str(terminal), text, "--json"]
            if force:
                cmd.append("--force")
            if wait:
                cmd += ["--wait", "--timeout", str(min(180, remaining))]
            data = _hc(cmd)
            attempts += 1
            if _hc_not_started(data):
                screen = _herdr_read_text(terminal, 80)
                if attempts >= 2:
                    return f"status=not_started terminal={terminal}\nThe turn did not start. Not resending again.\n{screen}"
                continue
            delivered = True
            if not wait:
                state = _herdr_status(terminal) or "unknown"
                return f"status={state} terminal={terminal}\n" + _herdr_read_text(terminal, 40)
        else:
            data = _hc(["wait", str(terminal), "--json", "--timeout", str(min(180, remaining))])
        state = _hc_state(data) or _herdr_status(terminal)
        if state == "blocked":
            return _blocked_text(terminal, state)
        if state == "error":
            return f"status=error terminal={terminal}\n" + _herdr_read_text(terminal, 80)
        if state in ("idle", "done"):
            return f"status={state} terminal={terminal}\n" + _herdr_read_text(terminal, 80)
        if not _hc_checkin(data, state):
            return f"status={state or 'unknown'} terminal={terminal}\n" + _herdr_read_text(terminal, 40)
    return f"status=working terminal={terminal}\nstill running\n" + _herdr_read_text(terminal, 40)


def herdr_read(terminal, lines=80):
    state = _herdr_status(terminal) or "unknown"
    return f"status={state} terminal={terminal}\n" + _herdr_read_text(terminal, lines)


def herdr_wait(terminal, timeout_seconds=1800):
    return _herdr_wait_only(terminal, timeout_seconds)


def _herdr_wait_only(terminal, timeout_seconds):
    deadline = time.time() + float(1800 if timeout_seconds is None else timeout_seconds)
    while time.time() < deadline:
        remaining = max(1, int(deadline - time.time()))
        data = _hc(["wait", str(terminal), "--json", "--timeout", str(min(180, remaining))])
        state = _hc_state(data) or _herdr_status(terminal)
        if state == "blocked":
            return _blocked_text(terminal, state)
        if state == "error":
            return f"status=error terminal={terminal}\n" + _herdr_read_text(terminal, 80)
        if state in ("idle", "done"):
            return f"status={state} terminal={terminal}\n" + _herdr_read_text(terminal, 80)
    return f"status=working terminal={terminal}\nstill running\n" + _herdr_read_text(terminal, 40)


def herdr_interrupt(terminal):
    data = _hc(["interrupt", str(terminal), "--json"])
    return f"interrupted terminal={terminal} status={_hc_state(data) or _herdr_status(terminal) or 'unknown'}"


def herdr_keys(terminal, keys):
    names = keys if isinstance(keys, list) else shlex.split(str(keys))
    if not names:
        raise RuntimeError("herdr_keys needs key names")
    _hc(["keys", str(terminal), *names, "--json"])
    return f"keys {' '.join(names)} terminal={terminal}\n" + _herdr_read_text(terminal, 40)


def _herdr_tool(name, args):
    fn = {
        "herdr_list": lambda: herdr_list(),
        "herdr_start": lambda: herdr_start(args.get("agent") or "", args.get("cwd") or "", args.get("name") or "", args.get("task") or ""),
        "herdr_send": lambda: herdr_send(args.get("terminal"), args.get("text") or "", args.get("wait", True), args.get("timeout_seconds", 1800), bool(args.get("force"))),
        "herdr_read": lambda: herdr_read(args.get("terminal"), args.get("lines", 80)),
        "herdr_wait": lambda: _herdr_wait_only(args.get("terminal"), args.get("timeout_seconds", 1800)),
        "herdr_interrupt": lambda: herdr_interrupt(args.get("terminal")),
        "herdr_keys": lambda: herdr_keys(args.get("terminal"), args.get("keys") or ""),
    }.get(name)
    if not fn:
        raise RuntimeError(f"unknown tool {name}")
    return fn()


def _herdr_run(argv):
    parser = argparse.ArgumentParser(prog="grok_mcp.py herdr-run")
    parser.add_argument("--terminal", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args(argv)
    deadline = time.time() + args.timeout
    prev, last_status = [], None

    def poll():
        nonlocal last_status, prev
        state = _herdr_status(args.terminal)
        if args.follow and state and state != last_status:
            print(f"status={state}", flush=True)
            last_status = state
        lines = _herdr_read_text(args.terminal, 200).splitlines()
        index = 0
        while index < len(prev) and index < len(lines) and prev[index] == lines[index]:
            index += 1
        if args.follow:
            for line in lines[index:]:
                print(line, flush=True)
        prev[:] = lines
        return state, lines

    # Deliver once. A check-in or not_started is handled in herdr_send.
    remaining = max(1, int(deadline - time.time()))
    result = herdr_send(args.terminal, args.text, wait=True, timeout_seconds=remaining, on_checkin=poll)
    state, lines = poll()
    print("---", flush=True)
    print("\n".join(lines[-40:]), flush=True)
    if result.startswith("status=idle") or result.startswith("status=done") or state in ("idle", "done"):
        return 0
    if result.startswith("status=blocked") or state == "blocked":
        print(result, flush=True)
        return 3
    if result.startswith("status=error") or state == "error":
        print(result, flush=True)
        return 1
    print(result, flush=True)
    return 4


def _schema(props, required):
    nums = ("wait_seconds", "since", "timeout_seconds")
    return {"type": "object", "required": required, "properties": {n: {"type": "number" if n in nums else "string"} for n in props}}


def _mcp():
    hub = Hub()
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if method and rid is None:
            continue
        try:
            if method == "initialize":
                result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "grok-bridge", "version": "0.1.0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": n, "description": d, "inputSchema": _schema(p, r)} for n, d, p, r in TOOLS]}
            elif method == "tools/call":
                params = msg.get("params") or {}
                result = {"content": [{"type": "text", "text": hub.call(params.get("name") or "", params.get("arguments") or {})}], "isError": False}
            elif method == "ping":
                result = {}
            else:
                raise RuntimeError(f"unknown method {method}")
            payload = {"jsonrpc": "2.0", "id": rid, "result": result}
        except Exception as exc:  # tool and protocol errors go back to the MCP client
            if method == "tools/call":
                payload = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
            else:
                payload = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(exc)}}
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


def _run(argv):
    """Load a leader session, send one prompt, print the final text. --follow prints events as they happen."""
    parser = argparse.ArgumentParser(prog="grok_mcp.py run")
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args(argv)
    hub = Hub()
    hub.load_session(args.session, args.cwd)
    hub.prompt(args.session, args.text, wait_seconds=0)
    sess, deadline = hub._sess(args.session), time.time() + args.timeout
    seen = sess.live_from
    while True:
        with sess.cv:
            fresh = [event for event in sess.events if event["n"] > seen and not event.get("replay")]
            idle = not sess.prompting and not sess.pending and sess.state != "running"
            if not fresh and not idle and time.time() < deadline:
                sess.cv.wait(0.2)
                continue
        for event in fresh:
            if args.follow:
                print(f"#{event['n']} {event['line']}", flush=True)
            seen = event["n"]
        if idle:
            final = hub._since(sess, sess.turn_start)
            if final:
                print(final, flush=True)
            return 0
        if time.time() >= deadline:
            print("still running", flush=True)
            return 4


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        raise SystemExit(_run(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "herdr-run":
        raise SystemExit(_herdr_run(sys.argv[2:]))
    _mcp()


if __name__ == "__main__":
    main()
