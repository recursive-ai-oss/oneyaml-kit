#!/bin/sh
# Builds the Go adapter in the current directory. KIT_GO_BIN says where the binary goes,
# so build output can stay out of the checkout; the default is an ignored directory here.
set -eu
name=$(basename "$(pwd)")
bin=${KIT_GO_BIN:-$(cd "$(dirname "$0")" && pwd)/.bin}
mkdir -p "$bin"
exec go build -trimpath -o "$bin/$name" .
