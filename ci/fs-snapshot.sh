#!/bin/bash
# File system snapshot and comparison, used to prove that installing and
# purging the package leaves the system as it was.
#
#   ci/fs-snapshot.sh snapshot OUTFILE    record the current state of /
#   ci/fs-snapshot.sh diff BEFORE AFTER   print differences, exit 1 if any
#
# A snapshot lists every path on the root file system (-xdev) with type,
# owner, group, mode and symlink target, plus a SHA-256 of every regular file
# below /etc (so added users, groups and edited configuration show up).
# Excluded are virtual and volatile trees and package manager state that
# legitimately changes with every apt run.
set -euo pipefail

ROOT=${FS_SNAPSHOT_ROOT:-/}
ROOT=${ROOT%/}

# Paths (relative to ROOT) that are not compared at all.
PRUNE=(
    /proc /sys /dev /run /tmp /var/tmp
    /var/cache /var/log /var/backups /var/lib/apt
    /var/lib/systemd/timers /var/lib/systemd/random-seed
    /var/lib/ucf/cache
    # Backups written by adduser/useradd/userdel (shadow-utils); the real
    # files are compared by content.
    /etc/passwd- /etc/group- /etc/shadow- /etc/gshadow- /etc/subuid- /etc/subgid-
)
# Files whose content is not hashed (pruned above, or legitimately changing).
NO_HASH_RE='^/etc/(passwd-|group-|shadow-|gshadow-|subuid-|subgid-|ld\.so\.cache|adjtime)$'

snapshot() {
    local out=$1
    local args=() p
    for p in "${PRUNE[@]}"; do
        args+=(-path "$ROOT$p" -o)
    done
    # dpkg state except the per-package info files (which purge must remove)
    args+=( \( -path "$ROOT/var/lib/dpkg/*" ! -path "$ROOT/var/lib/dpkg/info" ! -path "$ROOT/var/lib/dpkg/info/*" \) )
    {
        find "$ROOT/" -xdev \( "${args[@]}" \) -prune -o \
            -printf 'META\t%p\t%y\t%u:%g\t%m\t%l\n'
        find "$ROOT/etc" -xdev -type f -print0 |
            while IFS= read -r -d '' f; do
                rel=${f#"$ROOT"}
                if [[ $rel =~ $NO_HASH_RE ]]; then
                    continue
                fi
                printf 'HASH\t%s\t%s\n' "$f" "$(sha256sum < "$f" | cut -d' ' -f1)"
            done
    } | sed "s|\t$ROOT/|\t/|" | LC_ALL=C sort > "$out"
    echo "fs-snapshot: $(wc -l < "$out") entries -> $out" >&2
}

compare() {
    local before=$1 after=$2
    if diff -u "$before" "$after"; then
        echo "fs-snapshot: no differences" >&2
        return 0
    fi
    echo "fs-snapshot: the file system differs (- before, + after)" >&2
    return 1
}

case "${1:-}" in
    snapshot) [ $# -eq 2 ] || { echo "usage: $0 snapshot OUTFILE" >&2; exit 2; }
        snapshot "$2" ;;
    diff) [ $# -eq 3 ] || { echo "usage: $0 diff BEFORE AFTER" >&2; exit 2; }
        compare "$2" "$3" ;;
    *) echo "usage: $0 snapshot OUTFILE | diff BEFORE AFTER" >&2; exit 2 ;;
esac
