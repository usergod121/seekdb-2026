#!/bin/sh
# Wrapper so the public-case runner can drive the packaged image instead of a local build:
#   MVDB_BIN=/tmp/mvdb_docker.sh python3 tests/run_public_cases.py
# The runner passes --data-dir <host path>; the container sees that directory as /data.
set -eu
image="${MVDB_IMAGE:-mvdb:check}"
dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --data-dir) dir="$2"; shift 2 ;;
    *) shift ;;
  esac
done
exec docker run -i --rm -v "$dir:/data" "$image" --data-dir /data
