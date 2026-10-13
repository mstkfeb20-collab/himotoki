"""Prepare one clean folder per task and condition.

For every task: the source tree at the parent commit WITHOUT git history (so neither
condition can read the answer from `git log`), as a fresh one-commit repository, twice:
`<out>/<id>/A` (Claude Code alone) and `<out>/<id>/B` (with himotoki). For B a himotoki
workspace is indexed from its copy. The MCP configs and the prompt are written beside them.

  python prepare.py --tasks tasks.json --out ~/compare
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def sh(*a, **kw):
    return subprocess.run(a, check=True, **kw)


def snapshot(repo, rev, dest):
    os.makedirs(dest)
    arch = subprocess.Popen(["git", "-C", repo, "archive", rev], stdout=subprocess.PIPE)
    sh("tar", "-x", "-C", dest, stdin=arch.stdout)
    arch.wait()
    env = dict(os.environ, GIT_AUTHOR_NAME="snapshot", GIT_AUTHOR_EMAIL="s@example.com",
               GIT_COMMITTER_NAME="snapshot", GIT_COMMITTER_EMAIL="s@example.com")
    sh("git", "-C", dest, "init", "-q", "-b", "main")
    sh("git", "-C", dest, "add", "-A")
    sh("git", "-C", dest, "commit", "-qm", "snapshot", env=env)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", help="the original repository (overrides the path in the tasks file)")
    ap.add_argument("--himotoki-ai", action="store_true",
                    help="let himotoki use the AI judge (its API cost is then NOT in Claude Code's figures)")
    ap.add_argument("--himotoki", default="himotoki", help='himotoki command, e.g. "python -m himotoki.cli"')
    a = ap.parse_args()
    t = json.load(open(a.tasks, encoding="utf-8"))
    t["repo"] = os.path.abspath(os.path.expanduser(a.repo or t["repo"]))
    cmd = shlex.split(a.himotoki)
    cmd[0] = shutil.which(cmd[0]) or cmd[0]
    out = os.path.abspath(os.path.expanduser(a.out))
    os.makedirs(out, exist_ok=True)
    prompt = open(os.path.join(HERE, "prompt.txt"), encoding="utf-8").read()
    for task in t["tasks"]:
        base = os.path.join(out, task["id"])
        if os.path.exists(base):
            print(f"{task['id']}: exists, skipped (delete the folder to prepare again)")
            continue
        for cond in ("A", "B"):
            snapshot(t["repo"], task["parent"], os.path.join(base, cond))
        ws = os.path.join(base, "ws")
        print(f"{task['id']}: indexing for B …", flush=True)
        sh(*cmd, "index", "--repo", os.path.join(base, "B"), "--graph", "repo",
           env=dict(os.environ, HIMOTOKI_DATA_DIR=ws, HIMOTOKI_STORE="local"))
        json.dump({"mcpServers": {}}, open(os.path.join(base, "mcp-A.json"), "w"))
        json.dump({"mcpServers": {"himotoki": {"type": "stdio", "command": cmd[0],
                                               "args": cmd[1:] + ["mcp"],
                                               # an exported ANTHROPIC_API_KEY would switch himotoki to the AI
                                               # judge, whose cost Claude Code does not count: rule-based by default
                                               "env": dict({"HIMOTOKI_DATA_DIR": ws, "HIMOTOKI_STORE": "local"},
                                                           **({} if a.himotoki_ai else {"ANTHROPIC_API_KEY": ""}))}}},
                  open(os.path.join(base, "mcp-B.json"), "w"), indent=1)
        open(os.path.join(base, "prompt.txt"), "w", encoding="utf-8").write(prompt.replace("{requirement}", task["requirement"]))
    print(f"ready: {out}")


if __name__ == "__main__":
    sys.exit(main())
