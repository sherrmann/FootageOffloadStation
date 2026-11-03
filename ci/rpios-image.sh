#!/bin/bash
# Raspberry Pi OS image helpers for CI.
#
#   ci/rpios-image.sh resolve URL          print the final download URL
#                                          (the *_latest links redirect)
#   ci/rpios-image.sh fetch URL OUT.img    download, check SHA-256, decompress
#   ci/rpios-image.sh rootfs IMG OUT.tar.zst
#                                          pack the root partition (needs root)
#   ci/rpios-image.sh part-loop IMG N [ro]  attach partition N as a loop device
set -euo pipefail

resolve() {
    curl -fsSLI -o /dev/null -w '%{url_effective}' "$1"
}

fetch() {
    local url out xz expected actual
    url=$(resolve "$1")
    out=$2
    xz="$out.xz"
    echo "rpios-image: downloading $url" >&2
    curl -fsSL --retry 5 --retry-delay 10 -o "$xz" "$url"
    expected=$(curl -fsSL --retry 5 "$url.sha256" | cut -d' ' -f1)
    actual=$(sha256sum "$xz" | cut -d' ' -f1)
    if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        echo "rpios-image: checksum mismatch (expected '$expected', got '$actual')" >&2
        exit 1
    fi
    xz -dc -T0 "$xz" > "$out"
    rm -f "$xz"
}

# Attach partition N of an image as its own loop device (by offset, so it
# works without udev creating /dev/loopXpN). Prints the device.
part_loop() {
    local img=$1 n=$2 ro=${3:-} start sectors
    read -r start sectors < <(partx -g -o START,SECTORS -n "$n" "$img")
    losetup ${ro:+-r} --show -f -o $((start * 512)) --sizelimit $((sectors * 512)) "$img"
}

rootfs() {
    local img=$1 out=$2 loop mnt
    mnt=$(mktemp -d)
    loop=$(part_loop "$img" 2 ro)
    # shellcheck disable=SC2064 # expand now: the values are fixed
    trap "umount '$mnt' 2>/dev/null || true; losetup -d '$loop' || true; rmdir '$mnt'" EXIT
    mount -o ro "$loop" "$mnt"
    # Numeric owners: the tarball is imported into another system.
    tar -C "$mnt" --numeric-owner --xattrs -cf - . | zstd -q -T0 -3 -o "$out"
}

case "${1:-}" in
    resolve) resolve "$2" ;;
    fetch) fetch "$2" "$3" ;;
    rootfs) rootfs "$2" "$3" ;;
    part-loop) part_loop "$2" "$3" "${4:-}" ;;
    *) echo "usage: $0 resolve URL | fetch URL OUT.img | rootfs IMG OUT.tar.zst | part-loop IMG N [ro]" >&2; exit 2 ;;
esac
