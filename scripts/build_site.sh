#!/bin/sh
# Build the existing frontend for Sites without committing generated bundles.
set -eu
cd "$(dirname "$0")/.."
PATH="$PWD/.tools/node/bin:$PATH"
export PATH
npm --prefix frontend run build
mkdir -p dist/client
cp -R frontend/dist/. dist/client/
