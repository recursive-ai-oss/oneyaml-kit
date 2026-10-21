"""Typed models for the typed-load task.

Python has no typed YAML decoding in any of the libraries measured (typed binding is left to the
caller: a dataclass, pydantic or msgspec), so typed loading here is what a program does today: the
library's generic load, then a plain conversion into dataclasses. The conversion is this one function
for every library and is inside the timed work. A field that the document does not have, and that is
marked optional, keeps MISSING and is left out of the common form, the way an omitted field would be.
"""

from __future__ import annotations

import dataclasses
import typing
from dataclasses import dataclass, field

from common import MISSING


def key(name, default):
    """A field whose YAML key is not a Python identifier (or is a keyword)."""
    return field(default=default, metadata={"key": name})


@dataclass
class Geo:
    lat: float = 0.0
    lng: float = 0.0


@dataclass
class Address:
    city: str = ""
    zip: str = ""
    geo: typing.Optional[Geo] = MISSING


@dataclass
class Record:
    id: int = 0
    name: str = ""
    email: str = ""
    active: bool = False
    score: float = 0.0
    tags: list[str] = field(default_factory=list)
    address: Address = field(default_factory=Address)
    note: typing.Optional[str] = None


@dataclass
class Push:
    branches: list[str] = field(default_factory=list)


@dataclass
class On:
    pull_request: typing.Optional[str] = None
    push: Push = field(default_factory=Push)


@dataclass
class Matrix:
    runs_on: list[str] = field(default_factory=list, metadata={"key": "runs-on"})


@dataclass
class Strategy:
    matrix: Matrix = field(default_factory=Matrix)


@dataclass
class Service:
    image: str = ""
    ports: list[str] = field(default_factory=list)


@dataclass
class Step:
    id: typing.Optional[str] = MISSING
    name: typing.Optional[str] = MISSING
    if_: typing.Optional[str] = key("if", MISSING)
    uses: typing.Optional[str] = MISSING
    run: typing.Optional[str] = MISSING
    shell: typing.Optional[str] = MISSING
    with_: typing.Optional[dict] = key("with", MISSING)


@dataclass
class Job:
    runs_on: str = key("runs-on", "")
    strategy: typing.Optional[Strategy] = MISSING
    container: typing.Any = MISSING
    services: typing.Optional[dict[str, Service]] = MISSING
    env: typing.Optional[dict[str, str]] = MISSING
    steps: list[Step] = field(default_factory=list)


@dataclass
class Workflow:
    name: str = ""
    on: On = field(default_factory=On)
    jobs: dict[str, Job] = field(default_factory=dict)


_HINTS: dict = {}


def _hints(cls) -> dict:
    h = _HINTS.get(cls)
    if h is None:
        h = _HINTS[cls] = typing.get_type_hints(cls)
    return h


def _build(tp, v):
    origin = typing.get_origin(tp)
    if origin is typing.Union:
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return None if v is None else _build(args[0], v)
    if tp is typing.Any:
        return v
    if origin is list:
        (item,) = typing.get_args(tp)
        return [_build(item, x) for x in v]
    if origin is dict:
        _, item = typing.get_args(tp)
        return {k: _build(item, x) for k, x in v.items()}
    if dataclasses.is_dataclass(tp):
        return build(tp, v)
    if tp is float and isinstance(v, int) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, tp) or (tp is int and isinstance(v, bool)):
        raise TypeError(f"{type(v).__name__} where {tp.__name__} is expected")
    return v


def build(cls, data):
    """A dataclass from a decoded mapping. Keys the model does not have are ignored; a value of the wrong
    type is an error."""
    if not isinstance(data, dict):
        raise TypeError(f"{type(data).__name__} where a mapping is expected")
    hints = _hints(cls)
    kw = {}
    for f in dataclasses.fields(cls):
        key = f.metadata.get("key", f.name)
        if key in data:
            kw[f.name] = _build(hints[f.name], data[key])
    return cls(**kw)


def convert(model: str, data):
    """The value `data` as the workload's typed model."""
    if model == "records":
        return [build(Record, d) for d in data]
    if model == "gha":
        return build(Workflow, data)
    raise ValueError(f"no typed model {model!r}")


MODELS = ("gha", "records")
