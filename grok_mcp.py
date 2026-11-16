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

import json, os, shlex, subprocess, sys, threading, time
from pathlib import Path

MODES = ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan")
TOOLS = [
    ("new_session", "Create a Grok Build session.", ["cwd", "title", "mode"], ["cwd"]),
    ("load_session", "Load a session by id (TUI sessions too, if ACP allows).", ["session_id", "cwd"], ["session_id", "cwd"]),
    ("prompt", "Send text. Returns assistant text, 'still running', or 'waiting for approval'.", ["session_id", "text", "wait_seconds"], ["session_id", "text"]),
    ("read", "Numbered user/assistant messages. since keeps n > since.", ["session_id", "since"], ["session_id"]),
    ("pending_approvals", "Parked approvals. Show these to the human before answering.", [], []),
    ("respond_approval", "Answer one approval: allow_once, allow_always, or reject.", ["request_id", "option"], ["request_id", "option"]),
    ("cancel", "Cancel the in-flight turn.", ["session_id"], ["session_id"]),
    ("list_sessions", "Sessions created or loaded in this process.", [], []),
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

    def request(self, method, params, timeout=30):
        _rid, event, box = self.send(method, params)
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
        self.messages, self.by_id, self.tools = [], {}, {}
        self.state, self.prompting, self.prompt_rid, self.pending = "idle", False, None, []
        self.cv = threading.Condition()

    def add(self, role, text, mid):
        # Chunks with no messageId append to the open message of the same role
        # (grok 1.0.84 streams one word per agent_message_chunk).
        if mid and mid in self.by_id:
            self.by_id[mid]["text"] += text
            return
        if not mid and self.messages and self.messages[-1].get("open") and self.messages[-1]["role"] == role:
            self.messages[-1]["text"] += text
            return
        for msg in self.messages:
            msg["open"] = False
        msg = {"n": (self.messages[-1]["n"] + 1) if self.messages else 1, "role": role, "text": text, "open": not mid}
        self.messages.append(msg)
        if mid:
            self.by_id[mid] = msg

    def last_n(self):
        return self.messages[-1]["n"] if self.messages else 0


class Hub:
    def __init__(self):
        self.agents, self.sessions, self.perms = {}, {}, {}
        self.perm_n, self.map = 0, threading.Lock()

    def call(self, name, args):
        fn = getattr(self, name, None)
        if name not in {t[0] for t in TOOLS} or not fn:
            raise RuntimeError(f"unknown tool {name}")
        return fn(**(args or {}))

    def new_session(self, cwd, title="", mode=""):
        agent = self.agent(cwd, mode)
        result = agent.request("session/new", {"cwd": agent.cwd, "mcpServers": [], "_meta": {"title": title} if title else {}}, 60) or {}
        sid = str(result.get("sessionId") or "")
        if not sid:
            raise RuntimeError("session/new returned no sessionId")
        self.sessions[sid] = Session(sid, agent.cwd, title, agent)
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
        agent.request("session/load", {"sessionId": session_id, "cwd": agent.cwd, "mcpServers": []}, 60)
        return f"session={session_id} cwd={agent.cwd} state=idle last=#{self.sessions[session_id].last_n()}"

    def prompt(self, session_id, text, wait_seconds=None):
        sess = self._sess(session_id)
        wait = 30.0 if wait_seconds is None else float(wait_seconds)
        with sess.cv:
            if sess.prompting:
                raise RuntimeError("session is already running")
            sess.add("user", text, f"user-{time.time_ns()}")
            sess.prompting, sess.state = True, "running"
            start = sess.last_n()
        _rid, event, box = sess.agent.send(
            "session/prompt", {"sessionId": sess.id, "prompt": [{"type": "text", "text": text}]},
            before=lambda rid: setattr(sess, "prompt_rid", rid),
        )
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
                self._remember_tool(sess, update)
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
            if sess.agent is agent and sess.prompt_rid == request_id:
                with sess.cv:
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

    def _resolve_notice(self, sess, update):
        tid = str(update.get("tool_call_id") or update.get("toolCallId") or "")
        for perm in self.perms.values():
            if perm.get("session") == sess.id and perm.get("tool_call_id") == tid and not perm.get("resolved"):
                perm["resolved"] = True
                sess.pending = [item for item in sess.pending if item != perm["id"]]
                sess.state = "running" if sess.prompting else "idle"

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


def _schema(props, required):
    return {"type": "object", "required": required, "properties": {n: {"type": "number" if n in ("wait_seconds", "since") else "string"} for n in props}}


def main():
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


if __name__ == "__main__":
    main()
