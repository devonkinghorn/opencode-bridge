"""One MCP round-trip against tests/fake_acp_agent.py."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FAKE = ROOT / "tests" / "fake_acp_agent.py"


class McpTests(unittest.TestCase):
    def test_approval_round_trip(self):
        project = tempfile.mkdtemp()
        env = os.environ.copy()
        env["GROK_BRIDGE_AGENT"] = f"{sys.executable} {FAKE}"
        proc = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "grok_mcp.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=project,
        )
        seq = 0

        def rpc(method, params=None):
            nonlocal seq
            seq += 1
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": seq, "method": method, "params": params or {}}) + "\n").encode())
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        def tool(name, arguments):
            msg = rpc("tools/call", {"name": name, "arguments": arguments})
            text = msg["result"]["content"][0]["text"]
            self.assertFalse(msg["result"].get("isError"), text)
            return text

        try:
            init = rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
            self.assertEqual(init["result"]["serverInfo"]["name"], "grok-bridge")
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
            proc.stdin.flush()
            names = {item["name"] for item in rpc("tools/list")["result"]["tools"]}
            self.assertIn("respond_approval", names)
            created = tool("new_session", {"cwd": project, "title": "smoke", "mode": "plan"})
            sid = created.split("session=", 1)[1].split()[0]
            blocked = tool("prompt", {"session_id": sid, "text": "BLOCK please", "wait_seconds": 5})
            self.assertIn("waiting for approval:", blocked)
            self.assertIn("Edit src/x.ts", blocked)
            pending = tool("pending_approvals", {})
            self.assertIn("search_replace", pending)
            req = pending.split()[0]
            tool("respond_approval", {"request_id": req, "option": "allow_once"})
            seen = ""
            for _ in range(20):
                seen = tool("read", {"session_id": sid})
                if "decision:allow-once" in seen:
                    break
            self.assertIn("#1 [user]", seen)
            self.assertIn("BLOCK please", seen)
            self.assertIn("Working.", seen)
            self.assertIn("decision:allow-once", seen)
            self.assertIn(sid, tool("list_sessions", {}))
        finally:
            proc.kill()
            proc.wait(timeout=5)
            proc.stdin.close()
            proc.stdout.close()

    def test_pending_interaction_and_leader_flag(self):
        import grok_mcp

        saved = {key: os.environ.get(key) for key in ("GROK_BRIDGE_AGENT", "GROK_BIN", "GROK_LEADER", "GROK_LEADER_SOCKET")}
        try:
            os.environ.pop("GROK_BRIDGE_AGENT", None)
            os.environ["GROK_BIN"] = "grok"
            os.environ.pop("GROK_LEADER", None)
            os.environ.pop("GROK_LEADER_SOCKET", None)
            argv = grok_mcp._argv("plan")
            self.assertIn("--leader", argv)
            self.assertNotIn("--no-leader", argv)
            os.environ["GROK_LEADER"] = "0"
            self.assertIn("--no-leader", grok_mcp._argv("plan"))
            os.environ["GROK_LEADER"] = "1"
            os.environ["GROK_LEADER_SOCKET"] = "/tmp/leader.sock"
            flagged = grok_mcp._argv("default")
            self.assertEqual(flagged[flagged.index("--leader-socket") + 1], "/tmp/leader.sock")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        project = tempfile.mkdtemp()
        env = os.environ.copy()
        env["GROK_BRIDGE_AGENT"] = f"{sys.executable} {FAKE}"
        proc = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "grok_mcp.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=project,
        )
        seq = 0

        def rpc(method, params=None):
            nonlocal seq
            seq += 1
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": seq, "method": method, "params": params or {}}) + "\n").encode())
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        def tool(name, arguments):
            msg = rpc("tools/call", {"name": name, "arguments": arguments})
            text = msg["result"]["content"][0]["text"]
            self.assertFalse(msg["result"].get("isError"), text)
            return text

        try:
            rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
            created = tool("new_session", {"cwd": project})
            sid = created.split("session=", 1)[1].split()[0]
            blocked = tool("prompt", {"session_id": sid, "text": "INTERACT now", "wait_seconds": 5})
            self.assertIn("waiting for approval:", blocked)
            self.assertIn("Edit src/y.ts", blocked)
            pending = tool("pending_approvals", {})
            self.assertIn("status=pending", pending)
            self.assertIn("src/y.ts", pending)
            self.assertIn("Edit src/y.ts", pending)
            req = pending.split()[0]
            answer = tool("respond_approval", {"request_id": req, "option": "allow_once"})
            self.assertIn("TUI", answer)
            seen = tool("read", {"session_id": sid})
            self.assertIn("Hello", seen)
            self.assertEqual(seen.count("[assistant]"), 1)
            self.assertIn(f"waiting for approval: {req} Edit src/y.ts", seen)
            resolved = ""
            for _ in range(30):
                resolved = tool("read", {"session_id": sid})
                if "approval resolved:" in resolved:
                    break
                time.sleep(0.1)
            self.assertIn(f"approval resolved: {req} Edit src/y.ts", resolved)
            self.assertIn("status=resolved", tool("pending_approvals", {}))
        finally:
            proc.kill()
            proc.wait(timeout=5)
            proc.stdin.close()
            proc.stdout.close()


    def test_wait_and_events(self):
        project = tempfile.mkdtemp()
        env = os.environ.copy()
        env["GROK_BRIDGE_AGENT"] = f"{sys.executable} {FAKE}"
        proc = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "grok_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=project,
        )
        seq = 0

        def tool(name, arguments):
            nonlocal seq
            seq += 1
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": seq, "method": "tools/call", "params": {"name": name, "arguments": arguments}}) + "\n").encode())
            proc.stdin.flush()
            msg = json.loads(proc.stdout.readline())
            text = msg["result"]["content"][0]["text"]
            self.assertFalse(msg["result"].get("isError"), text)
            return text

        try:
            seq += 1
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": seq, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}) + "\n").encode())
            proc.stdin.flush()
            proc.stdout.readline()
            held = tool("new_session", {"cwd": project})
            hid = held.split("session=", 1)[1].split()[0]
            self.assertTrue(tool("prompt", {"session_id": hid, "text": "HOLD the turn", "wait_seconds": 0}).startswith("still running"))
            waiting = tool("wait", {"session_id": hid, "timeout_seconds": 5})
            self.assertIn("state=waiting", waiting)
            self.assertIn("Edit src/z.ts", waiting)
            made = tool("new_session", {"cwd": project, "title": "flow"})
            fid = made.split("session=", 1)[1].split()[0]
            self.assertTrue(tool("prompt", {"session_id": fid, "text": "FLOW the tests", "wait_seconds": 0}).startswith("still running"))
            done = tool("wait", {"session_id": fid, "timeout_seconds": 10})
            self.assertIn("state=idle", done)
            self.assertIn("All done", done)
            ev = tool("events", {"session_id": fid, "since": 0})
            self.assertIn("pytest -q", ev)
            self.assertIn("completed", ev)
            self.assertIn("assistant Hi there", ev)
            last = int(ev.strip().split("last=#", 1)[1])
            self.assertTrue(tool("events", {"session_id": fid, "since": last}).startswith("last=#"))
            tool("load_session", {"session_id": "replay-sess", "cwd": project})
            self.assertTrue(tool("prompt", {"session_id": "replay-sess", "text": "FLOW please", "wait_seconds": 0}).startswith("still running"))
            replayed = tool("wait", {"session_id": "replay-sess", "timeout_seconds": 10})
            self.assertIn("state=idle", replayed)
            self.assertNotIn("replay-should-skip", replayed)
            self.assertNotIn("replayed history", replayed)
            skipped = tool("events", {"session_id": "replay-sess"})
            self.assertNotIn("replay-should-skip", skipped)
            self.assertNotIn("replayed history", skipped)
            self.assertIn("pytest -q", skipped)
            tool_line = next(line for line in skipped.splitlines() if "pytest -q" in line)
            self.assertLessEqual(len(tool_line.split("Run tests ", 1)[1]), 120)
            self.assertNotIn("Z" * 121, tool_line)
        finally:
            proc.kill()
            proc.wait(timeout=5)
            proc.stdin.close()
            proc.stdout.close()

        cli = subprocess.run(
            [sys.executable, str(ROOT / "grok_mcp.py"), "run", "--session", "cli-session", "--cwd", project, "--text", "FLOW please", "--follow", "--timeout", "10"],
            env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        self.assertIn("pytest -q", cli.stdout)
        self.assertIn("All done", cli.stdout)
        self.assertNotIn("replay-should-skip", cli.stdout)
        self.assertNotIn("replayed history", cli.stdout)
        follow_line = next(line for line in cli.stdout.splitlines() if "pytest -q" in line)
        self.assertLessEqual(len(follow_line.split("Run tests ", 1)[1]), 120)


if __name__ == "__main__":
    unittest.main()
