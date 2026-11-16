#!/usr/bin/env python3
"""Tiny ACP stand-in. A prompt containing BLOCK parks on session/request_permission."""

import json
import sys
import threading
import uuid

write_lock = threading.Lock()
responses = {}
cv = threading.Condition()


def emit(obj):
    data = (json.dumps(obj) + "\n").encode()
    with write_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def handle(msg):
    method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if method == "initialize":
        emit({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}, "authMethods": []}})
    elif method == "session/new":
        emit({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": str(uuid.uuid4())}})
    elif method == "session/load":
        sid = params.get("sessionId")
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "messageId": "load-1", "content": {"type": "text", "text": "loaded"}}}})
        emit({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/set_mode":
        emit({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/prompt":
        sid = params.get("sessionId")
        text = "".join(b.get("text", "") for b in params.get("prompt") or [] if isinstance(b, dict))
        if "BLOCK" not in text:
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "messageId": "p1", "content": {"type": "text", "text": "plain reply"}}}})
            emit({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
            return
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "messageId": "a1", "content": {"type": "text", "text": "Working."}}}})
        perm = f"perm-{uuid.uuid4()}"
        emit({"jsonrpc": "2.0", "id": perm, "method": "session/request_permission", "params": {
            "sessionId": sid,
            "toolCall": {"toolCallId": "c1", "title": "Edit src/x.ts", "kind": "edit", "_meta": {"x.ai/tool": {"name": "search_replace", "kind": "edit"}}},
            "options": [
                {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
                {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
            ],
        }})
        with cv:
            while perm not in responses:
                cv.wait()
            outcome = responses.pop(perm).get("result") or {}
        chosen = ((outcome.get("outcome") or {}).get("optionId")) or "cancelled"
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "messageId": "a2", "content": {"type": "text", "text": f"decision:{chosen}"}}}})
        emit({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif rid is not None:
        emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}})


def main():
    for raw in sys.stdin.buffer:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "method" in msg:
            threading.Thread(target=handle, args=(msg,), daemon=True).start()
        else:
            with cv:
                responses[msg.get("id")] = msg
                cv.notify_all()


if __name__ == "__main__":
    main()
