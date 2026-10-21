from importlib import metadata

import textlibs
import yaml12
from pykit import Tagged


class ParseError(Exception):
    pass


def load_all(text):
    try:
        return yaml12.parse_yaml(text, multi=True)
    except ValueError as e:
        raise ParseError(str(e)) from None


def hook(o):
    # A tagged node (or an unhashable key) without a handler comes back wrapped.
    if type(o) is yaml12.Yaml:
        return Tagged(o.tag, o.value) if o.tag else o.value
    return None


textlibs.run("py-yaml12", metadata.version("py-yaml12"), load_all=load_all, dump=yaml12.format_yaml,
             own_errors=(ParseError,), hook=hook)
