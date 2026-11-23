#!/usr/bin/env bash
# Installs gVisor (runsc) as a Docker runtime, for tenant mode's sandbox (core/tenant.py, tenant.runtime: runsc).
# Safe to run again. Run it on every node that runs apps or databases of people you don't know.
#
#   setup-gvisor.sh
#
# With gVisor a container talks to a kernel that runs in user space instead of to this machine's kernel, so a bug in
# the kernel is no longer a way out of the container. It costs some speed (system calls, file access).
#
# Docker is restarted at the end to pick up the runtime; containers with a restart policy come back by themselves.
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

echo "Downloading gVisor for ${ARCH}"
for file in runsc runsc.sha512 containerd-shim-runsc-v1 containerd-shim-runsc-v1.sha512; do
  curl -fsSLO "${URL}/${file}"
done
sha512sum -c runsc.sha512 -c containerd-shim-runsc-v1.sha512
install -o root -g root -m 755 runsc containerd-shim-runsc-v1 /usr/local/bin/

# Adds {"runtimes": {"runsc": ...}} to /etc/docker/daemon.json, keeping what is there
/usr/local/bin/runsc install

echo "Restarting Docker to load the runtime"
systemctl restart docker

if docker info --format '{{json .Runtimes}}' | grep -q '"runsc"'; then
  echo "gVisor is ready. Set in config.yaml on the primary:  tenant: { enabled: true, runtime: runsc }"
else
  echo "Docker does not list the runsc runtime; check /etc/docker/daemon.json." >&2
  exit 1
fi
