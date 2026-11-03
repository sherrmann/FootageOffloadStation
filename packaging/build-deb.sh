#!/bin/bash
# Build build/footage-offload_<version>_all.deb with plain dpkg-deb.
#
# Usage: packaging/build-deb.sh [vX.Y.Z]
#
# Without an argument the version comes from footage_offload/__init__.py.
# With a git tag argument (as in the release job) the tag must match that
# version, so the package and `footage-offload version` never disagree.
#
# Needs: dpkg-deb, python3, gzip, tar, find. Runs as a normal user
# (--root-owner-group makes every file root:root in the package).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
PKG=footage-offload
PKGDIR="$REPO/packaging"
OUT="$REPO/build"

# --- version -----------------------------------------------------------------
PY_VERSION=$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*["'"'"']\([^"'"'"']*\)["'"'"'].*/\1/p' \
    "$REPO/footage_offload/__init__.py")
if [ -z "$PY_VERSION" ]; then
    echo "build-deb: cannot read __version__ from footage_offload/__init__.py" >&2
    exit 1
fi
if [ "$#" -ge 1 ] && [ -n "$1" ]; then
    TAG_VERSION=${1#refs/tags/}
    TAG_VERSION=${TAG_VERSION#v}
    if [ "$TAG_VERSION" != "$PY_VERSION" ]; then
        echo "build-deb: tag $1 does not match __version__ $PY_VERSION" >&2
        exit 1
    fi
fi
# Debian sorts "~" before the release: 1.1.0-rc1 -> 1.1.0~rc1
VERSION=$(printf '%s' "$PY_VERSION" | tr '-' '~')
case "$VERSION" in
    [0-9]*) ;;
    *) echo "build-deb: invalid version '$VERSION'" >&2; exit 1 ;;
esac

# Reproducible time stamps: last commit, else now.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    SOURCE_DATE_EPOCH=$(git -C "$REPO" log -1 --format=%ct 2>/dev/null || date +%s)
fi
export SOURCE_DATE_EPOCH

