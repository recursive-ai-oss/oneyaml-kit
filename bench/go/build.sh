#!/bin/sh
# Builds the Go harness into the directory given as the first argument. When
# KIT_ONEYAML_GO_SRC names a checkout of oneyaml-go (any directory with its go.mod), the
# library is linked in as well, through a module file generated in the build directory, so
# no tracked go.mod changes. Without it the harness measures the competitors alone.
set -eu
out=${1:?usage: build.sh <build directory>}
mkdir -p "$out"
out=$(cd "$out" && pwd)
if [ -z "${KIT_ONEYAML_GO_SRC:-}" ]; then
	exec go build -trimpath -mod=readonly -o "$out/oneyaml-bench-go" .
fi
src=$(cd "$KIT_ONEYAML_GO_SRC" && pwd)
test -f "$src/go.mod" || { echo "no go.mod in KIT_ONEYAML_GO_SRC" >&2; exit 1; }
mod=$out/oneyaml-bench-go.mod
cp go.mod "$mod"
cp go.sum "$out/oneyaml-bench-go.sum"
printf '\nrequire oneyaml.dev/go v0.0.0\n\nreplace oneyaml.dev/go => %s\n' "$src" >> "$mod"
# The version the report shows. tools/library-rev.sh holds the rule: a git checkout gives its commit
# (with +modified when tracked files changed), an export gives devel, and KIT_ONEYAML_GO_REV
# overrides both.
rev=$(sh "$(dirname "$0")/../../tools/library-rev.sh" GO "$src")
rev=${rev%% *}
exec go build -trimpath -mod=readonly -modfile="$mod" -tags oneyaml_go_src \
	-ldflags "-X main.sourceRevision=$rev" -o "$out/oneyaml-bench-go" .
