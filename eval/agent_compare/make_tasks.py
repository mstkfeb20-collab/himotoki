"""Pick comparison tasks from a history evaluation result.

Tasks are spread over the range of change sizes (small fixes to cross-layer changes)
with a fixed seed, so nobody hand-picks the commits himotoki happens to do well on.

  python make_tasks.py --repo ~/work/fineract --results ../fineract_history_results.json \
      --section after --n 6 --out tasks.json
"""
import argparse
import json
import random
import re
import subprocess

JAVA_MAIN = r"/src/main/.*\.(java|xml|jsp|html|properties|sql|groovy|ftl)$"
TS_MAIN = r"^(?!.*(\.test\.|\.spec\.|__tests__/|(^|/)e2e/|(^|/)tests?/|\.d\.ts$)).*\.(tsx?|prisma|sql)$"


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--section", default="after", help="dotted path to the object holding `rows`")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--code", default="java", help="java, ts, or a regex for the files that count")
    ap.add_argument("--out", default="tasks.json")
    ap.add_argument("--exclude", default=r"review comments|CodeRabbit|^address ",
                    help="subjects that are not requirements (left out before picking)")
    a = ap.parse_args()
    d = json.load(open(a.results, encoding="utf-8"))
    for k in a.section.split("."):
        d = d[k]
    rows = sorted((r for r in d["rows"] if not re.search(a.exclude, r["message"], re.I)),
                  key=lambda r: (r["changed"], r["sha"]))
    rng = random.Random(a.seed)
    # one commit from each of n equal slices of the size-ordered list
    picks = []
    for i in range(a.n):
        lo, hi = len(rows) * i // a.n, max(len(rows) * (i + 1) // a.n, len(rows) * i // a.n + 1)
        picks.append(rows[rng.randrange(lo, hi)])
    code = re.compile({"java": JAVA_MAIN, "ts": TS_MAIN}.get(a.code, a.code))
    tasks = []
    for r in picks:
        sha = git(a.repo, "rev-parse", r["sha"])
        parent = git(a.repo, "rev-parse", f"{sha}^")
        changed = [p for p in git(a.repo, "diff-tree", "--no-commit-id", "--name-status", "-r", sha).splitlines()
                   if p[:1] in "MD" and code.search(p.split("\t")[-1])]
        tasks.append({"id": sha[:8], "sha": sha, "parent": parent, "requirement": r["message"],
                      "truth": [p.split("\t")[-1] for p in changed]})
    json.dump({"repo": a.repo, "tasks": tasks}, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for t in tasks:
        print(f"{t['id']}  {len(t['truth']):2} files  {t['requirement'][:70]}")


if __name__ == "__main__":
    main()
