#!/usr/bin/env bash
# Installs gVisor (runsc) as a Docker runtime, for tenant mode's sandbox (core/tenant.py, tenant.runtime: runsc).
# Safe to run again. Run it on every node that runs apps or databases of people you don't know.
#
#   setup-gvisor.sh
#
# With gVisor a container talks to a kernel that runs in user space instead of to this machine's kernel, so a bug in
# the kernel is no longer a way out of the container. It costs some speed (system calls, file access).
#
# Docker is reloaded at the end to pick up the runtime (restarted only when a reload does not work).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

case "$(uname -m)" in
  x86_64)        ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  *) echo "gVisor runs on x86_64 and 64-bit ARM only (this is $(uname -m))." >&2; exit 1 ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed." >&2
  exit 1
fi

URL="https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"

# gVisor ships as one archive: runsc, containerd-shim-runsc-v1 and the gvisor-bin/ directory that must sit next to
# runsc (runsc install no longer downloads what is missing). zstd when it is here, else bzip2.
if command -v zstd >/dev/null 2>&1 && tar --help 2>/dev/null | grep -q -- '--zstd'; then
  ARCHIVE="gvisor.tar.zstd"; UNPACK=(--zstd -xf)
else
  ARCHIVE="gvisor.tar.bz2"; UNPACK=(-xjf)
fi

echo "Downloading gVisor for ${ARCH}"
curl -fsSLO "${URL}/${ARCHIVE}"
curl -fsSLO "${URL}/${ARCHIVE}.sha512"
sha512sum -c "${ARCHIVE}.sha512"
tar "${UNPACK[@]}" "${ARCHIVE}" -C /usr/local/bin
chown -R root:root /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1
[[ -d /usr/local/bin/gvisor-bin ]] && chown -R root:root /usr/local/bin/gvisor-bin

# Adds {"runtimes": {"runsc": ...}} to /etc/docker/daemon.json, keeping what is there
/usr/local/bin/runsc install

echo "Reloading Docker to load the runtime"
systemctl reload docker || systemctl restart docker

if docker info --format '{{json .Runtimes}}' | grep -q '"runsc"'; then
  echo "gVisor is ready. Set in config.yaml on the primary:  tenant: { enabled: true, runtime: runsc }"
else
  echo "Docker does not list the runsc runtime; check /etc/docker/daemon.json." >&2
  exit 1
fi
