#!/usr/bin/env python3
"""A scriptable adapter for the kit's own protocol tests.

Behaviour per case id comes from the file named by FAKE_SCRIPT (JSON): a map from case id to
one of "ok-events", "crash", "hang", "garbage", "unsupported", "error", "accepted",
"deep-value", "deep-garbage", "slow" (answers after one second), "pool" (the first such case
fills a 200 MB pool that later ones reuse, as a runtime that keeps freed memory does).
FAKE_PROTOCOL says which hello protocol to speak (default 1).
Every request id is appended to FAKE_LOG, one per line, so tests can see what was sent;
FAKE_STARTS and FAKE_LATER change the hello of later starts;
with FAKE_PIDLOG the id, the process id and the session id are also appended to that file.
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
pool = []
protocol = int(os.environ.get("FAKE_PROTOCOL", "1"))

# With FAKE_STARTS (a file) and FAKE_LATER (a JSON object of hello fields), every start after the
# first answers with those fields changed, as an adapter that says something else the second time.
starts = os.environ.get("FAKE_STARTS")
later = json.loads(os.environ.get("FAKE_LATER", "{}"))
started_before = bool(starts) and os.path.exists(starts)
if starts:
    open(starts, "a").write("x")

for line in sys.stdin:
    req = json.loads(line)
    if req["type"] == "hello":
        hello = {"type": "hello", "protocol": protocol, "name": "fake", "version": "0",
                 "language": "python", "capabilities": caps, "emulates": emulates}
        if started_before:
            hello.update(later)
        print(json.dumps(hello), flush=True)
    elif req["type"] == "bye":
        break
    elif req["type"] == "run":
        if log:
            open(log, "a").write(req["id"] + "\n")
        if os.environ.get("FAKE_PIDLOG"):
            open(os.environ["FAKE_PIDLOG"], "a").write("%s %d %d\n" % (req["id"], os.getpid(), os.getsid(0)))
        how = script.get(req["id"], "ok-events")
        if how == "pool" and not pool:
            pool.append(bytearray(b"\x01" * (200 * 1024 * 1024)))
        if how == "crash":
            os._exit(3)
        if how == "hang":
            time.sleep(60)
        if how == "slow":
            time.sleep(1.0)
        if how in ("alloc-hold", "alloc-answer"):
            held.append(b"\x01" * (200 * 1024 * 1024))
            if how == "alloc-hold":
                time.sleep(60)
        if how == "garbage":
            print("this is not json", flush=True)
            continue
        if how == "deep-value":
            n = 100000
            value = "[" * n + "null" + "]" * n
            print('{"id":%s,"status":"ok","value":%s}' % (json.dumps(req["id"]), value), flush=True)
            continue
        if how == "deep-garbage":
            print('{"id":%s,"status":"ok","value":%s' % (json.dumps(req["id"]), "[" * 100000), flush=True)
            continue
        if how == "accepted":
            r = {"id": req["id"], "status": "accepted"}
        elif how == "unsupported":
            r = {"id": req["id"], "status": "unsupported", "reason": "declined"}
        elif how == "error":
            r = {"id": req["id"], "status": "error", "error": {"category": "syntax"}, "events": []}
        elif how in ("emulated", "emulated-undeclared"):
            r = {"id": req["id"], "status": "ok", "events": ["+STR", "-STR"],
                 "emulated": ["unchanged" if how == "emulated" else "secret"]}
        else:
            r = {"id": req["id"], "status": "ok", "events": ["+STR", "-STR"]}
        print(json.dumps(r), flush=True)
