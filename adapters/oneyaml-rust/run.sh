#!/bin/sh
# Starts the built adapter. CARGO_TARGET_DIR says where the build put it.
dir=$(cd "$(dirname "$0")" && pwd)
exec "${CARGO_TARGET_DIR:-$dir/target}/release/oneyaml-kit-adapter"
