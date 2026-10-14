#!/bin/sh
# Builds the harness. $1 is the build directory the driver chose; build output goes under it and
# never into the source tree. DOTNET names the dotnet executable when it is not on PATH.
# SELFTEST=1 adds the deliberately wrong control libraries, in an assembly of its own.
set -eu
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
dir=$(cd "$(dirname "$0")" && pwd)
build=${1:?usage: build.sh BUILD_DIR}
art=$build/dotnet
extra=
if [ "${SELFTEST:-}" = 1 ]; then extra=-p:Selftest=true; art=$build/dotnet-selftest; fi
# With KIT_ONEYAML_CSHARP_SRC naming a checkout of oneyaml-csharp the harness measures it too; the
# commit is stamped in as the library's version. Without it the build is the competitors alone.
link=
if [ -n "${KIT_ONEYAML_CSHARP_SRC:-}" ]; then
	src=$(cd "$KIT_ONEYAML_CSHARP_SRC" && pwd)
	test -f "$src/src/OneYaml/OneYaml.csproj" || { echo "no src/OneYaml/OneYaml.csproj in KIT_ONEYAML_CSHARP_SRC" >&2; exit 1; }
	rev=devel
	if [ "$(git -C "$src" rev-parse --show-toplevel 2>/dev/null)" = "$src" ]; then
		rev=$(git -C "$src" rev-parse --short=12 HEAD 2>/dev/null || echo devel)
	fi
	link="-p:OneYamlSrc=$src -p:OneYamlRevision=$rev"
fi
KIT_DOTNET_ARTIFACTS=$art exec "${DOTNET:-dotnet}" build "$dir/Bench.csproj" -c Release \
  --artifacts-path "$art" --nologo -v quiet -clp:NoSummary -nodeReuse:false \
  -p:UseSharedCompilation=false $extra $link
