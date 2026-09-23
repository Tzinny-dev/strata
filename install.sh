#!/usr/bin/env bash
# strata install — detects OS/arch, downloads standalone binary from GH Releases
# Usage: curl -fsSL https://raw.githubusercontent.com/Tzinny-dev/strata/main/install.sh | bash
#        curl -fsSL .../install.sh | bash -s -- --version 0.1.1
#        ./install.sh --to /usr/local/bin
set -euo pipefail

REPO="Tzinny-dev/strata"
VERSION="${VERSION:-}"
TO="${TO:-/usr/local/bin}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2;;
    --to) TO="$2"; shift 2;;
    *) echo "unknown arg $1" >&2; exit 1;;
  esac
done

if [[ -z "$VERSION" ]]; then
  VERSION="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | grep -o '"tag_name": "v[^"]*"' | cut -d'"' -f4 | sed 's/^v//')"
  if [[ -z "$VERSION" ]]; then echo "could not detect latest version" >&2; exit 1; fi
fi

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Linux)  ASSET="strata-linux-amd64" ;;
  Darwin) ASSET="strata-macos-amd64" ;;
  MINGW*|MSYS*|CYGWIN*) ASSET="strata-windows-amd64.exe" ;;
  *) echo "unsupported OS $OS" >&2; exit 1;;
esac
# Normalize arch: binary.yml builds amd64 only today; arm64 will run via rosetta/emulation
# Future: strata-macos-arm64, strata-linux-arm64 when matrix adds arm

URL="https://github.com/$REPO/releases/download/v$VERSION/$ASSET"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "downloading $URL ..."
curl -fsSL "$URL" -o "$TMP/strata"
chmod +x "$TMP/strata"

# Verify basic smoke before install
"$TMP/strata" --help >/dev/null
"$TMP/strata" build --help >/dev/null 2>&1 || true

mkdir -p "$TO"
if [[ "$ASSET" == *.exe ]]; then
  mv "$TMP/strata" "$TO/strata.exe"
  echo "installed $TO/strata.exe (v$VERSION)"
else
  mv "$TMP/strata" "$TO/strata"
  echo "installed $TO/strata (v$VERSION)"
fi

echo "verify: $TO/strata --help"
