"""Run cases through an adapter and produce the result document."""

from __future__ import annotations

import datetime
import json
import os
import platform
import subprocess
import time
from pathlib import Path

from . import compare
from .cases import (Case, ROOT, YTS_RELEASE, SUITE_YTS, corpus_digest, current_pins, kit_version,
                    slow_ids, yts_revision)
from . import vocabulary
from .protocol import PROTOCOL, SUPPORTED_PROTOCOLS, SubprocessAdapter

RESULT_FORMAT = "oneyaml-kit-results/2"
ADAPTER_FORMAT = "oneyaml-kit-adapter/1"
DEVIATIONS_FORMAT = "oneyaml-kit-deviations/1"
ADAPTERS = ROOT / "adapters"
MODES = ("loose", "strict")
EXPECTATIONS = ("primary", "corrected")


class RunError(Exception):
    pass


class AdapterUnavailable(RunError):
    """The adapter cannot run on this machine because something it needs is not there (see
    `requires_env` in the manifest). This is not a failure of the adapter: callers report the
    adapter as skipped and say why."""


def load_manifest(name: str, adapters_dir: Path = ADAPTERS) -> dict:
    d = adapters_dir / name
    try:
        m = json.loads((d / "adapter.json").read_bytes())
    except (OSError, ValueError) as e:
        raise RunError(f"adapter {name}: cannot read adapter.json: {e}") from e
    if m.get("format") != ADAPTER_FORMAT:
        raise RunError(f"adapter {name}: format must be {ADAPTER_FORMAT!r}")
    m["_dir"] = d
    return m


def list_adapters(adapters_dir: Path = ADAPTERS) -> list[str]:
    if not adapters_dir.is_dir():
        return []
    return sorted(p.name for p in adapters_dir.iterdir() if (p / "adapter.json").is_file())


def load_deviations(manifest: dict) -> list[dict]:
    rel = manifest.get("deviations")
    if not rel:
        return []
    d = json.loads((manifest["_dir"] / rel).read_bytes())
    if d.get("format") != DEVIATIONS_FORMAT:
        raise RunError(f"deviations: format must be {DEVIATIONS_FORMAT!r}")
    out = []
    seen = set()
    for e in d["entries"]:
        extra = set(e) - {"case", "expectation", "mode", "verdict", "reason", "near_bound"}
        if extra:
            raise RunError(f"deviations: unknown keys {sorted(extra)} in {e.get('case')}")
        e.setdefault("expectation", "primary")
        if (e["expectation"] not in EXPECTATIONS or e.get("mode") not in MODES
                or e.get("verdict") not in (compare.FAIL, compare.UNSUPPORTED)
                or not e.get("reason") or not e.get("case")
                or not isinstance(e.get("near_bound", False), bool)):
            raise RunError(f"deviations: malformed entry {e!r}")
        key = (e["case"], e["expectation"], e["mode"])
        if key in seen:
            raise RunError(f"deviations: duplicate entry {key}")
        seen.add(key)
        out.append(e)
    return out


def unavailable_reason(manifest: dict, env: dict) -> str | None:
    """Why the adapter cannot run here, or None. `requires_env` lists environment variables
    that must be set and non-empty; an adapter for a library that is not publicly fetchable
    uses it to point at a local checkout."""
    missing = [v for v in manifest.get("requires_env", []) if not env.get(v)]
    if missing:
        return "environment variable " + ", ".join(missing) + " is not set"
    return None


