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
RESOLV=()
if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"runsc"'; then
  RUNTIME=(--runtime runsc)
  # Cloudbase gives a sandboxed app its own resolv.conf (gVisor can't reach Docker's name server): use the same one
  CB_USER="$(systemctl show -p User --value cloudbase 2>/dev/null || true)"
  CB_HOME="$(getent passwd "${CB_USER:-$(id -un)}" | cut -d: -f6)"
  if [[ -f "$CB_HOME/.cloudbase/sandbox-resolv.conf" ]]; then
    RESOLV=(-v "$CB_HOME/.cloudbase/sandbox-resolv.conf:/etc/resolv.conf:ro")
  fi
fi
FAILED=0

pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILED=1; }
cleanup() { docker rm -f $(docker ps -aq --filter "name=${PREFIX}") >/dev/null 2>&1 || true; docker image rm -f "${PREFIX}-build" >/dev/null 2>&1 || true; }
trap cleanup EXIT

app() {   # app <command>: run it in a container shut in like a tenant's app
  docker run --rm --name "${PREFIX}-$RANDOM" --network "$NET" "${CAPS[@]}" "${RUNTIME[@]}" "${RESOLV[@]}" \
    --pids-limit 512 --memory 256m --cpus 0.5 --security-opt no-new-privileges:true \
    "$IMAGE" sh -c "$1" >/dev/null 2>&1
}
reaches() { app "nc -z -w 3 $1 $2"; }

expect_closed() { if reaches "$1" "$2"; then fail "$3 ($1:$2 is reachable)"; else pass "$3"; fi; }

if [[ ! -f /var/lib/cloudbase-tenant ]]; then
  echo -e "\033[33mTenant mode is not on here\033[0m (no /var/lib/cloudbase-tenant): turn it on in config.yaml on the primary,"
  echo "restart Cloudbase and wait until this machine has its list. Until then mail and builds are not shut in."
  echo
  FAILED=1
fi
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
if app 'nslookup discord.com'; then pass "can look up public names (discord.com)"
elif [[ ${#RUNTIME[@]} -gt 0 && ${#RESOLV[@]} -eq 0 ]]; then fail "cannot look up public names: no sandbox resolv.conf yet (start an app here once after cloudbase update)"
else fail "cannot look up public names (discord.com)"; fi

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
build_reaches() {   # build_reaches <host> <port>: 0 reachable, 1 not, 2 the build failed for another reason
  local dir out
  dir="$(mktemp -d)"
  printf 'FROM %s\nRUN if nc -z -w 3 %s %s; then echo CB-REACHABLE; exit 1; fi\n' "$IMAGE" "$1" "$2" >"$dir/Dockerfile"
  out="$(docker build --no-cache --progress=plain -t "${PREFIX}-build" "$dir" 2>&1)"
  local rc=$?
  rm -rf "$dir"
  [[ $rc -eq 0 ]] && return 1
  grep -q "CB-REACHABLE" <<<"$out" && return 0
  echo "$out" | tail -5 | sed 's/^/      /'
  return 2
}
check_build() {
  build_reaches "$1" "$2"
  case $? in
    0) fail "a build can reach $3 ($1:$2)" ;;
    1) pass "a build cannot reach $3" ;;
    *) fail "the build for $3 failed for another reason (see above)" ;;
  esac
}
[[ -n "$LAN_GW" ]] && check_build "$LAN_GW" 80 "the local network"
check_build 169.254.169.254 80 "the cloud metadata address"
check_build "$HUB_IP" 7823 "the primary over the node network"
[[ -n "$BRIDGE_IP" ]] && check_build "$(ip -4 addr show docker0 2>/dev/null | awk '/inet /{split($2,a,"/"); print a[1]; exit}')" 22 "SSH on this machine"

echo
if [[ $FAILED -eq 0 ]]; then echo "Every check passed."; else echo "Some checks failed: see above."; fi
exit $FAILED
