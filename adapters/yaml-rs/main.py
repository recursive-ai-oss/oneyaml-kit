from importlib import metadata

import textlibs
import yaml_rs
from pykit import Declined


def load_all(text):
    """yaml_rs.loads gives one document as itself and several as a list, so a list is ambiguous
    (a sequence, or several documents). A second parse with an empty document appended settles it:
    several documents gain a null at the end, a single document becomes [document, null]. Anything
    else (the library read the appended marker as text) cannot be told apart and is declined."""
    r = yaml_rs.loads(text)
    if r is not None and not isinstance(r, list):
        return [r]
    probe = yaml_rs.loads(text + "\n---\n")
    if r is None:
        # an empty stream has no document; a document that is null is one
        if probe is None:
            return []
        if probe == [None, None]:
            return [None]
    elif isinstance(probe, list) and probe and probe[-1] is None:
        if probe == [r, None]:
            return [r]
        if len(r) >= 2 and probe[:-1] == r:
            return r
    raise Declined("the library returns a list for one sequence and for several documents")


textlibs.run("yaml-rs", metadata.version("yaml-rs"), load_all=load_all, accept=yaml_rs.loads, dump=yaml_rs.dumps,
             own_errors=(yaml_rs.YAMLDecodeError, yaml_rs.YAMLEncodeError),
             info={"parse_datetime": True})
