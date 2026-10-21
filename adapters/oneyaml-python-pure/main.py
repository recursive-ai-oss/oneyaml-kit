import sys
from pathlib import Path

import oneyaml
import pykit

# The revision of the checkout the build recorded (the same rule as the other linked adapters).
rev = (Path(sys.prefix) / ".kit-rev").read_text().strip()

hello = {
    "name": "oneyaml-python-pure", "version": rev, "language": "python",
    "capabilities": [], "emulates": [],
    "info": {"python": pykit.python_version(), "package": oneyaml.__version__,
             "backend": oneyaml.backend, "engine": oneyaml.engine_version()},
}
# The library has no features yet, so no operation is offered.
pykit.serve(hello, {})
