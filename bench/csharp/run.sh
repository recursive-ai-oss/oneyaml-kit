#!/bin/sh
# Starts the built harness in this process, so the driver pins and measures it directly.
# $1 is the build directory the driver chose; the rest are the harness's own arguments.
build=${1:?usage: run.sh BUILD_DIR ARGS...}
shift
art=$build/dotnet
dll=OneYamlKit.Bench.CSharp
if [ "${SELFTEST:-}" = 1 ]; then art=$build/dotnet-selftest; dll=OneYamlKit.Bench.CSharp.Selftest; fi
exec "${DOTNET:-dotnet}" "$art/bin/Bench/release/$dll.dll" "$@"
