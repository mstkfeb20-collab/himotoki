"""Record layouts of exchanged files: fixed-length layout lists, CSV item definitions,
COBOL copybooks.

Which files are layouts is declared in a rule pack (`layout: files:`). Each field becomes
a LayoutField node with its position and length; the field MAPS_TO the column of the
same physical name, and the layout DEFINES the data files it names (in its text or,
failing that, by the stem of its own file name). The lengths let the viewer show how
the digits of a value travel from the DB through the interface file.
"""
from __future__ import annotations

import re

from ..model import Kind, Rel
from ..pipeline.facts import FileFacts
from ..text import decode_source, words
from .text import _glob_rx

NAME = "layout"
VERSION = "1"

DATA_EXT = r"(?:dat|csv|tsv|txt|fix|fixed|seq|lst|prn|gz|zip)"
_NAME_H = re.compile(r"(?i)^(項目名?\(?物理\)?|物理名|物理項目名|項目ID|フィールド名?|field(_?name)?|name|項目名称?|カラム名?|column)$")
_LOGICAL_H = re.compile(r"(?i)^(論理名|項目名\(?論理\)?|名称|日本語名|label|description)$")
_START_H = re.compile(r"(?i)^(開始位置|開始桁|位置|開始|start|pos(ition)?|offset|from)$")
_LEN_H = re.compile(r"(?i)^(桁数|桁|長さ|バイト数|byte(s)?|サイズ|size|len(gth)?|width)$")
_TYPE_H = re.compile(r"(?i)^(型|属性|データ型|type|形式)$")
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{1,40}$")
_PIC = re.compile(r"(?im)^\s*(\d{2})\s+([A-Za-z][\w\-]*)\s+(?:.*?\bPIC(?:TURE)?\s+(?:IS\s+)?([^\s.]+(?:\.\d+)?)\s*(COMP-3|COMP|BINARY|PACKED-DECIMAL)?)?")


def is_layout(rules, path: str) -> bool:
    cfg = getattr(rules, "layout", None) or {}
    p = path if "/" in path else "./" + path
    low = path.lower()
    if cfg.get("exts") and not low.endswith(tuple(cfg["exts"])) and "." in low.rsplit("/", 1)[-1]:
        return False
    return any(_glob_rx(g).match(path) or _glob_rx(g).match(p) for g in cfg.get("files", []))


def _pic_len(pic: str, usage: str | None) -> int | None:
    n = 0
    for ch, rep in re.findall(r"([X9AZSVP*])(?:\((\d+)\))?", pic.upper()):
        if ch in "SVP":
            continue
        n += int(rep) if rep else 1
    if not n:
        return None
    if usage and usage.upper() in ("COMP-3", "PACKED-DECIMAL"):
        return n // 2 + 1
    return n


def _split(line: str):
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    if line.count("|") >= 2:
        return [c.strip() for c in line.strip().strip("|").split("|")]
    if "," in line:
        return [c.strip().strip('"') for c in line.split(",")]
    return re.split(r"\s+", line.strip())


