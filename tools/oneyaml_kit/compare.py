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
import math
from typing import Any

from .cases import ESCAPES, PATCH_OPS

PASS, FAIL, UNSUPPORTED = "pass", "fail", "unsupported"

ANY_PATCH = "patch:*"
CAP_CATEGORY = "error-category"
CAP_POSITION = "error-position"


def required_caps(case, expect: dict) -> tuple[set, set]:
    """(capabilities both modes need, extra capabilities strict mode needs)."""
    base: set[str] = set()
    if case.op == "events":
        base.add("events")
    elif case.op == "value":
        base.add("value")
    else:
        if case.args.get("format", "json-patch") == "merge-patch":
            base.add("patch:merge")
        else:
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
    if expect["kind"] == "bounded" and expect["category"] is not None:
        strict.add(CAP_CATEGORY)
    return base, strict


def satisfied(needs: set, caps: set) -> bool:
    """Whether `caps` covers `needs` (the wildcard needs any patch capability)."""
    for n in needs:
        if n == ANY_PATCH:
            if not any(c.startswith("patch:") for c in caps):
                return False
        elif n not in caps:
            return False
    return True


# --------------------------------------------------------------------------
# Values


def canon(v: Any, ignore_tags: bool = False):
    """A canonical form: types are strict (1 != 1.0 != true), mappings are unordered, and
    only the three escape forms may use `$`-prefixed keys. Raises ValueError for anything
    that is not a kit value, so a malformed answer can never compare equal."""
    if v is None:
        return ("n",)
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, int):
        return ("i", v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError("non-finite float outside the $float form")
        return ("f", v)
    if isinstance(v, str):
        return ("s", v)
    if isinstance(v, list):
        return ("l", [canon(x, ignore_tags) for x in v])
    if isinstance(v, dict):
        dollar = [k for k in v if isinstance(k, str) and k.startswith("$")]
        if dollar:
            if len(v) != 1 or dollar[0] not in ESCAPES:
                raise ValueError("object with a reserved key")
            k, x = dollar[0], v[dollar[0]]
            if k == "$float":
                if x not in ("inf", "-inf", "nan"):
                    raise ValueError("bad $float")
                return ("f", x)
            if k == "$tagged":
                if not isinstance(x, dict) or set(x) != {"tag", "value"} or not isinstance(x["tag"], str):
                    raise ValueError("bad $tagged")
                inner = canon(x["value"], ignore_tags)
                return inner if ignore_tags else ("t", x["tag"], inner)
            if not isinstance(x, list) or not all(isinstance(p, list) and len(p) == 2 for p in x):
                raise ValueError("bad $map")
            pairs = [(canon(a, ignore_tags), canon(b, ignore_tags)) for a, b in x]
        else:
            pairs = [(("s", k), canon(x, ignore_tags)) for k, x in v.items()]
        return ("m", sorted(pairs, key=repr))
    raise ValueError(f"not a kit value: {type(v).__name__}")


def values_equal(a: Any, b: Any, ignore_tags: bool = False) -> bool:
    try:
        return canon(a, ignore_tags) == canon(b, ignore_tags)
    except (ValueError, KeyError, TypeError, RecursionError):
        return False


def _short(x: Any, n: int = 160) -> str:
    s = repr(x)
    return s if len(s) <= n else s[: n - 3] + "..."


def _untag(v: Any) -> Any:
    while (isinstance(v, dict) and set(v) == {"$tagged"} and isinstance(v["$tagged"], dict)
           and "value" in v["$tagged"]):
        v = v["$tagged"]["value"]
    return v


def value_diff(exp: Any, act: Any, path: str = "$", depth: int = 0, ignore_tags: bool = False) -> str:
    """Where two kit values first differ, as a path, for the failure detail."""
    if depth > 200:
        return f"{path}: nested too deeply to describe"
    try:
        if canon(exp, ignore_tags) == canon(act, ignore_tags):
            return ""
    except (ValueError, KeyError, TypeError, RecursionError):
        return f"{path}: not a kit value"
    if ignore_tags:
        exp, act = _untag(exp), _untag(act)
    if isinstance(exp, list) and isinstance(act, list):
        for i, (e, a) in enumerate(zip(exp, act)):
            d = value_diff(e, a, f"{path}[{i}]", depth + 1, ignore_tags)
            if d:
                return d
        return f"{path}: expected {len(exp)} items, got {len(act)}"
    if (isinstance(exp, dict) and isinstance(act, dict)
            and not any(k.startswith("$") for k in list(exp) + list(act))):
        for k in exp:
            if k not in act:
                return f"{path}: key {k!r} missing"
            d = value_diff(exp[k], act[k], f"{path}.{k}", depth + 1, ignore_tags)
            if d:
                return d
        extra = [k for k in act if k not in exp]
        return f"{path}: unexpected key {extra[0]!r}" if extra else f"{path}: differs"
    return f"{path}: expected {_short(exp, 60)}, got {_short(act, 60)}"


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


def _bounded(case, expect: dict, resp: dict, caps: set) -> dict:
    status, err = resp["status"], resp.get("error") or {}
    m = resp.get("_measure") or {}
    secs, peak = m.get("seconds"), m.get("peak_mb")
    if secs is not None and secs > expect["time_seconds"]:
        return {"loose": FAIL, "strict": FAIL,
                "detail": f"took {secs:.2f} s, bound {expect['time_seconds']:g} s"}
    if peak is not None and peak > expect["memory_mb"]:
        return {"loose": FAIL, "strict": FAIL,
                "detail": f"peak memory growth {peak:.0f} MB, bound {expect['memory_mb']:g} MB"}
    if status == "ok":
        if expect["accept"] is None:
            return {"loose": FAIL, "strict": FAIL,
                    "detail": f"accepted; only a {expect['category']} error is allowed"}
        d = _success(case, expect["accept"], resp)
        if d:
            return {"loose": FAIL, "strict": FAIL, "detail": "accepted with a wrong result: " + d}
        verdict, detail = PASS, ""
    else:
        if expect["category"] is None:
            return {"loose": FAIL, "strict": FAIL, "detail": "refused; the exact result is required"}
        verdict, detail = PASS, ""
        if CAP_CATEGORY not in caps:
            verdict = UNSUPPORTED
        elif err.get("category") != expect["category"]:
            verdict = FAIL
            detail = f"category: expected {expect['category']}, got {err.get('category')}"
    if verdict == PASS and peak is None:
        verdict, detail = UNSUPPORTED, "memory use could not be measured on this platform"
    return {"loose": PASS, "strict": verdict, "detail": detail}


def evaluate(case, expect: dict, resp: dict, caps: set) -> dict:
    """Return {"loose","strict","detail"} for one response against one expectation."""
    status = resp.get("status")
    if status in ("crash", "timeout", "memory"):
        d = resp.get("detail") or status
        return {"loose": FAIL, "strict": FAIL, "detail": d}
    if status == "unsupported":
        return {"loose": UNSUPPORTED, "strict": UNSUPPORTED, "detail": resp.get("reason", "")}
    if status not in ("ok", "error"):
        d = f"protocol: bad status {status!r}"
        return {"loose": FAIL, "strict": FAIL, "detail": d}

    kind = expect["kind"]
    err = resp.get("error") or {}

    if kind == "bounded":
        return _bounded(case, expect, resp, caps)

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
