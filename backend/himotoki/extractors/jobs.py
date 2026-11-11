"""Job scheduler definitions: JP1/AJS3 unit definitions (ajsprint output) and crontab.

JP1 jobnets become JobNet nodes, jobs become Job nodes; `sc=` (the script or
command a job runs) becomes a reference to the script file, and arrows `ar=(f=,t=)`
become PRECEDES edges between jobs. crontab lines become CronEntry nodes that
run their command's script.
"""
from __future__ import annotations

import re

from ..model import Kind, Rel
from ..pipeline.facts import FileFacts
from ..text import decode_source, words

NAME = "jobs"
VERSION = "3"

JOBNET_TYPES = {"n", "rn", "rm", "rr", "mn", "nc"}
JOB_TYPES = {"j", "rj", "pj", "rp", "qj", "rq", "evwj", "flwj", "mlwj", "mswj", "lfwj", "ntwj", "tmwj", "cmsj",
             "pwlj", "pwrj", "jdj", "ljdj", "orj", "rorj", "fxj"}


def is_jp1(path: str, head: str) -> bool:
    return bool(re.search(r"^\s*unit\s*=", head, re.M)) and "{" in head


def is_crontab(path: str, head: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    if "cron" not in path.lower() and not base.startswith("crontab"):
        return False
    return bool(re.search(r"^\s*([\d*/,\-]+\s+){4}[\d*/,\-a-zA-Z]+\s+\S", head, re.M) or
                re.search(r"^\s*@(daily|hourly|weekly|monthly|reboot)\s", head, re.M))


def _parse_units(src: str):
    """-> list of (name, attrs dict[list], children, line)"""
    pos, n = 0, len(src)
    tok_rx = re.compile(r"unit\s*=\s*([^;]*);|\{|\}|(\w+)\s*=\s*((?:\"(?:[^\"\\]|\\.)*\"|\([^)]*\)|[^;])*);", re.S)

    def line_at(i):
        return src.count("\n", 0, i) + 1

    def parse_block(i):
        units, attrs = [], {}
        pending = None
        while i < n:
            m = tok_rx.search(src, i)
            if not m:
                return units, attrs, n
            i = m.end()
            t = m.group(0)
            if t.startswith("unit"):
                pending = (m.group(1).split(",")[0].strip(), line_at(m.start()))
            elif t == "{":
                cu, ca, i = parse_block(i)
                if pending:
                    units.append({"name": pending[0], "line": pending[1], "attrs": ca, "children": cu})
                    pending = None
            elif t == "}":
                return units, attrs, i
            elif m.group(2):
                attrs.setdefault(m.group(2), []).append(m.group(3).strip().strip('"'))
        return units, attrs, i

    units, _, _ = parse_block(0)
    return units


def extract_jp1(path: str, src: str, f: FileFacts):
    def walk(u, parent_path, parent_id):
        full = (parent_path.rstrip("/") + "/" + u["name"].lstrip("/")) if parent_path else u["name"]
        if not full.startswith("/"):
            full = "/" + full
        ty = (u["attrs"].get("ty") or ["n" if u["children"] else "j"])[0]
        kind = Kind.JOBNET if ty in JOBNET_TYPES or u["children"] else Kind.JOB
        nid = f"job:{full}"
        sc = (u["attrs"].get("sc") or [""])[0]
        prm = (u["attrs"].get("prm") or [""])[0]
        cm = (u["attrs"].get("cm") or [""])[0]
        f.node(nid, kind, full, u["line"], u["line"], " ".join([words(u["name"]), cm, sc, prm]),
               keys=[nid, f"job:{u['name']}"], unit_type=ty, script=sc or None, params=prm or None, comment=cm or None)
        if parent_id:
            f.edge(parent_id, nid, Rel.CONTAINS, 1.0, "parser")
        # file-watch jobs (JP1 flwj / flwtj): the watched file starts the following jobs
        for flwf in u["attrs"].get("flwf", []):
            watched = flwf.strip().strip('"')
            if watched:
                f.ref(nid, f"datafile:{watched}", Rel.READS, 0.95, u["line"], via="jp1-file-watch", watch="true")
        if sc:
            script = sc.split()[0]
            f.ref(nid, f"file:{script}", Rel.RUNS, 0.95, u["line"], via="jp1-sc")
            for tok in re.findall(r"((?:[a-z_]\w*\.)+[A-Z]\w+)", sc + " " + prm):
                f.ref(nid, f"class:{tok}", Rel.RUNS, 0.85, u["line"], via="jp1-sc", optional=True)
        # data files handed to the job as arguments / transfer definitions (prm=, fxj / ftp jobs)
        args = " ".join([sc, prm] + u["attrs"].get("env", []) + u["attrs"].get("ts1", []) + u["attrs"].get("td1", []))
        for tok in dict.fromkeys(re.findall(r"([\w./${}%\-]+\.(?:dat|csv|tsv|txt|fix|gz|zip|lst|prn|seq))\b", args)):
            f.ref(nid, f"datafile:{tok}", Rel.REFERENCES, 0.7, u["line"], via="jp1-args", optional=True)
        for ar in u["attrs"].get("ar", []):
            m = re.search(r"f\s*=\s*([^,\s)]+)\s*,\s*t\s*=\s*([^,\s)]+)", ar)
            if m:
                a = f"job:{full.rstrip('/')}/{m.group(1)}"
                b = f"job:{full.rstrip('/')}/{m.group(2)}"
                f.edge(a, b, Rel.PRECEDES, 1.0, "parser", condition=ar)
        for c in u["children"]:
            walk(c, full, nid)

    for u in _parse_units(src):
        walk(u, "", None)


def extract_cron(path: str, src: str, f: FileFacts):
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#") or re.match(r"^\w+=", s):
            continue
        m = re.match(r"^((?:[\d*/,\-]+\s+){4}[\d*/,\-a-zA-Z]+|@\w+)\s+(?:(\w+)\s+(?=/))?(.+)$", s)
        if not m:
            continue
        sched, user, cmd = m.group(1), m.group(2), m.group(3)
        nid = f"cron:{path}:{i}"
        f.node(nid, Kind.CRON, f"cron {sched} {cmd[:40]}", i, i, f"{sched} {cmd}", keys=[nid], schedule=sched,
               command=cmd, user=user)
        for tok in re.findall(r"([\w./${}\-]+\.(?:sh|ksh|bash|py|pl))\b", cmd):
            f.ref(nid, f"file:{tok}", Rel.RUNS, 0.95, i, via="crontab")
        for tok in re.findall(r"((?:[a-z_]\w*\.)+[A-Z]\w+)", cmd):
            f.ref(nid, f"class:{tok}", Rel.RUNS, 0.85, i, via="crontab", optional=True)


def extract(path: str, data: bytes, rules) -> FileFacts:
    f = FileFacts(path, NAME)
    src = decode_source(data)
    head = src[:4000]
    f.node(f"file:{path}", Kind.CONFIG, path.rsplit("/", 1)[-1], 1, src.count("\n") + 1, words(path),
           keys=[f"file:{path}"], scheduler="jp1" if is_jp1(path, head) else "cron")
    if is_jp1(path, head):
        extract_jp1(path, src, f)
    else:
        extract_cron(path, src, f)
    return f
