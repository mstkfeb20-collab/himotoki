"""Prisma schema extractor: models become tables (named by `@@map`), scalar fields
columns (named by `@map`), `///` comments their description. Each table also
provides the key `prisma:<model>`, which Prisma client calls in TypeScript
(`prisma.donor.findMany`) refer to; relation fields become references between tables."""
from __future__ import annotations

import re

from ..model import Kind, Rel
from ..pipeline.facts import FileFacts
from ..text import decode_source, words

NAME = "prisma"
VERSION = "1"
SCALARS = {"String", "Int", "BigInt", "Float", "Decimal", "Boolean", "DateTime", "Json", "Bytes", "Unsupported"}

BLOCK = re.compile(r"^(model|enum|view)\s+(\w+)\s*\{(.*?)^\}", re.S | re.M)


def extract(path: str, data: bytes, rules=None) -> FileFacts:
    f = FileFacts(path, NAME)
    src = decode_source(data)
    blocks = list(BLOCK.finditer(src))
    models = {m.group(2) for m in blocks if m.group(1) in ("model", "view")}
    enums = {m.group(2) for m in blocks if m.group(1) == "enum"}
    for m in blocks:
        kind, name, body = m.group(1), m.group(2), m.group(3)
        line0 = src.count("\n", 0, m.start()) + 1
        doc = _doc_above(src, m.start())
        if kind == "enum":
            vals = [v.split()[0] for v in body.splitlines() if v.strip() and not v.strip().startswith("//")]
            eid = f"prisma:{path}#{name}"
            f.node(eid, Kind.CLASS, name, line0, line0 + body.count("\n") + 1, f"{words(name)} {doc} {' '.join(vals)}",
                   keys=[f"tsexp:@prisma/client#{name}"], tskind="enum")
            continue
        mapped = re.search(r'@@map\(\s*"([^"]+)"', body)
        table = (mapped.group(1) if mapped else name).lower()
        tid = f"table:{table}"
        f.canonical(tid, Kind.TABLE, table, f"{words(name)} {words(table)} {doc}", keys=[tid, f"prisma:{name}"],
                    comment=doc or None, prisma_model=name)
        f.node(f"prisma:{path}#{name}", Kind.CLASS, name, line0, line0 + body.count("\n") + 1,
               f"{words(name)} {doc}", keys=[f"tsexp:@prisma/client#{name}"], tskind="prisma-model")
        f.edge(f"prisma:{path}#{name}", tid, Rel.MAPS_TO)
        pending = []
        for i, raw in enumerate(body.splitlines()):
            line = raw.strip()
            if line.startswith("///"):
                pending.append(line[3:].strip())
                continue
            if not line or line.startswith(("//", "@@")):
                pending = [] if not line.startswith("//") else pending
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            field, ftype = parts[0], re.sub(r"[?\[\]]", "", parts[1])
            comment = " ".join(pending + ([line.split("//", 1)[1].strip()] if "//" in line and "///" not in line else []))
            pending = []
            fl = line0 + i
            if ftype in models:
                f.ref(tid, f"prisma:{ftype}", Rel.REFERENCES, 0.8, fl, via="prisma-relation", optional=True)
                continue
            cm = re.search(r'@map\(\s*"([^"]+)"', line)
            col = (cm.group(1) if cm else field).lower()
            cid = f"col:{table}.{col}"
            f.canonical(cid, Kind.COLUMN, f"{table}.{col}", f"{words(table)} {words(field)} {comment}",
                        keys=[f"column:{table}.{col}"], comment=comment or None, type=ftype)
            f.edge(tid, cid, Rel.CONTAINS)
            fid = f"prisma:{path}#{name}.{field}"
            f.node(fid, Kind.FIELD, f"{name}.{field}", fl, fl, f"{words(field)} {words(name)} {comment}")
            f.edge(f"prisma:{path}#{name}", fid, Rel.CONTAINS)
            f.edge(fid, cid, Rel.MAPS_TO)
            if ftype in enums:
                f.ref(fid, f"tsexp:@prisma/client#{ftype}", Rel.REFERENCES, 0.9, fl, via="prisma-enum", optional=True)
    return f


def _doc_above(src: str, pos: int) -> str:
    out = []
    for line in reversed(src[:pos].splitlines()):
        s = line.strip()
        if s.startswith("///"):
            out.insert(0, s[3:].strip())
        elif s.startswith("//"):
            out.insert(0, s[2:].strip())
        else:
            break
    return " ".join(out)[:400]
