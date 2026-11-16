#!/usr/bin/env python3
"""Stand-in for the herdr `hc` CLI. State lives in HC_FAKE_STATE."""

import json
import os
import sys


def load():
    path = os.environ["HC_FAKE_STATE"]
    if not os.path.exists(path):
        return {"n": 0, "terms": {}, "log": []}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save(state):
    path = os.environ["HC_FAKE_STATE"]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def opt(args, name):
    if name in args:
        return args[args.index(name) + 1]
    return None


def positionals(args):
    skip = False
    valued = {"--name", "--cwd", "--lines", "--timeout", "--if", "--on", "--parent"}
    found = []
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in valued:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        found.append(arg)
    return found[1:]


def find(state, terminal):
    for term in state["terms"].values():
        if term["address"] == terminal or term["name"] == terminal:
            return term
    raise SystemExit(f"no terminal {terminal}")


def settle(term, waiting):
    if not waiting:
        term["status"] = "idle" if term["status"] != "blocked" else "blocked"
        return {"status": {"state": term["status"]}}
    if term["checkins"] > 0:
        term["checkins"] -= 1
        if term["checkins"] == 0:
            term["status"] = "idle"
            term["screen"].append("\x1b[32mfinished\x1b[0m")
            return {"status": {"state": "idle"}}
        term["status"] = "working"
        return {"checked_in": True, "status": {"state": "working"}}
    return {"status": {"state": term["status"]}}


def main():
    args = sys.argv[1:]
    state = load()
    state["log"].append(args)
    cmd = args[0]
    pos = positionals(args)
    if cmd == "ls":
        rows = [
            {"address": t["address"], "name": t["name"], "cwd": t["cwd"], "status": {"state": t["status"]}}
            for t in state["terms"].values()
        ]
        save(state)
        print(json.dumps({"terminals": rows}))
        return
    if cmd == "run":
        state["n"] += 1
        task = pos[-1] if len(pos) > 1 else ""
        checkins = 2 if "SLOW" in task else 0
        blocked = "BLOCK" in task
        lost = "LOST" in task
        addr = f"t{state['n']}"
        term = {
            "address": addr,
            "name": opt(args, "--name") or addr,
            "cwd": opt(args, "--cwd") or "",
            "status": "blocked" if blocked else ("working" if checkins else "idle"),
            "screen": ["Allow this action?"] if blocked else (["working on it"] if checkins else []),
            "checkins": checkins,
            "not_started_left": 2 if "SENDLOST" in task else 0,
        }
        state["terms"][addr] = term
        save(state)
        print(json.dumps({
            "address": addr,
            "name": term["name"],
            "cwd": term["cwd"],
            "state": "live",
            "status": {"state": term["status"]},
            "first_message": {"sent": not lost, "started": not lost},
        }))
        return
    if cmd == "get":
        term = find(state, pos[0])
        save(state)
        print(json.dumps({"address": term["address"], "name": term["name"], "cwd": term["cwd"], "status": {"state": term["status"]}}))
        return
    if cmd == "read":
        term = find(state, pos[0])
        lines = int(opt(args, "--lines") or "80")
        save(state)
        print("\n".join(term["screen"][-lines:]))
        return
    if cmd == "send":
        term = find(state, pos[0])
        text = pos[1] if len(pos) > 1 else ""
        if term["not_started_left"] > 0:
            term["not_started_left"] -= 1
            save(state)
            print(json.dumps({"result": "not_started"}))
            return
        if text:
            term["screen"].append("got: " + text)
        payload = settle(term, "--wait" in args)
        save(state)
        print(json.dumps(payload))
        return
    if cmd == "wait":
        term = find(state, pos[0])
        payload = settle(term, True)
        save(state)
        print(json.dumps(payload))
        return
    if cmd == "keys":
        term = find(state, pos[0])
        term["screen"].append("keys:" + " ".join(pos[1:]))
        save(state)
        print(json.dumps({"ok": True}))
        return
    if cmd == "interrupt":
        term = find(state, pos[0])
        term["status"] = "idle"
        term["screen"].append("interrupted")
        save(state)
        print(json.dumps({"status": {"state": "idle"}}))
        return
    save(state)
    raise SystemExit(f"unknown {cmd}")


if __name__ == "__main__":
    main()
