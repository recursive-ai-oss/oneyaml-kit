#!/bin/sh
# Builds the Python harness's environment in the directory given as the first argument: a CPython 3.13
# virtual environment (uv) holding exactly the libraries in libraries.lock. When
# KIT_ONEYAML_PYTHON_SRC names a checkout of oneyaml-python, the library is installed from it as well
# (a native wheel through its own build backend), and the revision is recorded beside the environment;
# without it the harness measures the competitors alone.
set -eu
out=${1:?usage: build.sh <build directory>}
mkdir -p "$out"
out=$(cd "$out" && pwd)
venv=$out/oneyaml-bench-python-venv
py=$venv/bin/python
if [ ! -x "$py" ]; then
	uv venv --quiet --python 3.13 --python-preference only-managed "$venv"
fi
uv pip install --quiet --python "$py" --require-hashes -r libraries.lock
record=$venv/oneyaml-bench-own.json
rm -f "$record"
if [ -n "${KIT_ONEYAML_PYTHON_SRC:-}" ]; then
	src=$(cd "$KIT_ONEYAML_PYTHON_SRC" && pwd)
	# The revision the report shows. tools/library-rev.sh holds the rule: a git checkout gives its commit
	# (with +modified when tracked files changed), an export gives devel, and KIT_ONEYAML_PYTHON_REV
	# overrides both.
	rev=$(sh "$(dirname "$0")/../../tools/library-rev.sh" PYTHON "$src")
	rev=${rev%% *}
	uv pip install --quiet --python "$py" --no-deps --reinstall "$src"
	printf '{"revision": "%s"}\n' "$rev" >"$record"
fi
