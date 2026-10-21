#!/bin/sh
# Builds the oneyaml-python adapters against a checkout of the library. KIT_ONEYAML_PYTHON_SRC
# names the checkout (a clone, a snapshot or a worktree). Run from the adapter's directory:
#   oneyaml-python       the native backend, built with maturin (needs a Rust toolchain)
#   oneyaml-python-pure  the pure-Python backend, from the checkout's pure wheel
# Each gets its own virtual environment outside the checkout (see env.sh).
set -eu
src=${KIT_ONEYAML_PYTHON_SRC:?KIT_ONEYAML_PYTHON_SRC must name an oneyaml-python checkout}
src=$(cd "$src" && pwd)
test -f "$src/pyproject.toml" || { echo "no pyproject.toml in KIT_ONEYAML_PYTHON_SRC" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
. "$here/env.sh"
name=$(basename "$(pwd)")
venv=$PYENV_ROOT/$name
# The version the report shows. tools/library-rev.sh holds the rule: a git checkout gives its commit
# (with +modified when tracked files changed), an export gives devel.
rev=$(sh "$here/../../tools/library-rev.sh" PYTHON "$src")
rev=${rev%% *}

lock "$PYENV_ROOT/$name.lock"
rm -rf "$venv"
uv venv --quiet --managed-python --python "$PY_VERSION" "$venv"
py=$venv/bin/python
work=$venv/.build
mkdir -p "$work"
case $name in
oneyaml-python)
	uv pip sync --quiet --python "$py" --require-hashes --only-binary :all: "$here/tools.lock"
	(cd "$src" && "$venv/bin/maturin" build --quiet --profile dev --out "$work/wheels")
	;;
oneyaml-python-pure)
	test -f "$src/tools/build_pure.py" || { echo "no tools/build_pure.py in the checkout" >&2; exit 1; }
	"$py" "$src/tools/build_pure.py" "$work/wheels" >/dev/null
	;;
*)
	echo "unknown adapter $name" >&2
	exit 1
	;;
esac
uv pip install --quiet --python "$py" --no-deps "$work"/wheels/*.whl
rm -rf "$work"
echo "$rev" > "$venv/.kit-rev"
