#!/usr/bin/env bash
# Build Tailwind CSS bundle. Run from repo root.
#
# First-time setup downloads the standalone Tailwind binary into ./bin/.
# CI and Docker use the same script — keeps build reproducible without Node.
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="${TAILWIND_VERSION:-3.4.13}"
BIN="./bin/tailwindcss"

if [[ ! -x "$BIN" ]]; then
  mkdir -p ./bin
  PLATFORM="linux-x64"
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) PLATFORM="macos-arm64" ;;
    Darwin-x86_64) PLATFORM="macos-x64" ;;
    Linux-aarch64) PLATFORM="linux-arm64" ;;
    Linux-x86_64) PLATFORM="linux-x64" ;;
  esac
  echo "Downloading tailwindcss v${VERSION} ${PLATFORM}..."
  curl -sSL -o "$BIN" "https://github.com/tailwindlabs/tailwindcss/releases/download/v${VERSION}/tailwindcss-${PLATFORM}"
  chmod +x "$BIN"
fi

"$BIN" \
  -c tailwind/tailwind.config.js \
  -i tailwind/input.css \
  -o app/static/app.css \
  --minify

echo "Built app/static/app.css ($(wc -c < app/static/app.css) bytes)"
