#!/bin/bash
# CI stage 2: runs inside a Raspberry Pi OS armhf root file system under QEMU
# user emulation (docker --platform linux/arm/v6, QEMU_CPU=arm1176), i.e. the
# armv6 userland of a Pi Zero W. No systemd and no udevd run here, which is
# exactly the chroot/image-builder case the maintainer scripts must handle.
#
# Run with --tmpfs /tmp:exec. Expects the repository at /src (read-only is
# fine) and the package at
# /debs/footage-offload_*_all.deb.
#
# 1. install dependencies (Depends + Recommends) and pytest
# 2. run the test suite on armv6 Python
# 3. snapshot the file system
# 4. install the .deb, smoke-test it, reinstall, remove, purge
# 5. snapshot again: the two snapshots must be identical
set -euo pipefail

SRC=${SRC:-/src}
SNAP="$SRC/ci/fs-snapshot.sh"
WORK=$(mktemp -d /tmp/stage2.XXXXXX)
shopt -s nullglob
debs=(/debs/footage-offload_*_all.deb)
shopt -u nullglob
if [ "${#debs[@]}" -ne 1 ]; then
    echo "stage2: expected exactly one /debs/footage-offload_*_all.deb" >&2
    exit 1
fi
DEB=${debs[0]}

export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

step() { printf '\n=== %s ===\n' "$*"; }

step "Environment"
uname -m
python3 -c 'import platform, sys; print(platform.machine(), sys.version)'
grep -E '^(PRETTY_NAME|VERSION_CODENAME)=' /etc/os-release
dpkg-deb -I "$DEB"

step "Container preparation"
# The image preloads libarmmem-${PLATFORM}.so; harmless under QEMU, but the
# warnings drown the log.
if [ -s /etc/ld.so.preload ]; then
    : > /etc/ld.so.preload
fi
# Never start daemons (samba, NetworkManager, ...) inside the container.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 0755 /usr/sbin/policy-rc.d

step "Dependencies (Depends + Recommends) and pytest"
apt-get update -q
depends=$(dpkg-deb -f "$DEB" Depends)
recommends=$(dpkg-deb -f "$DEB" Recommends)
apt-get satisfy -y -q --no-install-recommends "$depends, $recommends, python3-pytest"

step "QEMU user emulation workaround for libgphoto2"
# 32-bit programs under QEMU user emulation on a 64-bit host get 64-bit
# directory cookies from ext4/overlayfs, so readdir() without large file
# support (libltdl inside libgphoto2) fails and libgphoto2 reports "No
# iolibs found". A real Pi is not affected. tmpfs has small cookies, so serve
# libgphoto2's driver directories from /tmp (docker run --tmpfs /tmp).
if [ "$(stat -f -c %T /tmp)" = tmpfs ]; then
    for spec in IOLIBS:libgphoto2_port CAMLIBS:libgphoto2; do
        var=${spec%%:*}
        for dir in /usr/lib/*/"${spec#*:}"/[0-9]*/; do
            [ -d "$dir" ] || continue
            mkdir -p "/tmp/gphoto2-$var"
            cp -a "$dir." "/tmp/gphoto2-$var/"
            export "$var=/tmp/gphoto2-$var"
            echo "$var=/tmp/gphoto2-$var (copy of $dir)"
            break
        done
    done
else
    echo "/tmp is not a tmpfs; libgphoto2 tests may fail under QEMU" >&2
fi

step "Test suite on $(uname -m) Python"
cp -a "$SRC" "$WORK/src"
(cd "$WORK/src" && python3 -m pytest -q -p no:cacheprovider)
rm -rf "$WORK/src"

step "Snapshot before installation"
"$SNAP" snapshot "$WORK/before"

step "Install"
apt-get install -y -q "$DEB"
dpkg -s footage-offload | grep -E '^(Status|Version):'
test "$(dpkg -s footage-offload | sed -n 's/^Status: //p')" = "install ok installed"

step "Checks after installation"
getent passwd footage-offload
id footage-offload
test "$(stat -c '%U:%G %a' /etc/footage-offload)" = "footage-offload:footage-offload 750"
test "$(stat -c '%U:%G %a' /etc/footage-offload/config.json)" = "footage-offload:footage-offload 600"
test "$(stat -c '%U:%G %a' /var/lib/footage-offload)" = "footage-offload:footage-offload 750"
test "$(stat -c '%U:%G %a' /run/footage-offload)" = "root:footage-offload 750"
python3 -c 'import json, sys; json.load(open(sys.argv[1]))' /etc/footage-offload/config.json
test -L /etc/systemd/system/multi-user.target.wants/footage-offload.service
test -L /etc/systemd/system/multi-user.target.wants/footage-offload-helper.service
test -f /usr/lib/udev/rules.d/85-footage-offload.rules
if dpkg -L footage-offload | grep -q '/__pycache__/'; then
    echo "byte code must not be shipped in the package" >&2
    exit 1
fi
(cd / && footage-offload version)
footage-offload version | grep -q "$(dpkg-query -W -f='${Version}' footage-offload | tr '~' '-')"

step "Import every module as the service user"
runuser -u footage-offload -- python3 -I -c '
import importlib, pkgutil, footage_offload
for m in pkgutil.walk_packages(footage_offload.__path__, "footage_offload."):
    if m.name.endswith("__main__"):
        continue
    importlib.import_module(m.name)
    print("ok", m.name)
'

step "Start the service as the service user and query the API"
runuser -u footage-offload -- python3 -I -m footage_offload serve > "$WORK/serve.log" 2>&1 &
serve_pid=$!
ok=0
for _ in $(seq 1 120); do
    if python3 -I -c '
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8080/api/v1/session", timeout=5) as r:
    s = json.load(r)
assert "csrf_token" in s and "version" in s, s
print(s["version"], "login_enabled=%s" % s["login_enabled"])
' 2>/dev/null; then
        ok=1
        break
    fi
    if ! kill -0 "$serve_pid" 2>/dev/null; then
        break
    fi
    sleep 2
done
kill "$serve_pid" 2>/dev/null || true
wait "$serve_pid" 2>/dev/null || true
if [ "$ok" != 1 ]; then
    echo "the service did not answer on port 8080; its output:" >&2
    cat "$WORK/serve.log" >&2
    exit 1
fi

step "Reinstall (postinst configure with a previous version)"
apt-get install -y -q --reinstall "$DEB"
test "$(stat -c '%U:%G %a' /etc/footage-offload/config.json)" = "footage-offload:footage-offload 600"

step "Remove: program gone, configuration, state and user kept"
apt-get remove -y -q footage-offload
test ! -e /usr/lib/python3/dist-packages/footage_offload
test ! -e /usr/bin/footage-offload
test -f /etc/footage-offload/config.json
test -d /var/lib/footage-offload
getent passwd footage-offload >/dev/null

step "Purge"
apt-get purge -y -q footage-offload
if getent passwd footage-offload || getent group footage-offload; then
    echo "purge left the system user or group behind" >&2
    exit 1
fi
test ! -e /run/footage-offload

step "Snapshot after purge and comparison"
"$SNAP" snapshot "$WORK/after"
"$SNAP" diff "$WORK/before" "$WORK/after"

echo
echo "stage 2 passed"
