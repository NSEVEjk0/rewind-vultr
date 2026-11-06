#!/usr/bin/env bash
# Register the pinned runtime without replacing Docker or restarting containers.
set -euo pipefail
if [ "$(id -u)" -ne 0 ] || [ "$(uname -m)" != x86_64 ]; then
  echo 'Use root on a Linux x86_64 VM with an existing Docker daemon.' >&2
  exit 1
fi
rewind_project_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd -- "$rewind_project_dir"
if ! python3 -m rewind.runtime verify-bundle >/dev/null 2>&1; then
  rewind_stage_dir=$(mktemp -d)
  trap 'rm -rf "$rewind_stage_dir"' EXIT
  curl -fsSL --max-time 120 \
    https://github.com/google/gvisor/releases/download/release-20261102.0/gvisor-x86_64.tar.bz2 \
    -o "$rewind_stage_dir/gvisor.tar.bz2"
  python3 -m rewind.runtime_bundle --archive "$rewind_stage_dir/gvisor.tar.bz2" \
    --destination /opt/reprolab-runtime/release-20261102.0
fi
python3 -m rewind.runtime enable
rewind_image_ref='python:3.12-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487'
if ! /usr/bin/docker --host=unix:///var/run/docker.sock image inspect "$rewind_image_ref" >/dev/null 2>&1; then
  /usr/bin/docker --host=unix:///var/run/docker.sock pull "$rewind_image_ref"
fi
python3 -m rewind.runtime verify-bundle
