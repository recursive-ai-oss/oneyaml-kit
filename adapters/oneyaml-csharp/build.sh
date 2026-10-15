#!/bin/sh
# Builds the adapter against a checkout of oneyaml-csharp. KIT_ONEYAML_CSHARP_SRC names the
# checkout (any directory that holds src/OneYaml/OneYaml.csproj: a clone, a snapshot, a
# worktree). The library is linked by a project reference, so nothing tracked changes.
# KIT_DOTNET_ARTIFACTS says where the output goes (default: the kit output directory,
# out/artifacts). It is passed as the artifacts path, which also moves the library's own bin and
# obj folders, so nothing is written into the checkout or this folder.
# DOTNET names the dotnet executable when it is not on PATH.
set -eu
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
src=${KIT_ONEYAML_CSHARP_SRC:?KIT_ONEYAML_CSHARP_SRC must name an oneyaml-csharp checkout}
src=$(cd "$src" && pwd)
test -f "$src/src/OneYaml/OneYaml.csproj" || { echo "no src/OneYaml/OneYaml.csproj in KIT_ONEYAML_CSHARP_SRC" >&2; exit 1; }
dir=$(cd "$(dirname "$0")" && pwd)
art=${KIT_DOTNET_ARTIFACTS:-$dir/../../out/artifacts}
# The version the report shows: the checkout's commit when it is a git checkout.
rev=devel
if [ "$(git -C "$src" rev-parse --show-toplevel 2>/dev/null)" = "$src" ]; then
	rev=$(git -C "$src" rev-parse --short=12 HEAD 2>/dev/null || echo devel)
elif [ -n "${KIT_ONEYAML_CSHARP_REV:-}" ]; then
	# A source unpacked from an archive has no history; the launcher names its revision.
	rev=$KIT_ONEYAML_CSHARP_REV
fi
exec "${DOTNET:-dotnet}" build "$dir/OneYamlAdapter.csproj" -c Release --artifacts-path "$art" \
  --nologo -v quiet -clp:NoSummary -nodeReuse:false -p:UseSharedCompilation=false \
  -p:OneYamlSrc="$src" -p:OneYamlRevision="$rev"
