"""Which tasks a harness times from text to text.

A library whose API takes and returns text has no parsed document to start from, so its parsing and
writing are inside its timed work and its cells carry the "text API" mark. Some harnesses go further
and time a whole task that way for every library, because the task is only defined on text (the Go
patch task). The same work gets the same mark, so those cells are marked for all libraries, ours
included. The mark never enters a verdict: it only says what a cell timed.
"""

from __future__ import annotations

# Harnesses that recorded no `text_tasks` but are known to have timed these tasks from text. Only the
# exact version that was checked is listed; a newer harness says so itself in its record.
KNOWN = {("oneyaml-bench-go", "0.2.0"): ("patch",)}


def text_tasks(harness: dict) -> tuple[str, ...]:
    if "text_tasks" in harness:
        return tuple(harness["text_tasks"])
    return KNOWN.get((harness.get("name"), harness.get("version")), ())


def mark_text_tasks(result: dict) -> dict:
    """Sets `state` to `text` on every cell of a text task. Changes the result in place and returns it."""
    for c in result["cells"]:
        if c["task"] in text_tasks(result["harnesses"].get(c["language"], {})):
            c["state"] = "text"
    return result
