#!/bin/sh
# Starts the built adapter in this process, so the kit measures its memory directly.
dir=$(cd "$(dirname "$0")" && pwd)
art=${KIT_DOTNET_ARTIFACTS:-$dir/artifacts}
exec "${DOTNET:-dotnet}" "$art/bin/YamlDotNetAdapter/release/OneYamlKit.Adapter.YamlDotNet.dll"
