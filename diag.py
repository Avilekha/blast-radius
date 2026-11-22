"""Compare an output.jsonl to sample/answers.json: misses, per-commit selection size.
Usage: python3 diag.py sample output.jsonl"""
import json, sys
from pathlib import Path

sample, out = Path(sys.argv[1]), sys.argv[2]
tests = json.loads((sample / "tests.json").read_text())["tests"]
ans = json.loads((sample / "answers.json").read_text())["commits"]
tot = miss = 0
for line in open(out):
    r = json.loads(line)
    sel = {n for w in r["workers"] for n in w}
    a = ans[r["commit"]]
    missed = [n for n in a["failing"] if n not in sel]
    secs = sum(tests[n]["seconds"] for n in sel)
    tot += secs
    miss += bool(missed)
    flag = "MISS " + ", ".join(tests[n]["id"] for n in missed) if missed else ""
    print(f'{r["commit"]:6} {a["kind"]:8} sel={len(sel):5} {secs:7.1f}s fail={len(a["failing"]):3} {flag}')
print(f"misses={miss} selected_seconds_total={tot:.1f}")
