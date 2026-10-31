#!/usr/bin/env bash
# Run packaging/test-pkg-in-docker.sh for every distro against dist/ and a local MSI.
#   packaging/test-all-distros.sh /path/to/SPIKE.msi-dir   (dir containing SPIKE.msi)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MSIDIR="${1:-/tmp/msi}"
for img in ubuntu:24.04 fedora:42 debian:13; do
  echo "######## $img"
  timeout 1100 docker run --rm --shm-size=1g -v "$ROOT/dist:/dist:ro" -v "$MSIDIR:/msi:ro" \
    -v "$ROOT/packaging/test-pkg-in-docker.sh:/t.sh:ro" "$img" bash /t.sh 2>&1 \
    | grep -E "^(== |/usr/bin|root|udev|Exec|Startup|spike3-linux|python deps|web app ready|ua:|blocks:|host |REMOVE|Traceback|--- app.log|FATAL)"
done
