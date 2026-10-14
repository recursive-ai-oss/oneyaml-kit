"""Comparison of an adapter's response with a case's expectation.

Comparison is full and structural: every byte, every event line, the whole value.
There is no silent narrowing. Two modes exist and are always reported separately:

* strict: everything the expectation states (error category, position, the events
  emitted before an error, an unchanged document after a refused patch);
* loose: the outcome only. A valid input must give the full expected result; an
  invalid input must be reported as an error, of any kind, anywhere.

A mode is "unsupported" when the adapter has not declared a capability that mode
needs (for example error positions), never a silent pass.
"""

from __future__ import annotations

import base64

from . import kitjson
from .cases import PATCH_OPS

PASS, FAIL, UNSUPPORTED = "pass", "fail", "unsupported"

ANY_PATCH = "patch:*"
CAP_CATEGORY = "error-category"
CAP_POSITION = "error-position"
CAP_ERROR_EVENTS = "error-events"


def required_caps(case, expect: dict) -> tuple[set, set]:
    """(capabilities both modes need, extra capabilities strict mode needs)."""
    base: set[str] = set()
    # `a|b` means either capability will do. An adapter that cannot describe results can still
    # say whether an input is accepted (`outcome:<op>`), which is enough to be asked.
    if case.op == "events":
        base.add("events|outcome:events")
    elif case.op == "value":
        base.add("value|outcome:value")
    else:
        if case.args.get("format", "json-patch") == "merge-patch":
            base.add("patch:merge")
        else:
            # An empty patch is how a round trip is asked for: it names no operation, but a
            # library that cannot write a document back must not be sent it.
            if not case.args["patch"]:
                base.add(ANY_PATCH)
            for o in case.args["patch"]:
                name = o.get("op")
                # An operation name outside RFC 6902 can only be refused; any patch-capable
                # adapter can be asked to do that.
                base.add(f"patch:{name}" if name in PATCH_OPS else ANY_PATCH)
    if case.profile != "core":
        base.add(f"profile:{case.profile}")
    strict: set[str] = set()
    if expect["kind"] == "error":
        if expect["category"] != "any":
            strict.add(CAP_CATEGORY)
        if expect["line"] is not None:
            strict.add(CAP_POSITION)
        if expect["events"] is not None:
            strict.add(CAP_ERROR_EVENTS)
    if expect["kind"] == "bounded" and expect["category"] is not None:
        strict.add(CAP_CATEGORY)
    return base, strict


def satisfied(needs: set, caps: set) -> bool:
    """Whether `caps` covers `needs`. The wildcard needs any patch capability, and `a|b`
    needs either a or b."""
    for n in needs:
        if n == ANY_PATCH:
            if not any(c.startswith("patch:") for c in caps):
                return False
        elif not any(alt in caps for alt in n.split("|")):
            return False
    return True


# --------------------------------------------------------------------------
# Values


# Equality, digests and the first-difference path live in kitjson: they never recurse, so a
# value nested a hundred thousand levels deep is compared like any other.
values_equal = kitjson.values_equal
value_diff = kitjson.value_diff
_short = kitjson.short


def diff_lines(exp: list, act: list) -> str:
    for i, (e, a) in enumerate(zip(exp, act)):
        if e != a:
            return f"line {i + 1}: expected {_short(e)}, got {_short(a)}"
    if len(exp) != len(act):
        which, extra = ("expected", exp) if len(exp) > len(act) else ("got", act)
        return f"{which} more lines after line {min(len(exp), len(act))}: {_short(extra[min(len(exp), len(act))])}"
    return ""


def diff_bytes(exp: bytes, act: bytes) -> str:
    n = min(len(exp), len(act))
    i = next((k for k in range(n) if exp[k] != act[k]), n)
    if i == n and len(exp) == len(act):
        return ""
    return (f"first difference at byte {i}: expected {_short(exp[i:i + 40])}, "
            f"got {_short(act[i:i + 40])} (lengths {len(exp)} and {len(act)})")


# --------------------------------------------------------------------------


def _decode_output(resp: dict) -> bytes | None:
    o = resp.get("output")
    if o is None:
        return None
    try:
        return base64.b64decode(o, validate=True)
    except ValueError:
        return None


def _success(case, expect: dict, resp: dict) -> str:
    """Compare an `ok` response with an events, value or bytes expectation; '' when equal."""
    kind = expect["kind"]
    if kind == "events":
        return diff_lines(expect["events"], resp.get("events") or [])
    if kind == "value":
        return value_diff(expect["value"], resp.get("value"), ignore_tags=expect.get("tags") == "ignore")
    out = _decode_output(resp)
    return "output missing or not base64" if out is None else diff_bytes(expect["bytes"], out)


def _over_bounds(expect: dict, resp: dict) -> str:
    """Why a bounded case went over its time or memory bound, or ''."""
    m = resp.get("_measure") or {}
    secs, peak = m.get("seconds"), m.get("peak_mb")
    if secs is not None and secs > expect["time_seconds"]:
        return f"took {secs:.2f} s, bound {expect['time_seconds']:g} s"
    if peak is not None and peak > expect["memory_mb"]:
        return f"peak memory growth {peak:.0f} MB, bound {expect['memory_mb']:g} MB"
    return ""


