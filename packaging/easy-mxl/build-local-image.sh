#!/usr/bin/env bash
# Build the immutable local Core image through the deployment bundle tool.
# Usage: packaging/easy-mxl/build-local-image.sh <semver> [--apply]
# Without --apply this prints the exact build (dry run). The tag is
# <semver>.g<12-hex revision> from a clean checkout; it is never rebuilt.
set -euo pipefail

VERSION="${1:?usage: build-local-image.sh <semver> [--apply]}"
shift
BASE_IMAGE="${BASE_IMAGE:-mxl-lab:latest}"

exec python3 packaging/core-bundle/mxl_core_bundle.py build \
  --version "${VERSION}" \
  --base-image "${BASE_IMAGE}" \
  "$@"
