#!/bin/sh
# Builds the shared environment of the Python adapters: one virtual environment with every
# library pinned in libs.lock (exact versions and hashes, binary wheels only, so nothing is
# compiled). Needs uv. The environment lives outside the checkout:
#   $KIT_PY_ENV, else next to the kit's build output directory, else the user cache.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
. "$here/env.sh"
venv=$PYENV_ROOT/libs
want=$(cat "$here/libs.lock" "$here/env.sh" | sha256sum | cut -c1-16)

lock "$PYENV_ROOT/libs.lock"
if [ -x "$venv/bin/python" ] && [ "$(cat "$venv/.kit-stamp" 2>/dev/null)" = "$want" ]; then
	exit 0
fi
rm -rf "$venv"
uv venv --quiet --managed-python --python "$PY_VERSION" "$venv"
uv pip sync --quiet --python "$venv/bin/python" --require-hashes --only-binary :all: "$here/libs.lock"
# PyYAML's C loader is the point of one adapter: a wheel without libyaml must fail the build.
"$venv/bin/python" -c 'import yaml, sys; sys.exit(0 if yaml.__with_libyaml__ else "PyYAML has no libyaml")'
echo "$want" > "$venv/.kit-stamp"
