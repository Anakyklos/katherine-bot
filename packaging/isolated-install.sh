#!/bin/bash
# Katherine .deb isolated install harness (#338).
#
# Purpose: install and run the REAL .deb with the REAL dpkg, without
# root on the host and without touching the host's dpkg database or
# filesystem. This is a *mount-namespace pivot root* built with only
# unshare(1), mount(8) and tmpfs — the same primitives container
# runtimes use; no Docker requirement.
#
# What is isolated:
#   - dpkg database: fresh status db on tmpfs seeded from the host's
#     real dpkg status (so Depends resolution sees the system state);
#   - the install target: /install-root on the tmpfs root;
#   - $HOME: a tmpfs home, so XDG data paths are created fresh and no
#     real user data is ever touched;
#   - host /usr etc. are rbinds, effectively read-only (namespace-root
#     maps to the unprivileged user for root-owned host files).
#
# What is NOT faked: dpkg itself, the .deb, the installed tree, the
# app's Python, WebKitGTK, the X server (Xvfb), the app process.
#
# Usage:
#   packaging/isolated-install.sh <deb> [command...]
#
# The command (and any args) runs inside the isolated environment
# after the .deb is unpacked+configured at /install-root. When no
# command is given, an interactive shell runs.
#
# Extra environment understood inside:
#   KAT_DISPLAY - passed as DISPLAY (e.g. :99 from a host Xvfb).
#   KAT_INSTALL_ARGS - extra dpkg --unpack args (rarely used).

set -euo pipefail

DEB_PATH="${1:?usage: isolated-install.sh <deb> [command...]}"
shift || true
DEB_PATH="$(readlink -f "$DEB_PATH")"
[ -f "$DEB_PATH" ] || { echo "no such .deb: $DEB_PATH" >&2; exit 2; }
DEB_FILE="$(basename "$DEB_PATH")"
DEB_DIR="$(dirname "$DEB_PATH")"

WORK="$(mktemp -d /tmp/katherine-isolated-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# Pass the inner script and the command via env: no quoting hell.
export KAT_WORK="$WORK"
export KAT_DEB_FILE="$DEB_FILE"
export KAT_DEB_DIR="$DEB_DIR"
export KAT_DISPLAY="${KAT_DISPLAY:-}"
export KAT_CMD="${*:-bash}"

exec unshare --user --map-root-user --mount --pid --fork \
  env KAT_WORK="$KAT_WORK" KAT_DEB_FILE="$KAT_DEB_FILE" KAT_DEB_DIR="$KAT_DEB_DIR" \
      KAT_DISPLAY="$KAT_DISPLAY" KAT_CMD="$KAT_CMD" TMPDIR=/tmp \
  bash --noprofile --norc -euo pipefail <<'INNER'
MB=/usr/bin/mount
NEW="$KAT_WORK/newroot"
mkdir -p "$NEW/oldroot"
$MB -t tmpfs tmpfs "$NEW"

for d in oldroot dev etc var opt home tmp root proc sys run srv mnt media install-root; do
  mkdir -p "$NEW/$d"
done
mkdir -p "$NEW/usr"
$MB --rbind /usr "$NEW/usr"
$MB --rbind /etc "$NEW/etc"
$MB --rbind /var "$NEW/var"
$MB --rbind /opt "$NEW/opt"
$MB --rbind /dev "$NEW/dev"
mkdir -p "$NEW/home/user"
ln -s usr/bin "$NEW/bin"
ln -s usr/lib "$NEW/lib"
ln -s usr/lib64 "$NEW/lib64"
ln -s usr/sbin "$NEW/sbin"

# Seed the isolated dpkg db from the host's REAL status BEFORE pivot:
# after pivot_root, /oldroot/var is the rbind (accessible), but copying
# up-front keeps the logic in one place.
mkdir -p "$NEW/dpkg-db/info" "$NEW/dpkg-db/triggers" "$NEW/dpkg-db/updates"
cat /var/lib/dpkg/status > "$NEW/dpkg-db/status"
cat /var/lib/dpkg/available > "$NEW/dpkg-db/available" 2>/dev/null || : > "$NEW/dpkg-db/available"
echo amd64 > "$NEW/dpkg-db/arch"

# Make the .deb visible inside.
mkdir -p "$NEW/debs"
$MB --bind "$KAT_DEB_DIR" "$NEW/debs"

cd "$NEW"
$MB --make-rprivate /
pivot_root . oldroot
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export TMPDIR=/tmp

# The dpkg db seed was written pre-pivot; it now lives at /dpkg-db
# (part of the new root tmpfs). No host state is reachable for writes.

# Isolated HOME: XDG data lands here; the real home is unreachable.
export HOME=/home/user
mkdir -p "$HOME"
chmod 700 "$HOME"

# Optional host X display (Xvfb) for GUI runs.
if [ -n "$KAT_DISPLAY" ]; then
  export DISPLAY="$KAT_DISPLAY"
fi

# Never leak real env keys into the isolated run.
unset GROQ_API_KEY GROQ_API_KEY_2 SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY 2>/dev/null || true

# Install the .deb into the isolated root.
mkdir -p /install-root
dpkg --root=/install-root --admindir=/dpkg-db --force-not-root --log=/dpkg-db/dpkg.log \
    --force-depends --unpack "/debs/$KAT_DEB_FILE"
dpkg --root=/install-root --admindir=/dpkg-db --force-not-root --log=/dpkg-db/dpkg.log \
    --force-depends --configure katherine-desktop

echo "=== ISOLATED ENV READY ==="
dpkg-query --root=/install-root --admindir=/dpkg-db -W katherine-desktop

# Run the requested command (or an interactive shell).
eval "$KAT_CMD"
INNER