umask 022
mkdir -p "$OUT"
STAGE=$(mktemp -d "$OUT/stage.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT INT TERM
ROOT="$STAGE/root"

# --- program files -------------------------------------------------------------
SITE="$ROOT/usr/lib/python3/dist-packages"
mkdir -p "$SITE"
# Python modules (no tests, no byte code) and everything below data/.
(
    cd "$REPO"
    find footage_offload \
        \( -name __pycache__ -o -name tests -o -name '.*' \) -prune -o \
        -type f \( -name '*.py' -o -path 'footage_offload/data/*' \) \
        ! -name '*.pyc' ! -name '*.pyo' ! -name '*~' ! -name '*.swp' \
        -print
) | sort > "$STAGE/files"
if ! grep -qx 'footage_offload/cli.py' "$STAGE/files"; then
    echo "build-deb: footage_offload/cli.py is missing" >&2
    exit 1
fi
while IFS= read -r f; do
    mkdir -p "$SITE/$(dirname "$f")"
    cp "$REPO/$f" "$SITE/$f"
done < "$STAGE/files"

mkdir -p "$ROOT/usr/bin"
cat > "$ROOT/usr/bin/$PKG" <<'EOF'
#!/bin/sh
# Footage Offload Station command line. -I: never import from the current
# directory or PYTHONPATH (this is also run with sudo).
exec /usr/bin/python3 -I -m footage_offload "$@"
EOF

# --- systemd, tmpfiles, udev, polkit (below /usr: Bookworm and merged-/usr Trixie)
mkdir -p "$ROOT/usr/lib/systemd/system" "$ROOT/usr/lib/tmpfiles.d" \
    "$ROOT/usr/lib/udev/rules.d" "$ROOT/usr/share/polkit-1/rules.d"
cp "$PKGDIR/systemd/$PKG.service" "$PKGDIR/systemd/$PKG-helper.service" \
    "$ROOT/usr/lib/systemd/system/"
cp "$PKGDIR/tmpfiles/$PKG.conf" "$ROOT/usr/lib/tmpfiles.d/"
cp "$PKGDIR"/udev/*.rules "$ROOT/usr/lib/udev/rules.d/"
cp "$PKGDIR"/polkit/*.rules "$ROOT/usr/share/polkit-1/rules.d/"

# --- default configuration (not a conffile: the service rewrites it) ---------
mkdir -p "$ROOT/usr/share/$PKG"
python3 -B -c '
import json, sys
sys.path.insert(0, sys.argv[1])
from footage_offload.config import DEFAULTS
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(DEFAULTS, f, indent=2, sort_keys=True)
    f.write("\n")
' "$REPO" "$ROOT/usr/share/$PKG/config.default.json"

# --- documentation -------------------------------------------------------------
DOC="$ROOT/usr/share/doc/$PKG"
mkdir -p "$DOC"
YEAR=$(sed -n 's/^Copyright (c) \([0-9][0-9-]*\).*/\1/p' "$REPO/LICENSE" 2>/dev/null | head -n 1)
[ -n "$YEAR" ] || YEAR=$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y)
{
    cat <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Footage Offload Station
Upstream-Contact: Samuel Herrmann <sherrmann@users.noreply.github.com>
Source: https://github.com/sherrmann/FootageOffloadStation

Files: *
Copyright: $YEAR Samuel Herrmann
License: Expat

License: Expat
EOF
    # Licence text as a DEP-5 paragraph: indent, empty lines become " ."
    if [ -f "$REPO/LICENSE" ]; then
        sed -n '/^Permission is hereby granted/,$p' "$REPO/LICENSE" |
            sed -e 's/^[[:space:]]*$/./' -e 's/^/ /'
    else
        echo "build-deb: LICENSE missing" >&2
        exit 1
    fi
} > "$DOC/copyright"
if [ -f "$REPO/CHANGELOG.md" ]; then
    gzip -9n < "$REPO/CHANGELOG.md" > "$DOC/changelog.gz"
fi
if [ -f "$REPO/README.md" ]; then
    gzip -9n < "$REPO/README.md" > "$DOC/README.md.gz"
fi

# --- control area --------------------------------------------------------------
mkdir -p "$ROOT/DEBIAN"
for s in postinst prerm postrm; do
    cp "$PKGDIR/debian/$s" "$ROOT/DEBIAN/$s"
    chmod 0755 "$ROOT/DEBIAN/$s"
done

# Normalise permissions (the working tree may have odd modes).
find "$ROOT" -type d -exec chmod 0755 {} +
find "$ROOT" -type f -exec chmod 0644 {} +
chmod 0755 "$ROOT/usr/bin/$PKG" "$ROOT"/DEBIAN/postinst "$ROOT"/DEBIAN/prerm "$ROOT"/DEBIAN/postrm

INSTALLED_SIZE=$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@INSTALLED_SIZE@/$INSTALLED_SIZE/" \
    "$PKGDIR/debian/control" > "$ROOT/DEBIAN/control"
(
    cd "$ROOT"
    find . -path ./DEBIAN -prune -o -type f -print | sed 's|^\./||' | LC_ALL=C sort |
        xargs md5sum
) > "$ROOT/DEBIAN/md5sums"
chmod 0644 "$ROOT/DEBIAN/control" "$ROOT/DEBIAN/md5sums"

find "$ROOT" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

DEB="$OUT/${PKG}_${VERSION}_all.deb"
rm -f "$DEB"
# xz: readable by every dpkg (Ubuntu's dpkg-deb would default to zstd).
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$DEB" >/dev/null
echo "$DEB"

if command -v lintian >/dev/null 2>&1; then
    echo "--- lintian (informational) ---" >&2
    lintian --tag-display-limit 0 "$DEB" >&2 || true
fi
