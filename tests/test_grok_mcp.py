"""One MCP round-trip against tests/fake_acp_agent.py."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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


if __name__ == "__main__":
    unittest.main()
