#!/bin/sh
# Starts the built Go adapter of the current directory. It replaces this shell, so the
# library's process is the one the kit measures.
set -eu
name=$(basename "$(pwd)")
bin=${KIT_GO_BIN:-$(cd "$(dirname "$0")" && pwd)/.bin}
exec "$bin/$name"
