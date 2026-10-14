#!/bin/sh
# Builds the harness. $1 is the build directory the driver chose; build output goes under it and
# never into the source tree. DOTNET names the dotnet executable when it is not on PATH.
# SELFTEST=1 adds the deliberately wrong control libraries, in an assembly of its own.
set -eu
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
dir=$(cd "$(dirname "$0")" && pwd)
build=${1:?usage: build.sh BUILD_DIR}
# Build properties are kept as separate words (not one string) so a path with spaces stays one argument.
set --
art=$build/dotnet
if [ "${SELFTEST:-}" = 1 ]; then set -- "$@" -p:Selftest=true; art=$build/dotnet-selftest; fi
# With KIT_ONEYAML_CSHARP_SRC naming a checkout of oneyaml-csharp the harness measures it too; the
# commit is stamped in as the library's version. Without it the build is the competitors alone.
linked=
if [ -n "${KIT_ONEYAML_CSHARP_SRC:-}" ]; then
	src=$(cd "$KIT_ONEYAML_CSHARP_SRC" && pwd)
	test -f "$src/src/OneYaml/OneYaml.csproj" || { echo "no src/OneYaml/OneYaml.csproj in KIT_ONEYAML_CSHARP_SRC" >&2; exit 1; }
	rev=devel
	if [ "$(git -C "$src" rev-parse --show-toplevel 2>/dev/null)" = "$src" ]; then
		rev=$(git -C "$src" rev-parse --short=12 HEAD 2>/dev/null || echo devel)
	fi
	set -- "$@" "-p:OneYamlSrc=$src" "-p:OneYamlRevision=$rev"
	linked=1
	# A stale file from an earlier build must not stand in for this restore's result.
	rm -f "$art/obj/Bench/packages.linked.lock.json"
fi
if [ -z "$linked" ]; then
	KIT_DOTNET_ARTIFACTS=$art exec "${DOTNET:-dotnet}" build "$dir/Bench.csproj" -c Release \
	  --artifacts-path "$art" --nologo -v quiet -clp:NoSummary -nodeReuse:false \
	  -p:UseSharedCompilation=false "$@"
fi
KIT_DOTNET_ARTIFACTS=$art "${DOTNET:-dotnet}" build "$dir/Bench.csproj" -c Release \
  --artifacts-path "$art" --nologo -v quiet -clp:NoSummary -nodeReuse:false \
  -p:UseSharedCompilation=false "$@"
# The linked restore is not locked, so check it against the committed lock file: every locked
# package, direct or transitive, must resolve to the same version. Fails the build otherwise.
exec python3 "$dir/check_linked_lock.py" "$dir/packages.lock.json" "$art/obj/Bench/packages.linked.lock.json"
