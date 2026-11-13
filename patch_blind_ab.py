p = "tests/blind_ab.sh"
s = open(p, encoding="utf-8").read()
s = s.replace('GOLDEN=/mnt/e/pw/round2/golden/mvdb-golden-current',
              'GOLDEN=${MVDB_GOLDEN:-/mnt/e/pw/round2/golden/mvdb-gate-proven}')
open(p, "w", encoding="utf-8", newline="\n").write(s)
print("blind_ab now defaults to the gate-proven binary")