def parse(src: str):
    """-> list of fields {name, logical, start, length, type, line}."""
    if _PIC.search(src) and re.search(r"(?i)\bPIC(TURE)?\b", src):
        out, pos = [], 1
        for m in _PIC.finditer(src):
            if not m.group(3):
                continue
            ln = _pic_len(m.group(3), m.group(4))
            out.append({"name": m.group(2), "start": pos, "length": ln, "type": m.group(3),
                        "line": src.count("\n", 0, m.start()) + 1})
            pos += ln or 0
        return out
    lines = src.splitlines()
    cols = None
    out = []
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith(("#", "//", "*", "--", ";")) or re.fullmatch(r"[\s|:\-=+]+", s):
            continue
        cells = _split(s)
        if cols is None or not out:
            hm = {}
            for ci, c in enumerate(cells):
                for key, rx in (("name", _NAME_H), ("logical", _LOGICAL_H), ("start", _START_H),
                                ("length", _LEN_H), ("type", _TYPE_H)):
                    if key not in hm and rx.match(c.replace(" ", "").replace("　", "")):
                        hm[key] = ci
                        break
            if "length" in hm and ("name" in hm or "logical" in hm):
                cols = hm
                continue
        if cols:
            get = lambda k: cells[cols[k]] if k in cols and cols[k] < len(cells) else ""
            name, logical = get("name"), get("logical")
            if not _IDENT.match(name or ""):
                logical = logical or name
                name = next((c for c in cells if _IDENT.match(c) and not c.isdigit()), "")
            if not name:
                continue
            out.append({"name": name, "logical": logical or None, "start": _int(get("start")),
                        "length": _int(get("length")), "type": get("type") or None, "line": i})
            continue
        # no header: NAME [logical] [start] length ...; the first identifier and the numbers
        name = next((c for c in cells if _IDENT.match(c) and not re.fullmatch(r"[XN9]\(?\d*\)?", c)), None)
        nums = [int(c) for c in cells if re.fullmatch(r"\d{1,5}", c)]
        if not name or not nums:
            continue
        logical = next((c for c in cells if re.search(r"[^\x00-\x7f]", c)), None)
        out.append({"name": name, "logical": logical, "nums": nums, "line": i})
    if out and "nums" in out[0]:
        _positions(out)
    return out


def _int(v):
    m = re.match(r"\s*(\d{1,5})", v or "")
    return int(m.group(1)) if m else None


def _positions(fields):
    """Without a header: (start, length) when the numbers chain (start + length = next
    start), else the first number is the length."""
    chain = len(fields) > 1 and all(len(f["nums"]) >= 2 for f in fields) and all(
        a["nums"][0] + a["nums"][1] == b["nums"][0] for a, b in zip(fields, fields[1:]))
    pos = 1
    for f in fields:
        nums = f.pop("nums")
        if chain:
            f["start"], f["length"] = nums[0], nums[1]
        else:
            f["start"], f["length"] = pos, nums[0]
            pos += nums[0]


def extract(path: str, data: bytes, rules) -> FileFacts:
    f = FileFacts(path, NAME)
    src = decode_source(data)
    fid = f"file:{path}"
    fields = parse(src)
    base = path.rsplit("/", 1)[-1]
    f.node(fid, Kind.CONFIG, base, 1, src.count("\n") + 1, words(path) + " " + re.sub(r"\s+", " ", src)[:3000],
           keys=[fid], layout=True, fields=len(fields))
    total = 0
    for fl in fields:
        nid = f"layout:{path}#{fl['name']}"
        props = {k: fl[k] for k in ("start", "length", "type", "logical") if fl.get(k) is not None}
        f.node(nid, Kind.LAYOUT_FIELD, fl["name"], fl["line"], fl["line"],
               " ".join(str(x) for x in (fl["name"], words(fl["name"]), fl.get("logical") or "")),
               keys=[nid], layout=path, **props)
        f.edge(fid, nid, Rel.CONTAINS, 1.0, "parser")
        col = fl["name"].replace("-", "_").lower()
        extra = {"length": str(fl["length"])} if fl.get("length") else {}
        f.ref(nid, f"colany:{col}", Rel.MAPS_TO, 0.75, fl["line"], via="layout", optional=True, **extra)
        total = max(total, (fl.get("start") or 0) + (fl.get("length") or 0) - 1)
    named = set()
    for m in re.finditer(r"([\w$.{}\-]+\." + DATA_EXT + r")\b", src):
        if m.group(1) not in named and not m.group(1).lower().endswith(base.lower()):
            named.add(m.group(1))
            f.ref(fid, f"datafile:{m.group(1)}", Rel.DEFINES, 0.85, src.count("\n", 0, m.start()) + 1, via="layout",
                  optional=True)
    if not named:
        f.ref(fid, f"datastem:{base.rsplit('.', 1)[0]}", Rel.DEFINES, 0.7, 1, via="layout", optional=True)
    if total:
        f.payload["record_length"] = total
    return f
