#!/bin/sh
# Builds the adapter once. KIT_DOTNET_ARTIFACTS says where the output goes (default: the kit output
# directory, out/artifacts, never the adapter folder).
# DOTNET names the dotnet executable when it is not on PATH.
set -eu
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
dir=$(cd "$(dirname "$0")" && pwd)
art=${KIT_DOTNET_ARTIFACTS:-$dir/../../out/artifacts}
exec "${DOTNET:-dotnet}" build "$dir/SharpYamlAdapter.csproj" -c Release --artifacts-path "$art" \
  --nologo -v quiet -clp:NoSummary -nodeReuse:false -p:UseSharedCompilation=false
