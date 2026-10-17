#!/bin/sh
# Builds the oneyaml-go adapter against a checkout of the library. KIT_ONEYAML_GO_SRC names
# the checkout (any directory that holds the library's go.mod: a clone, a snapshot, a
# worktree). The library is linked through a module file generated in the build output
# directory, so no tracked go.mod changes. KIT_GO_BIN says where the binary goes.
set -eu
src=${KIT_ONEYAML_GO_SRC:?KIT_ONEYAML_GO_SRC must name an oneyaml-go checkout}
src=$(cd "$src" && pwd)
test -f "$src/go.mod" || { echo "no go.mod in KIT_ONEYAML_GO_SRC" >&2; exit 1; }
here=$(pwd)
kit=$(cd "$here/../gokit" && pwd)
bin=${KIT_GO_BIN:-$kit/.bin}
mkdir -p "$bin"
bin=$(cd "$bin" && pwd)
mod=$bin/oneyaml-go.mod
cat > "$mod" <<EOF
module example.invalid/oneyaml-kit/adapters/oneyaml-go

go 1.26

require (
	example.invalid/oneyaml-kit/gokit v0.0.0
	oneyaml.dev/go v0.0.0
)

replace example.invalid/oneyaml-kit/gokit => $kit

replace oneyaml.dev/go => $src
EOF
# The version the report shows. tools/library-rev.sh holds the rule: a git checkout gives its commit
# (with +modified when tracked files changed), an export gives devel, and KIT_ONEYAML_GO_REV
# overrides both.
rev=$(sh "$(dirname "$0")/../../tools/library-rev.sh" GO "$src")
rev=${rev%% *}
exec go build -trimpath -modfile="$mod" -tags oneyaml_go_src \
	-ldflags "-X main.version=$rev" -o "$bin/oneyaml-go" .
