#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-0.2.0-candidate}"
REVISION="${2:-$(git rev-parse HEAD)}"
BASE_IMAGE="${BASE_IMAGE:-mxl-lab:latest}"
IMAGE="mxl-switcher-core:${VERSION}"

docker build \
  --file packaging/easy-mxl/Dockerfile.runtime \
  --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "VERSION=${VERSION}" \
  --build-arg "REVISION=${REVISION}" \
  --tag "${IMAGE}" \
  .

printf '%s\n' "${IMAGE}"
