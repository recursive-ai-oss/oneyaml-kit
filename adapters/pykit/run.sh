#!/bin/sh
# Starts the Python adapter of the current directory. It replaces this shell, so the process the
# kit measures for memory is the interpreter running the library.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
. "$here/env.sh"
dir=$(pwd)
name=$(basename "$dir")
case $name in
oneyaml-python) venv=$PYENV_ROOT/$name; export ONEYAML_BACKEND=native ;;
oneyaml-python-pure) venv=$PYENV_ROOT/$name; export ONEYAML_BACKEND=pure ;;
*) venv=$PYENV_ROOT/libs ;;
esac
PYTHONPATH=$here PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 exec "$venv/bin/python" -B "$dir/main.py"
