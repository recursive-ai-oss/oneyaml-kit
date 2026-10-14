#!/usr/bin/env python3
"""A scriptable adapter for the kit's own protocol tests.

Behaviour per case id comes from the file named by FAKE_SCRIPT (JSON): a map from
case id to one of "ok-events", "crash", "hang", "garbage", "unsupported", "error".
Every request id is appended to FAKE_LOG, one per line, so tests can see what was sent.
"""
import json
import os
import sys
import time

script = json.load(open(os.environ["FAKE_SCRIPT"]))
log = os.environ.get("FAKE_LOG")
caps = json.loads(os.environ.get("FAKE_CAPS", '["events", "value"]'))
emulates = json.loads(os.environ.get("FAKE_EMULATES", "[]"))
held = []

for line in sys.stdin:
    req = json.loads(line)
    if req["type"] == "hello":
        print(json.dumps({"type": "hello", "protocol": 1, "name": "fake", "version": "0",
                          "language": "python", "capabilities": caps,
                          "emulates": emulates}), flush=True)
    elif req["type"] == "bye":
        break
    elif req["type"] == "run":
        if log:
            open(log, "a").write(req["id"] + "\n")
        how = script.get(req["id"], "ok-events")
        if how == "crash":
            os._exit(3)
        if how == "hang":
            time.sleep(60)
        if how in ("alloc-hold", "alloc-answer"):
            held.append(b"\x01" * (200 * 1024 * 1024))
            if how == "alloc-hold":
                time.sleep(60)
        if how == "garbage":
            print("this is not json", flush=True)
            continue
        if how == "unsupported":
            r = {"id": req["id"], "status": "unsupported", "reason": "declined"}
        elif how == "error":
            r = {"id": req["id"], "status": "error", "error": {"category": "syntax"}, "events": []}
        elif how in ("emulated", "emulated-undeclared"):
            r = {"id": req["id"], "status": "ok", "events": ["+STR", "-STR"],
                 "emulated": ["unchanged" if how == "emulated" else "secret"]}
        else:
            r = {"id": req["id"], "status": "ok", "events": ["+STR", "-STR"]}
        print(json.dumps(r), flush=True)
