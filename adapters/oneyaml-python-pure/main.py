import sys
from pathlib import Path

import oneyaml
import pykit
from pykit import Failure, ok

# The revision of the checkout the build recorded (the same rule as the other linked adapters).
rev = (Path(sys.prefix) / ".kit-rev").read_text().strip()


def failure_of(e):
    return Failure(e.category, e.message, e.line, e.column)


def classify(e):
    # Only the library's own exception is a rejection of the input; anything else is a crash.
    return failure_of(e) if isinstance(e, oneyaml.YAMLError) else None


def hook(obj):
    # Values the kit notation has no name for: a tagged node, and the read-only mapping that
    # stands for a mapping used as a key.
    if isinstance(obj, oneyaml.Tagged):
        return pykit.Tagged(obj.tag, obj.value)
    if isinstance(obj, oneyaml.FrozenMapping):
        return dict(obj)
    return None


def events(data, args):
    try:
        # The backend's own event notation (an internal call: the public API has no event stream).
        return ok(events=oneyaml._impl.events_text(data))
    except oneyaml.YAMLError as e:
        f = failure_of(e)
        f.events = e.events
        raise f from None


def value(data, args):
    docs = oneyaml.loads_all(data)
    return ok(value=pykit.JSONText(pykit.documents_json(docs, hook)))


hello = {
    "name": "oneyaml-python-pure", "version": rev, "language": "python",
    "capabilities": ["events", "error-events", "value", "error-category", "error-position"],
    "emulates": [],
    "info": {"python": pykit.python_version(), "package": oneyaml.__version__,
             "backend": oneyaml.backend, "engine": oneyaml.engine_version()},
}
pykit.serve(hello, {"events": events, "value": value}, classify)
