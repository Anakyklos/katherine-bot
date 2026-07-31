#!/usr/bin/env bash
# Hide every migration AFTER the given target version, run a command, and
# always restore the migrations (including on failure).
#
# Usage: hide-migrations-after.sh <target-version> <command...>
#
# Legacy-scenario tests simulate a pre-migration database by hiding migration
# files. Later migrations hard-depend on the hidden ones, so they must be
# hidden too. The mechanism compares zero-padded fixed-width version prefixes
# lexicographically (equivalent to numeric ordering), so any future migration
# is auto-hidden with no manual list to maintain.
set -euo pipefail

target="$1"
shift

hidden=()
restore_migrations() {
  for f in "${hidden[@]:-}"; do
    if [[ -f "$f.legacy-test-hidden" ]]; then
      mv "$f.legacy-test-hidden" "$f"
    fi
  done
}
trap restore_migrations EXIT

for migration in supabase/migrations/*.sql; do
  base="$(basename "$migration")"
  version="${base%%_*}"
  if [[ "$version" > "$target" && -f "$migration" ]]; then
    mv "$migration" "$migration.legacy-test-hidden"
    hidden+=("$migration")
  fi
done

"$@"