def build(manifest: dict, env: dict) -> None:
    cmd = manifest.get("build")
    if not cmd:
        return
    r = subprocess.run(cmd, cwd=manifest["_dir"], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RunError(f"adapter build failed:\n{r.stdout[-2000:]}\n{r.stderr[-4000:]}")


def _load() -> float | None:
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def limits_of(case: Case) -> dict | None:
    """The bounds to enforce for a case, exactly as the case states them. The time bound is a
    hang guard, generous by design; the memory bound is the real check."""
    e = case.expect
    if e["kind"] == "bounded":
        return {"time_seconds": e["time_seconds"], "memory_mb": e["memory_mb"]}
    return None


def evaluate_case(case: Case, resp: dict, caps: set, wall_us: int, emulates: set = frozenset(),
                  protocol: int = PROTOCOL) -> dict:
    base, _ = compare.required_caps(case, case.expect)
    r = {
        "id": case.id, "suite": case.suite, "category": case.category, "op": case.op,
        "expect": case.expect["kind"], "micros": wall_us,
    }
    if resp.get("status") == "accepted" and protocol < 2:
        resp = {"status": "crash", "detail": "protocol: `accepted` is an answer of protocol 2; "
                                           "this adapter said hello with protocol 1"}
    emu = resp.get("emulated") or []
    if not isinstance(emu, list) or not set(emu) <= set(emulates):
        resp = {"status": "crash", "detail": f"protocol: response claims emulated behaviours {emu!r} "
                                           f"that the hello did not declare"}
        emu = []
    if resp.get("status") == "unsupported":
        r.update(loose=compare.UNSUPPORTED, strict=compare.UNSUPPORTED,
                 reason="adapter", detail=resp.get("reason", ""))
    elif not compare.satisfied(base, caps):
        r.update(loose=compare.UNSUPPORTED, strict=compare.UNSUPPORTED, reason="capability",
                 detail="missing " + ", ".join(sorted(b for b in base if not compare.satisfied({b}, caps))))
    else:
        v = compare.evaluate(case, case.expect, resp, caps)
        r.update(loose=v["loose"], strict=v["strict"], detail=v["detail"])
        if v.get("reason"):
            r["reason"] = v["reason"]
        if v["strict"] == compare.UNSUPPORTED and not v.get("reason"):
            r["reason"] = "capability"
            _, extra = compare.required_caps(case, case.expect)
            missing = sorted(x for x in extra if x not in caps)
            if missing:
                r["detail"] = (v["detail"] + "; " if v["detail"] else "") + "strict needs " + ", ".join(missing)
        if case.expect["kind"] in ("error", "bounded"):
            r["got"] = compare.summarize(resp)
        if resp.get("_measure"):
            m = resp["_measure"]
            r["measured"] = {"seconds": round(m["seconds"], 3),
                             "time_bound": m.get("time_bound", case.expect.get("time_seconds")),
                             "peak_mb": None if m["peak_mb"] is None else round(m["peak_mb"], 1)}
    if emu:
        r["emulated"] = sorted(emu)
    if case.suite == "own" and case.tags:
        # Carried into the record so a report can show which verdicts rest on a convention.
        r["tags"] = sorted(case.tags)
    if case.correction and r["loose"] != compare.UNSUPPORTED:
        cv = compare.evaluate(case, case.correction["expect"], resp, caps)
        r["corrected"] = {"loose": cv["loose"], "strict": cv["strict"], "detail": cv["detail"]}
    return r


def verify_deviations(results: list[dict], deviations: list[dict], complete: bool,
                      deferred: frozenset | set | None = None) -> dict:
    """Every non-passing verdict must be listed, and every listed entry must still hold.
    `deferred` holds the ids of cases a run may leave out because they are slow (by default
    every case tagged slow): an entry for one of them that is not in the results is neither
    checked nor unknown, but an entry for an id that is no case at all still is."""
    if deferred is None:
        deferred = slow_ids()
    by_id = {r["id"]: r for r in results}
    listed = {(e["case"], e["expectation"], e["mode"]): e for e in deviations}
    unlisted, stale, unknown = [], [], []
    near_bound_passed = []
    for key, e in listed.items():
        r = by_id.get(key[0])
        if r is None:
            if complete and key[0] not in deferred:
                unknown.append(f"{key[0]}: not a case of this run")
            continue
        src = r if key[1] == "primary" else r.get("corrected")
        if src is None:
            stale.append(f"{key[0]} ({key[1]}/{key[2]}): case has no corrected expectation")
            continue
        actual = src[key[2]]
        label = f"{key[0]} ({key[1]}/{key[2]})"
        if e.get("near_bound"):
            # Only a failed time bound can land on either side of the limit from run to run.
            if e["verdict"] != compare.FAIL or r.get("expect") != "bounded":
                stale.append(f"{label}: invalid near_bound: allowed only on a fail entry for a "
                             "case with a time bound")
                continue
            m = r.get("measured")
            if actual in (compare.PASS, compare.FAIL):
                # The mark only covers a run that came near the bound; a faster pass means the
                # library got quicker, and a faster failure is some other failure.
                if not m or not m.get("time_bound"):
                    stale.append(f"{label}: marked near the time bound, but the run recorded no time")
                    continue
                if m["seconds"] < m["time_bound"] / 2:
                    stale.append(f"{label}: {actual}ed in {m['seconds']:g} s of a {m['time_bound']:g} s "
                                 "bound, so it is no longer near the bound")
                    continue
            if actual == compare.PASS:
                note = f"{key[0]} {m['seconds']:g} s of {m['time_bound']:g} s"
                if note not in near_bound_passed:
                    near_bound_passed.append(note)
                continue
        if actual != e["verdict"]:
            stale.append(f"{key[0]} ({key[1]}/{key[2]}): listed as {e['verdict']}, now {actual}")
        elif actual == compare.UNSUPPORTED and r.get("reason") != "adapter":
            stale.append(f"{key[0]} ({key[1]}/{key[2]}): listed, but unsupported through a "
                         "capability the adapter does not declare")
    for r in results:
        for exp in EXPECTATIONS:
            src = r if exp == "primary" else r.get("corrected")
            if src is None:
                continue
            for mode in MODES:
                v = src[mode]
                if v == compare.PASS or (v == compare.UNSUPPORTED and r.get("reason") == "capability"):
                    continue
                if (r["id"], exp, mode) not in listed:
                    unlisted.append(f"{r['id']} ({exp}/{mode}): {v}: {src.get('detail') or r.get('detail', '')}"[:300])
    return {"listed": len(deviations), "unlisted": unlisted, "stale": stale, "unknown": unknown,
            "near_bound_passed": near_bound_passed,
            "ok": not (unlisted or stale or unknown)}


def execute(ad, caps: set, cases: list[Case], progress=None, emulates=(),
            protocol: int = PROTOCOL) -> list[dict]:
    """Send every case the adapter declared support for and judge the answers."""
    results = []
    emulates = set(emulates)
    for c in cases:
        t = time.monotonic()
        base, _ = compare.required_caps(c, c.expect)
        # A case the adapter has not declared support for is not sent at all.
        if compare.satisfied(base, caps):
            lim = limits_of(c)
            if lim and hasattr(ad, "run_fresh"):
                # A bounded case is measured in a process of its own, from a clean start.
                resp = ad.run_fresh(c.id, c.op, c.input, c.args, lim)
            elif lim:
                resp = ad.run(c.id, c.op, c.input, c.args, lim)
            else:
                resp = ad.run(c.id, c.op, c.input, c.args)
        else:
            resp = {"status": "skipped"}
        results.append(evaluate_case(c, resp, caps, int((time.monotonic() - t) * 1e6), emulates, protocol))
        if progress:
            progress(len(results), len(cases))
    return results


PROTOCOL2_CAPS = ("error-events",)


def check_hello(hello: dict) -> tuple[set, int]:
    """Validate an adapter's hello; return its capabilities (normalised) and protocol version.

    A protocol 1 adapter that declares `events` has always sent the events before an error,
    so it is credited with `error-events`; the newer capabilities need protocol 2."""
    name = hello.get("name")
    protocol = hello.get("protocol")
    if protocol not in SUPPORTED_PROTOCOLS:
        raise RunError(f"adapter {name}: protocol {protocol!r} is not one of {SUPPORTED_PROTOCOLS}")
    caps = hello.get("capabilities")
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        raise RunError(f"adapter {name}: capabilities must be a list of strings")
    bad = vocabulary.check(hello.get("emulates", []))
    if bad:
        raise RunError(f"adapter {name}: unknown emulated behaviours {bad}; the vocabulary is "
                       f"{sorted(vocabulary.EMULATES)}")
    caps = set(caps)
    if protocol == 1:
        new = sorted(c for c in caps if c in PROTOCOL2_CAPS or c.startswith("outcome:"))
        if new:
            raise RunError(f"adapter {name}: {new} need protocol 2")
        if "events" in caps:
            caps.add("error-events")
    return caps, protocol


def run_mode(cases: list[Case]) -> str:
    """Name the mode by the cases that actually ran, so a report never calls a run with slow cases fast."""
    return "full" if any("slow" in c.tags for c in cases) else "fast"


def run_adapter(name: str, cases: list[Case], complete: bool, adapters_dir: Path = ADAPTERS,
                timeout: float | None = None, env: dict | None = None,
                skip_build: bool = False, progress=None, out: Path | None = None) -> dict:
    manifest = load_manifest(name, adapters_dir)
    env = dict(os.environ if env is None else env)
    if out is not None:
        # Build output belongs in the output directory, never in the checkout.
        env.setdefault("CARGO_TARGET_DIR", str(Path(out) / "target"))
        env.setdefault("KIT_GO_BIN", str(Path(out) / "gobin"))
        env.setdefault("KIT_DOTNET_ARTIFACTS", str(Path(out) / "artifacts"))
    why = unavailable_reason(manifest, env)
    if why:
        raise AdapterUnavailable(f"adapter {name}: not available: {why}")
    if not skip_build:
        build(manifest, env)
    deviations = load_deviations(manifest)
    ad = SubprocessAdapter(manifest["command"], cwd=manifest["_dir"], env=env,
                           timeout=timeout or float(manifest.get("timeout_seconds", 10)),
                           memory_limit_mb=manifest.get("memory_limit_mb"))
    hello = ad.start()
    try:
        caps, protocol = check_hello(hello)
    except RunError:
        ad.close()
        raise
    # Every later start of this adapter must say the same hello.
    ad.expected_hello = hello
    t0 = time.monotonic()
    try:
        results = execute(ad, caps, cases, progress, hello.get("emulates", []), protocol)
    finally:
        ad.close()
    elapsed = time.monotonic() - t0
    pins = current_pins()
    return {
        "format": RESULT_FORMAT,
        "kit": {
            "version": kit_version(),
            "corpus_digest": corpus_digest(pins),
            "suites": {
                SUITE_YTS: {"release": YTS_RELEASE, "revision": yts_revision(),
                            "scored": True},
                "own": {"cases": len(pins["own"]["cases"])},
            },
        },
        "run": {
            "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
            "mode": run_mode(cases), "complete": complete, "cases": len(cases),
            "seconds": round(elapsed, 3), "adapter_restarts": ad.restarts,
            "bounded_sessions": ad.fresh_sessions,
            "system": {"os": platform.platform(), "machine": platform.machine(),
                       "cpus": os.cpu_count(), "load_1m": _load(),
                       "python": platform.python_version()},
        },
        "adapter": {
            "name": hello["name"], "version": hello["version"], "language": hello["language"],
            "capabilities": sorted(hello["capabilities"]), "emulates": sorted(hello.get("emulates", [])),
            "info": hello.get("info", {}),
            "protocol": hello["protocol"],
        },
        "cases": results,
        "deviations": {**verify_deviations(results, deviations, complete), "entries": deviations},
    }


def validate_result(res: dict) -> list[str]:
    """Structural check of a result document; returns the problems found."""
    problems = []
    if res.get("format") != RESULT_FORMAT:
        problems.append(f"format must be {RESULT_FORMAT!r}")
    for k in ("kit", "run", "adapter", "cases", "deviations"):
        if k not in res:
            problems.append(f"missing {k}")
    if problems:
        return problems
    for k in ("name", "version", "language", "capabilities", "protocol"):
        if k not in res["adapter"]:
            problems.append(f"adapter.{k} missing")
    seen = set()
    verdicts = (compare.PASS, compare.FAIL, compare.UNSUPPORTED)
    for r in res["cases"]:
        for k in ("id", "suite", "category", "op", "expect", "loose", "strict"):
            if k not in r:
                problems.append(f"case {r.get('id')}: missing {k}")
        if r.get("id") in seen:
            problems.append(f"duplicate case {r['id']}")
        seen.add(r.get("id"))
        t = r.get("tags", [])
        if not isinstance(t, list) or not all(isinstance(x, str) for x in t):
            problems.append(f"case {r.get('id')}: tags must be a list of strings")
        for m in MODES:
            if r.get(m) not in verdicts:
                problems.append(f"case {r.get('id')}: bad {m} verdict {r.get(m)!r}")
    return problems
