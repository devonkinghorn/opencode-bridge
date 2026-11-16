"""herdr backend against tests/fake_hc.py. No real hc."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FAKE = ROOT / "tests" / "fake_hc.py"


class HerdrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = str(Path(self.tmp) / "hc.json")
        self.env = os.environ.copy()
        self.env["HC_BIN"] = str(FAKE)
        self.env["HC_FAKE_STATE"] = self.state
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "grok_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=self.env, cwd=self.tmp,
        )
        self.seq = 0

    def tearDown(self):
        self.proc.kill()
        self.proc.wait(timeout=5)
        self.proc.stdin.close()
        self.proc.stdout.close()

    def tool(self, name, arguments):
        self.seq += 1
        self.proc.stdin.write((json.dumps({
            "jsonrpc": "2.0", "id": self.seq, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }) + "\n").encode())
        self.proc.stdin.flush()
        msg = json.loads(self.proc.stdout.readline())
        text = msg["result"]["content"][0]["text"]
        self.assertFalse(msg["result"].get("isError"), text)
        return text

    def log(self):
        return json.loads(Path(self.state).read_text(encoding="utf-8"))["log"]

    def test_send_checkin_block_and_not_started(self):
        self.seq += 1
        self.proc.stdin.write((json.dumps({
            "jsonrpc": "2.0", "id": self.seq, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        }) + "\n").encode())
        self.proc.stdin.flush()
        self.proc.stdout.readline()
        blocked = self.tool("herdr_start", {"agent": "claude", "cwd": self.tmp, "name": "trust", "task": "BLOCK please"})
        self.assertIn("status=blocked", blocked)
        self.assertIn("human should approve", blocked)
        self.assertIn("Allow this action?", blocked)
        self.assertFalse(any(item[0] == "keys" for item in self.log()))
        slow = self.tool("herdr_start", {"agent": "grok", "cwd": self.tmp, "name": "slow", "task": "SLOW job"})
        self.assertIn("status=working", slow)
        sent = self.tool("herdr_send", {"terminal": "slow", "text": "keep going", "timeout_seconds": 30})
        self.assertIn("status=idle", sent)
        self.assertIn("finished", sent)
        self.assertNotIn("\x1b", sent)
        listed = self.tool("herdr_list", {})
        self.assertIn("slow", listed)
        lost = self.tool("herdr_start", {"agent": "codex", "cwd": self.tmp, "name": "lost", "task": "SENDLOST hello"})
        self.assertIn("terminal=", lost)
        delivered = self.tool("herdr_send", {"terminal": "lost", "text": "hello there", "timeout_seconds": 20})
        self.assertIn("got: hello there", delivered)
        self.assertIn("status=idle", delivered)
        commands = [item[0] for item in self.log()]
        self.assertGreaterEqual(commands.count("send"), 2)
        self.assertIn("read", commands)
        self.tool("herdr_keys", {"terminal": "trust", "keys": "down enter"})
        self.assertTrue(any(item[0] == "keys" and "down" in item for item in self.log()))
        stopped = self.tool("herdr_interrupt", {"terminal": "slow"})
        self.assertIn("interrupted", stopped)

    def test_herdr_run_follow(self):
        self.seq += 1
        self.proc.stdin.write((json.dumps({
            "jsonrpc": "2.0", "id": self.seq, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        }) + "\n").encode())
        self.proc.stdin.flush()
        self.proc.stdout.readline()
        self.tool("herdr_start", {"agent": "claude", "cwd": self.tmp, "name": "watch", "task": "SLOW watch"})
        cli = subprocess.run(
            [sys.executable, str(ROOT / "grok_mcp.py"), "herdr-run", "--terminal", "watch", "--text", "next step", "--follow", "--timeout", "30"],
            env=self.env, capture_output=True, text=True, timeout=40,
        )
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        self.assertIn("status=working", cli.stdout)
        self.assertIn("status=idle", cli.stdout)
        self.assertIn("got: next step", cli.stdout)
        self.assertIn("finished", cli.stdout)


if __name__ == "__main__":
    unittest.main()
