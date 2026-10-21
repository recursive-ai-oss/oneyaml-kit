"""The correctness guard: judges what a library produced before anything is timed.

The comparison is complete: the whole value, every output byte. A library that fails the guard
gets a verdict and a reason in the results and no numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

from common import VALUE, FailedPatch, Product, Workload, bytes_diff, clip, first_diff


@dataclass
class Verdict:
    status: str          # ok, differs, rejects, unjudged
    detail: str = ""
    variant: str = ""


def ok(variant: str) -> Verdict:
    return Verdict("ok", "", variant)


def match_value(w: Workload, got) -> Verdict:
    diffs = []
    for name, want in w.expected:
        d = first_diff(got, want)
        if not d:
            return ok(name)
        diffs.append(d if name == "spec" else f"{name}: {d}")
    return Verdict("differs", "; ".join(diffs))


def match_bytes(got: bytes, want: list, what: str) -> Verdict:
    first = ""
    for name, b in want:
        d = bytes_diff(got, b)
        if not d:
            return ok(name)
        first = first or d
    return Verdict("differs", first or f"workload has no expected {what}")


def judge(task: str, w: Workload, product: Product | None, error: BaseException | None,
          failing: FailedPatch | None, referee) -> Verdict:
    """One library's answer to one task. `referee(bytes)` returns the value the reference parsers read
    from a text, or raises RefereeError."""
    if error is not None:
        return Verdict("rejects", clip(f"{type(error).__name__}: {error}"))
    p = product
    if task in ("typed-load", "generic-load"):
        if p.kind != VALUE:
            return Verdict("differs", "the runner gave no value")
        return match_value(w, p.value)
    if task == "dump":
        return _judge_dump(w, p, referee)
    if task == "document-parse":
        # A lossless document must hold the value and write the input back unchanged.
        by = match_value(w, p.value) if p.has_value else ok("bytes-only")
        d = bytes_diff(p.data, w.input)
        if by.status == "ok" and not d:
            return by
        if by.status == "ok":
            return Verdict("differs", "does not write the input back: " + d)
        return by
    if task == "unchanged-emit":
        d = bytes_diff(p.data, w.input)
        return Verdict("differs", d) if d else ok("spec")
    if task == "edit-script":
        return match_bytes(p.data, w.edit_expected, "edit output")
    if task == "patch":
        v = match_bytes(p.data, w.patch_expected, "patch output")
        if v.status == "differs" and p.value_only:
            v = _value_only(p, w, referee, v)
        if v.status != "ok":
            return v
        if failing is None:
            return Verdict("differs", "the runner did not apply the failing patch")
        if not failing.errored:
            return Verdict("differs", "a patch with a failing step reported success")
        d = bytes_diff(failing.data, w.input)
        return Verdict("differs", "a failed patch changed the document: " + d) if d else v
    return Verdict("differs", f"task {task} has no judge")


def _read(referee, data: bytes):
    """(value, None) or (None, Verdict) for a text the reference parsers could not read one way."""
    from referee import RefereeError

    try:
        return referee(data), None
    except RefereeError as e:
        if e.rejected:
            return None, Verdict("differs", "the output does not parse: " + clip(str(e)))
        return None, Verdict("unjudged", clip(str(e)))


def _judge_dump(w: Workload, p: Product, referee) -> Verdict:
    got, bad = _read(referee, p.data)
    if bad:
        return bad
    # Read back, the output must hold the value, or the merged value when the library applies merge
    # keys while loading.
    v = match_value(w, got)
    if v.status == "differs":
        v.detail = "read back: " + v.detail
    return v


def _value_only(p: Product, w: Workload, referee, exact: Verdict) -> Verdict:
    """The output of a library that cannot keep the text: both the output and the expected bytes are
    read by the reference parsers and the values must be equal. The result is the variant
    `value-only`, which is a known deviation, never a plain pass."""
    got, bad = _read(referee, p.data)
    if bad:
        return bad
    for _, b in w.patch_expected:
        want, bad = _read(referee, b)
        if bad:
            return Verdict("unjudged", "the expected bytes cannot be read: " + bad.detail)
        d = first_diff(got, want)
        if d:
            return Verdict("differs", "value after the patch: " + d)
        return ok("value-only")
    return exact
