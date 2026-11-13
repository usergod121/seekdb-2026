#!/usr/bin/env bash
# Runs every blind corpus against the golden binary and the candidate, so a difference between the two
# counts as a regression while the corpora's own spec-ambiguous expectations stay visible.
set -u
cd /mnt/e/mvdb || exit 1
GOLDEN=/mnt/e/pw/round2/golden/mvdb-golden-current
CANDIDATE=/mnt/e/mvdb/build/minivectordb
for bin in "$GOLDEN" "$CANDIDATE"; do
  echo "== $bin"
  fails=0
  for corpus in tests/blind/*/; do
    [ -f "$corpus/manifest.json" ] || continue
    out=$(MVDB_BIN="$bin" MVDB_CASES="/mnt/e/mvdb/$corpus" timeout 900 python3 -u tests/run_official_cases.py 2>&1 | tail -1)
    printf '%-28s %s\n' "$(basename "$corpus")" "$out"
    case "$out" in
      *FAIL*) fails=$((fails + 1)) ;;
    esac
  done
  echo "corpora with disagreements: $fails"
done
