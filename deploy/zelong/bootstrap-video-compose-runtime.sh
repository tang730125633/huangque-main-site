#!/usr/bin/env bash
set -euo pipefail

NODE_VERSION="v22.14.0"
NODE_ARCHIVE="node-${NODE_VERSION}-linux-x64.tar.xz"
NODE_SHA256="69b09dba5c8dcb05c4e4273a4340db1005abeafe3927efda2bc5b249e80437ec"
NODE_URL="https://nodejs.org/dist/${NODE_VERSION}/${NODE_ARCHIVE}"
RUNTIME_ROOT="/home/ubuntu/.local"
NODE_ROOT="${RUNTIME_ROOT}/hq-node"
CACHE_ROOT="/home/ubuntu/.cache/hyperframes"
TMP="$(mktemp -d /tmp/hq-video-compose-runtime.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

if [[ "$(id -un)" != "ubuntu" ]]; then
  echo "must run as ubuntu" >&2
  exit 2
fi

mkdir -p "$RUNTIME_ROOT" "$CACHE_ROOT"
if [[ ! -x "$NODE_ROOT/bin/node" ]] || [[ "$($NODE_ROOT/bin/node --version)" != "$NODE_VERSION" ]]; then
  curl -fsSL --retry 4 --retry-delay 2 "$NODE_URL" -o "$TMP/$NODE_ARCHIVE"
  printf '%s  %s\n' "$NODE_SHA256" "$TMP/$NODE_ARCHIVE" | sha256sum -c -
  rm -rf "$TMP/node" && mkdir -p "$TMP/node"
  tar -xJf "$TMP/$NODE_ARCHIVE" -C "$TMP/node" --strip-components=1
  rm -rf "$NODE_ROOT.next"
  mv "$TMP/node" "$NODE_ROOT.next"
  rm -rf "$NODE_ROOT"
  mv "$NODE_ROOT.next" "$NODE_ROOT"
fi

export PATH="$NODE_ROOT/bin:$PATH"
export HYPERFRAMES_SKIP_SKILLS=1
export PRODUCER_LOW_MEMORY_MODE=1
export ONNXRUNTIME_NODE_INSTALL_CUDA=skip
if [[ -z "${HYPERFRAMES_BROWSER_PATH:-}" ]] && [[ -x /usr/bin/chromium-browser ]]; then
  export HYPERFRAMES_BROWSER_PATH=/usr/bin/chromium-browser
fi
node --version
npm --version
npx --yes hyperframes@0.7.90 browser ensure
npx --yes hyperframes@0.7.90 --version
printf 'video-compose runtime ready: node=%s hyperframes=0.7.90\n' "$(node --version)"
