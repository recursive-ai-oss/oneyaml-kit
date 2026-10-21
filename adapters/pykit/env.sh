# Sourced by the Python adapters' build and run scripts.
#   PYENV_ROOT  where the virtual environments live (outside the checkout)
#   PY_VERSION  the interpreter every adapter runs on
#   lock FILE   waits for an exclusive lock on FILE (several adapters may build at once)
if [ -z "${KIT_PY_ENV:-}" ]; then
	if [ -n "${CARGO_TARGET_DIR:-}" ]; then
		KIT_PY_ENV=$(dirname "$CARGO_TARGET_DIR")/pyenv
	else
		KIT_PY_ENV=${XDG_CACHE_HOME:-$HOME/.cache}/oneyaml-kit/pyenv
	fi
fi
mkdir -p "$KIT_PY_ENV"
PYENV_ROOT=$(cd "$KIT_PY_ENV" && pwd)
PY_VERSION=3.13

lock() {
	n=0
	until mkdir "$1.d" 2>/dev/null; do
		n=$((n + 1))
		# A lock left by a killed build is older than any build takes.
		if [ "$n" -gt 600 ]; then rm -rf "$1.d"; fi
		sleep 1
	done
	trap 'rmdir "'"$1"'.d"' EXIT
}
