"""Value flow across methods: which screen input reaches which column.

Built from the per-method summaries of `extractors/javaflow.py`, at link time when calls
and types can be resolved:

* flows inside a method; a call passes its arguments to the parameters of the resolved
  method(s) (an interface method to every implementation) and takes back its return
  value; an unresolved (library) call passes its receiver and arguments through to its
  result (`trim()`, `parseInt`, `format` keep carrying the value);
* a field of a declared type is one location for every object of that type, so a value
  set on a DTO in the action is the value the DAO reads from it;
* a callee that fills an object it was given (`copy(form, dto)`) passes those fields back
  to the caller's object;
* sinks bind values to SQL parameter markers: JDBC `ps.setXxx(n, v)` (numbered, counted
  `i++`, or a list of collected values), JdbcTemplate arguments, named parameters
  (`:name`, Map / ParameterSource keys), MyBatis `#{prop}` of the mapper method's
  parameters, iBATIS `#prop#` of the statement id used.

From every field (and request parameter) the analysis follows the flows forward and adds
`FLOWS_TO` edges field -> column, with `usage` write / cond and the hops as evidence.
Nothing is guessed from names here; what the source does not tell produces no edge.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque

from ..model import Kind, Rel
from .sqlparams import markers

MAX_STATES = 4000
MAX_PATH = 4
_NO_PASS = {"equals", "equalsIgnoreCase", "isEmpty", "isBlank", "size", "length", "contains", "containsKey",
            "startsWith", "endsWith", "indexOf", "compareTo", "hashCode", "getClass", "executeUpdate",
            "executeQuery", "execute", "next", "close", "matches", "isNotEmpty", "isNotBlank", "hasNext",
            "debug", "info", "warn", "error", "trace", "println", "print", "log", "addError", "add", "put",
            "prepareStatement", "prepareCall", "getConnection", "commit", "rollback", "setAutoCommit",
            "forward", "findForward", "saveErrors", "saveMessages", "getSession", "<init>"}
_PASS_NEW = re.compile(r"^(String|Integer|Long|BigDecimal|BigInteger|Double|Short|Timestamp|Date|java\.sql\.\w+|"
                       r"StringBuffer|StringBuilder|Float)$")


def _split(atom: str):
    base, _, path = atom.partition("|")
    return base, tuple(p for p in path.split(".") if p)


class Flow:
    def __init__(self, resolver, linker):
        self.jr = resolver
        self.lk = linker
        self.edges = defaultdict(list)        # node -> [(src_path, dst, dst_path, hop)]
        self.impls = defaultdict(list)
        for fqn in self.jr.cls:
            for a in self.jr.ancestors(fqn):
                self.impls[a].append(fqn)
        self.mutates: dict[str, set] = {}     # method id -> {(param index, path)}
        self.stmt_cache: dict[str, list] = {}

    # ------------------------------------------------------------ locations
    def loc(self, c: dict, mid: str, atom: str):
        base, path = _split(atom)
        if base == "TH":
            if not path:
                return None
            f = self.jr.fields(c["fqn"]).get(path[0])
            if f is None:
                o = self.jr.cls.get(c.get("outer") or "")
                f = self.jr.fields(o["fqn"]).get(path[0]) if o else None
            if f is None or f[1].get("constant") is not None:
                return None
            return f"F:{f[0]}", path
        if base.startswith("T:"):
            t = base[2:]
            if t.startswith("TF:"):
                f = self.jr.fields(c["fqn"]).get(t[3:])
                fq = self.jr._elem(f[1]["type"], self.jr.cls[f[0]]) if f else None
            else:
                fq = self.jr.resolve(t, c)
            return (f"F:{fq}", path) if fq else None
        if base in ("S", "Q"):
            return base, path
        return f"{mid}::{base}", path

    def add(self, src, dst, hop):
        if src is None or dst is None or src == dst:
            return
        self.edges[src[0]].append((src[1], dst[0], dst[1], hop))

    # ------------------------------------------------------------ build
    def build(self):
        for fqn, c in self.jr.cls.items():
            for m in c["methods"]:
                fl = m.get("flow")
                if not fl:
                    continue
                mid = self.jr.mid(fqn, m)
                muts = set()
                for s, d, line in fl["f"]:
                    db, dp = _split(d)
                    if re.fullmatch(r"p\d+", db) and dp:
                        muts.add((int(db[1:]), dp))
                self.mutates[mid] = muts
        for fqn, c in self.jr.cls.items():
            for m in c["methods"]:
                fl = m.get("flow")
                if fl:
                    self._method(c, m, fl)
            for m in c["methods"]:
                if c["kind"] == "interface" and not m["is_ctor"]:
                    self._mapper(c, m)

    def _hop(self, c, m, line, what=""):
        return f"{c['name']}.{m['name']}():{line}" + (f" {what}" if what else "")

    def _method(self, c, m, fl):
        mid = self.jr.mid(c["fqn"], m)
        L = lambda a: self.loc(c, mid, a)
        for s, d, line in fl["f"]:
            self.add(L(s), L(d), self._hop(c, m, line))
        for call in fl["c"]:
            self._call(c, m, mid, call, L)
        lists = fl.get("lists") or {}
        for sk in fl["s"]:
            self._sink(c, m, mid, sk, L, lists, fl["s"])

    def _targets(self, c, m, call):
        if call.get("new"):
            r = self.jr.resolve(call["new"], c)
            if not r:
                return []
            return [(r, x) for x in self.jr.cls[r]["methods"] if x["is_ctor"] and len(x["params"]) == call["a"]]
        rt = self.jr._receiver(c, m, call.get("r"))
        if not rt or rt not in self.jr.cls:
            return []
        out = []
        for o, tm in self.jr.methods(rt, call["n"], call["a"])[:3]:
            out.append((o, tm))
            if tm.get("abstract") or self.jr.cls[o]["kind"] == "interface":
                for sub in self.impls.get(o, []):
                    for sm in self.jr.cls[sub]["methods"]:
                        if sm["name"] == tm["name"] and len(sm["params"]) == len(tm["params"]) and not sm.get("abstract"):
                            out.append((sub, sm))
        return out

    def _call(self, c, m, mid, call, L):
        k = call["k"]
        res = (f"{mid}::c{k}", ())
        hop = self._hop(c, m, call["l"], call["n"] if call["n"] != "<init>" else f"new {call.get('new')}")
        targets = self._targets(c, m, call)
        if targets:
            for o, tm in targets:
                tid = self.jr.mid(o, tm)
                for j, atoms in enumerate(call["args"]):
                    for a in atoms:
                        src = L(a)
                        self.add(src, (f"{tid}::p{j}", ()), hop)
                        # a callee that fills the object it was given hands the fields back
                        if src is not None and not src[1]:
                            for pj, path in self.mutates.get(tid, ()):
                                if pj == j:
                                    self.add((f"{tid}::p{j}", path), (src[0], path), hop + " (戻り)")
                self.add((f"{tid}::ret", ()), res, hop)
            return
        if call["n"] in _NO_PASS and not (call["n"] == "<init>" and _PASS_NEW.match(call.get("new") or "")):
            return
        # library call: the value passes through (trim, parseInt, format, new BigDecimal(s))
        for a in call["recv"] + [x for atoms in call["args"] for x in atoms]:
            self.add(L(a), res, hop)

    # ------------------------------------------------------------ sinks
    def _const(self, c, text: str | None) -> str:
        def one(mm):
            ref = mm.group(1)
            if "." in ref:
                cls, _, fld = ref.rpartition(".")
                fq = self.jr.resolve(cls, c)
            else:
                fq, fld = c["fqn"], ref
            f = self.jr.fields(fq).get(fld) if fq and fq in self.jr.cls else None
            return (f[1].get("constant") or "") if f else ""
        for _ in range(3):
            if not text or "{{C:" not in text:
                break
            text = re.sub(r"\{\{C:([\w.$]+)\}\}", one, text)
        return text or ""

    def col(self, mk: dict):
        if not mk.get("column"):
            return None
        t = mk.get("table")
        return (f"COL:{t}.{mk['column']}" if t else f"COLANY:{mk['column']}"), (mk["usage"] or "cond",)

    def _sink(self, c, m, mid, sk, L, lists, sinks):
        hop = self._hop(c, m, sk["l"])
        k = sk["k"]
        if k == "pos":
            sql = self._const(c, sk.get("sql"))
            mks = markers(sql)
            if not mks:
                return
            same = [x for x in sinks if x["k"] == "pos" and x.get("stmt") == sk.get("stmt")]
            idx = sk["idx"]
            if idx == "seq":
                seqs = [x for x in same if x["idx"] == "seq"]
                fixed = sorted({x["idx"] for x in same if isinstance(x["idx"], int)})
                i = seqs.index(sk)
                free = [n for n in range(1, len(mks) + 1) if n not in fixed]
                idx = free[i] if i < len(free) else None
            if isinstance(idx, str) and idx.startswith("list:"):
                for n, atoms in enumerate(lists.get(idx[5:], [])):
                    if n < len(mks):
                        for a in atoms:
                            self.add(L(a), self.col(mks[n]), hop + f" ?{n + 1}")
                return
            if isinstance(idx, int) and 1 <= idx <= len(mks):
                for a in sk["v"]:
                    self.add(L(a), self.col(mks[idx - 1]), hop + f" ?{idx}")
        elif k == "jdbc":
            mks = markers(self._const(c, sk["sql"]))
            pos = 0
            for v in sk["vals"]:
                if isinstance(v, str) and v.startswith("list:"):
                    for atoms in lists.get(v[5:], []):
                        if pos < len(mks):
                            for a in atoms:
                                self.add(L(a), self.col(mks[pos]), hop + f" ?{pos + 1}")
                        pos += 1
                else:
                    if pos < len(mks):
                        for a in v:
                            self.add(L(a), self.col(mks[pos]), hop + f" ?{pos + 1}")
                    pos += 1
        elif k == "named":
            for mk in markers(self._const(c, sk["sql"])):
                if not mk.get("name"):
                    continue
                for a in sk["v"]:
                    src = L(a)
                    if src:
                        self.add((src[0], src[1] + tuple(mk["name"].split("."))), self.col(mk), hop + f" :{mk['name']}")
        elif k == "stmt":
            for sid, sql, fe in self._statement(sk["id"]):
                for mk in markers(sql):
                    if not mk.get("name"):
                        continue
                    parts = self._param_path(mk["name"], fe)
                    for a in sk["v"]:
                        src = L(a)
                        if src:
                            self.add((src[0], src[1] + parts), self.col(mk), hop + f" {sid} #{mk['name']}")

    def _statement(self, sid: str):
        if sid in self.stmt_cache:
            return self.stmt_cache[sid]
        out = []
        for key in (f"sql:{sid}", f"ibatis:{sid}"):
            for nid in self.lk.keys.get(key.lower(), []) or self.lk.keys.get(key, []):
                n = self.lk.nodes.get(nid)
                if n is not None and n.props.get("sql"):
                    out.append((nid, n.props["sql"], n.props.get("foreach") or []))
        if not out:
            short = sid.split(".")[-1]
            for nid in self.lk.keys.get(f"ibatis:{short}".lower(), []):
                n = self.lk.nodes.get(nid)
                if n is not None and n.props.get("sql"):
                    out.append((nid, n.props["sql"], n.props.get("foreach") or []))
        self.stmt_cache[sid] = out
        return out

    @staticmethod
    def _param_path(name: str, foreach) -> tuple:
        parts = [p for p in re.sub(r"\[\d*\]", "", name).split(".") if p]
        for coll, item in foreach or []:
            if parts and item and parts[0] == item:
                cp = [p for p in (coll or "").split(".") if p and p not in ("list", "array", "collection")]
                return tuple(cp + parts[1:])
        return tuple(parts)

    def _mapper(self, c, m):
        """A MyBatis mapper method: its parameters fill the #{...} of the bound statement."""
        mid = self.jr.mid(c["fqn"], m)
        names = [p[1] for p in m["params"]]
        for e in self.lk.out_edges_of(mid):
            if e[1] != Rel.EXECUTES:
                continue
            n = self.lk.nodes.get(e[0])
            if n is None or not n.props.get("sql"):
                continue
            fe = n.props.get("foreach") or []
            for mk in markers(n.props["sql"]):
                if not mk.get("name"):
                    continue
                parts = self._param_path(mk["name"], fe)
                if not parts:
                    continue
                if parts[0] in names and len(names) > 1 or (parts[0] in names and len(parts) > 1):
                    j, rest = names.index(parts[0]), parts[1:]
                elif len(names) == 1:
                    j, rest = 0, parts if parts[0] not in names else parts[1:]
                else:
                    continue
                self.add((f"{mid}::p{j}", tuple(rest)), self.col(mk), f"{c['name']}.{m['name']}() {n.name} #{{{mk['name']}}}")

    def _dyna_sources(self):
        """DynaActionForm properties (declared in struts-config, read as form.get("x")): the
        form parameter of the action class's methods, by property name."""
        props = {}
        for nid, n in self.lk.nodes.items():
            if nid.startswith("formprop:"):
                form, _, p = nid[len("formprop:"):].partition(".")
                props.setdefault(form, []).append((nid, p))
        out = []
        if not props:
            return out
        for nid, n in self.lk.nodes.items():
            if n.kind != Kind.ENDPOINT or not n.props.get("form") or n.props["form"] not in props:
                continue
            cls = self.jr.resolve(n.props.get("action_type") or "", None)
            if not cls or cls not in self.jr.cls:
                continue
            for m in self.jr.cls[cls]["methods"]:
                for i, (pt, pn) in enumerate(m["params"]):
                    if "Form" in pt:
                        mid = self.jr.mid(cls, m)
                        for pid, p in props[n.props["form"]]:
                            out.append((pid, (f"{mid}::p{i}", (p,))))
        return out

    # ------------------------------------------------------------ reach
    def reach(self, start):
        """Columns reached from (node, path): {(col, usage): hops}."""
        seen = {start: None}
        q = deque([start])
        out = {}
        while q and len(seen) < MAX_STATES:
            st = q.popleft()
            node, path = st
            for ep, dn, dp, hop in self.edges.get(node, ()):
                if path[:len(ep)] == ep:
                    nxt = (dn, (dp + path[len(ep):])[:MAX_PATH])
                elif ep[:len(path)] == path:
                    nxt = (dn, dp)
                else:
                    continue
                if dn.startswith("COL"):
                    usage = dp[0] if dp else "cond"
                    if (dn, usage) not in out:
                        hops, cur = [hop], st
                        while seen.get(cur) is not None and len(hops) < 12:
                            cur, h = seen[cur]
                            hops.append(h)
                        out[(dn, usage)] = list(reversed(hops))
                    continue
                if nxt not in seen:
                    seen[nxt] = (st, hop)
                    q.append(nxt)
        return out

    def run(self):
        self.build()
        sources = []
        for fqn, c in self.jr.cls.items():
            if c["kind"] in ("interface", "annotation", "enum"):
                continue
            for fd in c["fields"]:
                if fd.get("constant") is not None or fd.get("static"):
                    continue
                sources.append((f"java:{fqn}.{fd['name']}", (f"F:{fqn}", (fd["name"],))))
        sources += self._dyna_sources()
        qkeys = {ep[0] for ep, *_ in self.edges.get("Q", []) if ep}
        for k in sorted(qkeys):
            sources.append((f"reqparam:{k}", ("Q", (k,))))
        out = []
        for nid, st in sources:
            for (colkey, usage), hops in self.reach(st).items():
                out.append((nid, colkey, usage, hops))
        return out
