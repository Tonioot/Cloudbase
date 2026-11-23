#!/usr/bin/env bash
# Checks on a real node that tenant mode shuts an app in: run it on a node (and on the primary) once tenant mode is on
# and the node has its list. It starts throwaway containers as Cloudbase would start a stranger's app (same networks,
# same limits and capabilities as core/tenant.py) and tries to get out. Nothing of Cloudbase's is touched.
#
#   tenant-check.sh [internet host:port to expect open, default 1.1.1.1:443]
#
# Exit status 0 when every check passes.
set -uo pipefail

if [[ $EUID -ne 0 ]] && ! docker info >/dev/null 2>&1; then
  exec sudo bash "$0" "$@"
fi

OPEN_TARGET="${1:-1.1.1.1:443}"
IMAGE="alpine:3.20"
NET="cloudbase-apps"                     # internet only (cbapps0)
PREFIX="cb-tenant-check-$$"
CAPS=(--cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SETUID --cap-add SETGID
      --cap-add NET_BIND_SERVICE --cap-add KILL)
RUNTIME=()
if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"runsc"'; then
  RUNTIME=(--runtime runsc)
fi
FAILED=0

pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILED=1; }
cleanup() { docker rm -f $(docker ps -aq --filter "name=${PREFIX}") >/dev/null 2>&1 || true; docker image rm -f "${PREFIX}-build" >/dev/null 2>&1 || true; }
trap cleanup EXIT

app() {   # app <command>: run it in a container shut in like a tenant's app
  docker run --rm --name "${PREFIX}-$RANDOM" --network "$NET" "${CAPS[@]}" "${RUNTIME[@]}" \
    --pids-limit 512 --memory 256m --cpus 0.5 --security-opt no-new-privileges:true \
    "$IMAGE" sh -c "$1" >/dev/null 2>&1
}
reaches() { app "nc -z -w 3 $1 $2"; }

expect_closed() { if reaches "$1" "$2"; then fail "$3 ($1:$2 is reachable)"; else pass "$3"; fi; }

docker network inspect "$NET" >/dev/null 2>&1 || { echo "No $NET network here yet: start an app on this machine first." >&2; exit 1; }
docker pull -q "$IMAGE" >/dev/null

BRIDGE_IP="$(docker network inspect "$NET" --format '{{(index .IPAM.Config 0).Gateway}}')"
LAN_GW="$(ip route show default 2>/dev/null | awk '{print $3; exit}')"
WG_IP="$(ip -4 addr show cbwg0 2>/dev/null | awk '/inet /{split($2,a,"/"); print a[1]; exit}')"
HUB_IP="$(echo "${WG_IP:-10.77.0.1}" | awk -F. '{print $1"."$2".0.1"}')"

echo "Network (an app with internet access only)"
expect_closed "$BRIDGE_IP" 22   "cannot reach SSH on this machine"
expect_closed "$BRIDGE_IP" 7823 "cannot reach the Cloudbase panel port on this machine"
expect_closed 169.254.169.254 80 "cannot reach the cloud metadata address"
[[ -n "$LAN_GW" ]] && expect_closed "$LAN_GW" 80 "cannot reach the router on the local network"
[[ -n "$LAN_GW" ]] && expect_closed "$LAN_GW" 22 "cannot reach SSH on the local network"
expect_closed "$HUB_IP" 7823 "cannot reach the primary over the node network"
expect_closed "$HUB_IP" 22   "cannot reach SSH on the primary over the node network"
expect_closed smtp.gmail.com 25 "cannot send mail directly (port 25)"
if reaches "${OPEN_TARGET%:*}" "${OPEN_TARGET#*:}"; then pass "can reach the internet ($OPEN_TARGET)"; else fail "cannot reach the internet ($OPEN_TARGET)"; fi

echo "Other apps"
docker run -d --name "${PREFIX}-victim" --network "$NET" "${CAPS[@]}" "${RUNTIME[@]}" "$IMAGE" sh -c "nc -lk -p 8080 -e cat" >/dev/null
sleep 1
VICTIM_IP="$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" "${PREFIX}-victim")"
expect_closed "$VICTIM_IP" 8080 "cannot reach another app on the same network"

echo "The container itself"
if app 'i=0; while [ $i -lt 700 ]; do sleep 30 & i=$((i+1)); done; wait'; then fail "a fork bomb is not stopped (700 processes started)"; else pass "a fork bomb stops at the process limit"; fi
# NET_RAW is bit 13 of the capability mask: forged packets on the bridge
if app 'c=$(awk "/CapEff/{print \$2}" /proc/self/status); [ $(( 0x$c & 0x2000 )) -eq 0 ]'; then pass "no raw sockets (NET_RAW is gone)"; else fail "has NET_RAW (raw sockets)"; fi
if app 'mknod /tmp/x c 1 1'; then fail "can create device files (MKNOD)"; else pass "cannot create device files"; fi
if app 'chroot / true'; then fail "can chroot (SYS_CHROOT)"; else pass "cannot chroot"; fi
[[ ${#RUNTIME[@]} -gt 0 ]] && pass "gVisor (runsc) sandbox is used" || echo "  note: gVisor is not installed here (scripts/setup-gvisor.sh)"

echo "A build"
BUILD_DIR="$(mktemp -d)"
cat >"$BUILD_DIR/Dockerfile" <<EOF
FROM $IMAGE
RUN ! nc -z -w 3 ${LAN_GW:-192.168.1.1} 80
RUN ! nc -z -w 3 169.254.169.254 80
RUN ! nc -z -w 3 $HUB_IP 7823
EOF
if docker build -q -t "${PREFIX}-build" "$BUILD_DIR" >/dev/null 2>&1; then pass "a build cannot reach the local network, the metadata address or the primary"; else fail "a build can reach a private address"; fi
rm -rf "$BUILD_DIR"

echo
if [[ $FAILED -eq 0 ]]; then echo "Every check passed."; else echo "Some checks failed: see above."; fi
exit $FAILED