def _bounded(case, expect: dict, resp: dict, caps: set) -> dict:
    status, err = resp["status"], resp.get("error") or {}
    peak = (resp.get("_measure") or {}).get("peak_mb")
    over = _over_bounds(expect, resp)
    if over:
        return {"loose": FAIL, "strict": FAIL, "detail": over}
    want = expect["category"]
    if status in ("ok", "accepted"):
        if expect["accept"] is None:
            what = "a " + " or ".join(want) + " error"
            return {"loose": FAIL, "strict": FAIL, "detail": f"accepted; only {what} is allowed"}
        if status == "accepted":
            # Within the bounds, but nothing was described that could be compared.
            return {"loose": UNSUPPORTED, "strict": UNSUPPORTED,
                    "reason": "adapter" if case.op in caps else "capability",
                    "detail": "accepted within the bounds; the result was not described, so it "
                              "cannot be checked"}
        d = _success(case, expect["accept"], resp)
        if d:
            return {"loose": FAIL, "strict": FAIL, "detail": "accepted with a wrong result: " + d}
        verdict, detail = PASS, ""
    else:
        if want is None:
            return {"loose": FAIL, "strict": FAIL, "detail": "refused; the exact result is required"}
        verdict, detail = PASS, ""
        if CAP_CATEGORY not in caps:
            verdict = UNSUPPORTED
        elif err.get("category") not in want:
            verdict = FAIL
            detail = f"category: expected one of {', '.join(want)}, got {err.get('category')}"
    if verdict == PASS and peak is None:
        verdict, detail = UNSUPPORTED, "memory use could not be measured on this platform"
    return {"loose": PASS, "strict": verdict, "detail": detail}


def _accepted(case, expect: dict, caps: set) -> dict:
    """An `accepted` answer: the input was taken, and no result is described."""
    if case.op == "patch":
        return {"loose": FAIL, "strict": FAIL, "detail": "protocol: accepted is not an answer to a patch"}
    if expect["kind"] == "error":
        return {"loose": FAIL, "strict": FAIL, "detail": "expected an error, the input was accepted"}
    # A result was expected but cannot be compared. Without the full capability that is the
    # adapter's declared limit (nothing to list); with it, the adapter declined this answer.
    reason = "adapter" if case.op in caps else "capability"
    return {"loose": UNSUPPORTED, "strict": UNSUPPORTED, "reason": reason,
            "detail": "accepted; the result was not described, so it cannot be checked"}


def evaluate(case, expect: dict, resp: dict, caps: set) -> dict:
    """Return {"loose","strict","detail"} for one response against one expectation."""
    status = resp.get("status")
    if status in ("crash", "timeout", "memory"):
        d = resp.get("detail") or status
        return {"loose": FAIL, "strict": FAIL, "detail": d}
    if status == "unsupported":
        return {"loose": UNSUPPORTED, "strict": UNSUPPORTED, "detail": resp.get("reason", "")}
    if status not in ("ok", "error", "accepted"):
        d = f"protocol: bad status {status!r}"
        return {"loose": FAIL, "strict": FAIL, "detail": d}

    kind = expect["kind"]
    err = resp.get("error") or {}

    if status == "accepted" and case.op == "patch":
        return {"loose": FAIL, "strict": FAIL, "detail": "protocol: accepted is not an answer to a patch"}

    if kind == "bounded":
        return _bounded(case, expect, resp, caps)

    if status == "accepted":
        return _accepted(case, expect, caps)

    if kind == "error":
        if status != "error":
            return {"loose": FAIL, "strict": FAIL, "detail": "expected an error, the input was accepted"}
        problems, missing = [], False
        if expect["category"] != "any":
            if CAP_CATEGORY not in caps:
                missing = True
            elif err.get("category") != expect["category"]:
                problems.append(f"category: expected {expect['category']}, got {err.get('category')}")
        if expect["line"] is not None:
            if CAP_POSITION not in caps:
                missing = True
            else:
                want = (expect["line"], expect["column"])
                got = (err.get("line"), err.get("column") if expect["column"] is not None else None)
                if got != want:
                    problems.append(f"position: expected {want[0]}:{want[1]}, got {got[0]}:{got[1]}")
        if expect["events"] is not None:
            if CAP_ERROR_EVENTS not in caps:
                missing = True  # the error and its position still count; the prefix is not claimed
            else:
                d = diff_lines(expect["events"], resp.get("events") or [])
                if d:
                    problems.append("events before the error: " + d)
        if expect["unchanged"]:
            out = _decode_output(resp)
            if out is None:
                problems.append("document after failure not reported")
            else:
                d = diff_bytes(case.input, out)
                if d:
                    problems.append("document changed after a failed patch: " + d)
        if problems:
            return {"loose": PASS, "strict": FAIL, "detail": "; ".join(problems)}
        return {"loose": PASS, "strict": UNSUPPORTED if missing else PASS, "detail": ""}

    # Success expected.
    if status == "error":
        e = err.get("message") or err.get("category") or "error"
        pos = f" at {err['line']}:{err.get('column')}" if err.get("line") else ""
        d = f"unexpected error: {_short(e)}{pos}"
        return {"loose": FAIL, "strict": FAIL, "detail": d}
    d = _success(case, expect, resp)
    v = FAIL if d else PASS
    return {"loose": v, "strict": v, "detail": d}


def summarize(resp: dict) -> dict:
    """What the library actually returned, in a few fields, so a verdict can be audited."""
    g: dict = {"status": resp.get("status")}
    e = resp.get("error")
    if isinstance(e, dict):
        for k in ("category", "line", "column"):
            if k in e:
                g[k] = e[k]
        if e.get("message"):
            g["message"] = str(e["message"])[:200]
    if isinstance(resp.get("events"), list):
        g["events"] = len(resp["events"])
    if resp.get("detail"):
        g["detail"] = str(resp["detail"])[:200]
    return g
