"""Transaction analysis over the linked graph.

Finds where transactions start and what runs inside them, then lists the
places where a change is risky because of how commits happen:

* boundaries — EJB container-managed transactions (annotation, ejb-jar.xml,
  EJB default REQUIRED), Spring @Transactional, bean-managed transactions
  (UserTransaction.begin / Connection.commit ...), and stored procedures that
  commit by themselves;
* scope — everything reachable from a boundary through calls, interface
  implementations, SQL execution and stored-procedure calls, until another
  boundary starts its own transaction (REQUIRES_NEW / NOT_SUPPORTED) or the
  call leaves the process (SOAP / REST / JMS);
* risks — remote calls inside a transaction, independent commits
  (REQUIRES_NEW, autonomous transactions, COMMIT inside a procedure called by a
  container transaction), splitting commits inside loops, row locks, updates
  through database links, file output and asynchronous messages that a
  rollback does not undo.

Everything is read from node / edge properties the extractors and rule packs
produce (tx_attr, tx_management, tx_calls, tx, remote_role, dblink ...), so the
analysis itself knows no framework. Results are written back as node
properties (`tx`, `tx_risks`, `tx_in`) and summarised in the index report.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..model import Kind, Rel

# attribute values -> canonical names (ejb-jar.xml writes RequiresNew, annotations REQUIRES_NEW)
_ATTR = {re.sub(r"[_\s]", "", a).lower(): a for a in
         ("REQUIRED", "REQUIRES_NEW", "MANDATORY", "SUPPORTS", "NOT_SUPPORTED", "NEVER", "NESTED")}
STARTS = {"REQUIRED", "REQUIRES_NEW", "NESTED"}       # begin a transaction when none is active
SUSPENDS = {"REQUIRES_NEW", "NOT_SUPPORTED", "NEVER"}  # do not take part in the caller's transaction

RISK = {
    "REMOTE_IN_TX": ("high", "トランザクション内でリモート呼び出し（{what}）。ロールバックしても相手システムの更新は戻らないため、補償処理・再送・タイムアウト時の扱いを確認"),
    "INDEPENDENT_TX": ("medium", "{what} は別トランザクション（{attr}）で先にコミットされる。呼び出し元がロールバックしても結果が残る"),
    "PROC_COMMIT_IN_TX": ("high", "ストアド {what} が内部でCOMMIT/ROLLBACKする。呼び出し元トランザクションの途中で確定・破棄される（XA環境ではエラー）"),
    "AUTONOMOUS": ("medium", "{what} は自律型トランザクション（PRAGMA AUTONOMOUS_TRANSACTION）で独立にコミットする"),
    "DBLINK_WRITE": ("high", "DBリンク {what} 経由の更新。分散トランザクション（2相コミット）となり、リンク先の障害・ロック待ちの影響を受ける"),
    "COMMIT_IN_LOOP": ("high", "{what} はループ内で分割コミットする。途中で失敗すると一部だけ確定するため、再実行・リカバリ設計を確認"),
    "LOCK": ("low", "{what} で行ロック（SELECT … FOR UPDATE 等）。ロック順序・待機時間・デッドロックに注意"),
    "ASYNC_MESSAGE": ("medium", "{what} へのメッセージ送信。受信側は別トランザクションで非同期に処理される"),
    "FILE_OUTPUT_IN_TX": ("medium", "トランザクション内でファイル {what} を出力。ロールバックしてもファイルは残る"),
}
LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}
MAX_DEPTH = 12
MAX_MEMBERS = 400


def norm_attr(v) -> str | None:
    if not v:
        return None
    v = str(v).split(".")[-1]
    return _ATTR.get(re.sub(r"[_\s]", "", v).lower(), v.upper())


def _class_of(nid: str) -> str | None:
    return nid.split("#", 1)[0] if nid.startswith("java:") and "#" in nid else None


class TxAnalyzer:
    def __init__(self, nodes: dict, edges: list):
        self.nodes = nodes
        self.out = defaultdict(list)
        self.inc = defaultdict(list)
        for e in edges:
            self.out[e.src].append(e)
            self.inc[e.dst].append(e)

    # ------------------------------------------------------------ configuration
    def attr(self, mid: str) -> tuple[str | None, str]:
        """Effective transaction attribute of a method and where it came from."""
        n = self.nodes.get(mid)
        if n is None or n.kind != Kind.METHOD or n.props.get("ctor"):
            return None, ""
        for e in self.inc.get(mid, []):          # deployment descriptor overrides annotations
            if e.rel == Rel.CONFIGURES and e.props.get("tx_attr"):
                return norm_attr(e.props["tx_attr"]), "descriptor"
        if n.props.get("tx_attr"):
            return norm_attr(n.props["tx_attr"]), "annotation"
        c = self.nodes.get(_class_of(mid) or "")
        if c is None:
            return None, ""
        if str(c.props.get("tx_management", "")).upper().endswith("BEAN"):
            return "BEAN", "bean-managed"
        for e in self.inc.get(c.id, []):
            if e.rel == Rel.CONFIGURES and str(e.props.get("transaction_type", "")).lower() == "bean":
                return "BEAN", "bean-managed"
        if c.props.get("tx_attr_class"):
            return norm_attr(c.props["tx_attr_class"]), "class annotation"
        if c.props.get("ejb") and c.props.get("type", "class") == "class":
            return "REQUIRED", "EJB default"
        return None, ""

    # ------------------------------------------------------------ local traits
    def local_risks(self, nid: str) -> list[dict]:
        n = self.nodes[nid]
        out = []
        tx = n.props.get("tx") if isinstance(n.props.get("tx"), dict) and n.kind == Kind.DB_OBJECT else {}
        if tx.get("autonomous"):
            out.append(self._risk("AUTONOMOUS", nid, what=n.name))
        if tx.get("commit_in_loop"):
            out.append(self._risk("COMMIT_IN_LOOP", nid, what=n.name))
        if tx.get("locks"):
            out.append(self._risk("LOCK", nid, what=n.name))
        calls = n.props.get("tx_calls") or []
        if any(c.get("op") == "commit" and c.get("loop") for c in calls):
            out.append(self._risk("COMMIT_IN_LOOP", nid, what=n.name, line=next(
                c.get("line") for c in calls if c.get("op") == "commit" and c.get("loop"))))
        if n.kind == Kind.SQL_STATEMENT and re.search(r"\bfor\s+update\b", n.props.get("sql") or "", re.I):
            out.append(self._risk("LOCK", nid, what=n.name))
        return out

    def _risk(self, code, at, root=None, line=None, **fmt):
        level, msg = RISK[code]
        r = {"code": code, "level": level, "message": msg.format(**{"what": "", "attr": "", **fmt}), "at": at}
        if root:
            r["root"] = root
        if line:
            r["line"] = line
        return r

    # ------------------------------------------------------------ scope
    def scope(self, root: str, root_attr: str):
        members, risks, tables, remote = [], [], set(), []
        seen = {root}
        stack = [(root, 0)]
        while stack and len(members) < MAX_MEMBERS:
            nid, depth = stack.pop()
            members.append(nid)
            n = self.nodes[nid]
            if nid != root:
                risks += [dict(r, root=root) for r in self.local_risks(nid)]
            if n.kind == Kind.DB_OBJECT and isinstance(n.props.get("tx"), dict):
                t = n.props["tx"]
                if (t.get("commits") or t.get("rollbacks")) and not t.get("autonomous") and root_attr != "PLSQL":
                    risks.append(self._risk("PROC_COMMIT_IN_TX", nid, root, what=n.name))
            for e in self.out.get(nid, []):
                d = self.nodes.get(e.dst)
                if d is None or e.dst in seen:
                    continue
                via = str(e.props.get("via") or "")
                # ---- leaving the process
                if d.kind == Kind.ENDPOINT and e.rel in (Rel.REFERENCES, Rel.CALLS) and n.kind == Kind.METHOD:
                    remote.append(e.dst)
                    risks.append(self._risk("REMOTE_IN_TX", nid, root, e.props.get("line"), what=f"REST {d.name}"))
                    seen.add(e.dst)
                    continue
                if via.startswith("soap-client") or d.props.get("remote_role") == "soap-provider" and \
                        n.props.get("remote_call"):
                    remote.append(e.dst)
                    risks.append(self._risk("REMOTE_IN_TX", nid, root, e.props.get("line"), what=f"SOAP {d.name}"))
                    seen.add(e.dst)
                    continue
                if e.rel in (Rel.READS, Rel.WRITES):
                    if d.kind == Kind.TABLE:
                        tables.add(d.name)
                        if e.rel == Rel.WRITES and e.props.get("dblink"):
                            risks.append(self._risk("DBLINK_WRITE", nid, root, e.props.get("line"),
                                                    what=f"@{e.props['dblink']}（{d.name}）"))
                    elif d.kind == Kind.DATA_FILE and e.rel == Rel.WRITES and n.kind != Kind.SCRIPT:
                        risks.append(self._risk("FILE_OUTPUT_IN_TX", nid, root, e.props.get("line"), what=d.name))
                    continue
                if depth >= MAX_DEPTH:
                    continue
                # ---- fields: injected message destinations, data sources
                if e.rel == Rel.REFERENCES and d.kind == Kind.FIELD:
                    for fe in self.out.get(e.dst, []):
                        if fe.props.get("via") == "jndi-resource":
                            t = self.nodes.get(fe.dst)
                            if t is not None and (t.props.get("ejb") == "MessageDriven" or
                                                  "jms" in (d.props.get("jndi") or "").lower()):
                                risks.append(self._risk("ASYNC_MESSAGE", nid, root, e.props.get("line"),
                                                        what=d.props.get("jndi") or t.name))
                    continue
                if e.rel not in (Rel.CALLS, Rel.IMPLEMENTED_BY, Rel.EXECUTES):
                    continue
                # ---- another boundary
                a, _ = self.attr(e.dst) if d.kind == Kind.METHOD else (None, "")
                if a in SUSPENDS or a == "BEAN":
                    risks.append(self._risk("INDEPENDENT_TX", nid, root, e.props.get("line"), what=d.name,
                                            attr=a if a != "BEAN" else "Bean管理"))
                    seen.add(e.dst)
                    continue
                seen.add(e.dst)
                stack.append((e.dst, depth + 1))
        return members, risks, sorted(tables), remote

    # ------------------------------------------------------------ main
    def run(self) -> dict:
        roots = []
        for nid, n in self.nodes.items():
            if n.kind == Kind.METHOD:
                a, src = self.attr(nid)
                if a in STARTS:
                    roots.append((nid, a, src))
                elif a == "BEAN" and any(c.get("op") in ("begin", "commit") for c in n.props.get("tx_calls") or []):
                    roots.append((nid, "BEAN", src))
                elif n.props.get("tx_calls") and any(c.get("op") == "commit" for c in n.props["tx_calls"]):
                    roots.append((nid, "PROGRAMMATIC", "commit call"))
            elif n.kind == Kind.DB_OBJECT and isinstance(n.props.get("tx"), dict) and \
                    (n.props["tx"].get("commits") or n.props["tx"].get("autonomous")):
                roots.append((nid, "PLSQL", "COMMIT in procedure"))
        member_of = defaultdict(list)
        all_risks = []
        boundaries = []
        for nid, a, src in sorted(roots):
            members, risks, tables, remote = self.scope(nid, a)
            risks = self.local_risks(nid) + risks
            # one entry per (code, place)
            uniq = {}
            for r in risks:
                uniq.setdefault((r["code"], r["at"]), r)
            risks = sorted(uniq.values(), key=lambda r: (LEVEL_ORDER[r["level"]], r["code"]))
            n = self.nodes[nid]
            n.props["tx"] = {**(n.props["tx"] if isinstance(n.props.get("tx"), dict) else {}),
                             "boundary": a, "source": src, "members": len(members), "tables": tables[:30],
                             "remote": remote[:10]}
            n.props["tx_risks"] = risks
            for m in members[1:]:
                member_of[m].append(nid)
            for r in risks:
                if r["at"] != nid:
                    at = self.nodes.get(r["at"])
                    if at is not None:
                        lst = at.props.setdefault("tx_risks", [])
                        if not any(x["code"] == r["code"] and x.get("root") == r.get("root") for x in lst):
                            lst.append(r)
            all_risks += risks
            boundaries.append({"id": nid, "name": n.name, "boundary": a, "source": src, "members": len(members),
                               "risks": len(risks)})
        # nodes outside any boundary keep their local traits (a procedure nobody calls still locks)
        for nid, n in self.nodes.items():
            if nid in member_of or "tx_risks" in n.props:
                continue
            lr = self.local_risks(nid)
            if lr:
                n.props["tx_risks"] = lr
                all_risks += lr
        for m, rs in member_of.items():
            self.nodes[m].props["tx_in"] = sorted(set(rs))[:8]
        # a REQUIRED method whose callers are themselves inside a transaction joins that one
        top = [b for b in boundaries if b["boundary"] in ("REQUIRES_NEW", "BEAN", "PLSQL", "PROGRAMMATIC")
               or not member_of.get(b["id"])]
        by_code = Counter(r["code"] for r in all_risks)
        return {"boundaries": len(boundaries), "top_level": len(top), "risks": dict(by_code.most_common()),
                "samples": sorted(top, key=lambda b: -b["risks"])[:30]}


def analyze(nodes: dict, edges: list) -> dict:
    """Annotate nodes with transaction scope and risks; returns the summary."""
    return TxAnalyzer(nodes, edges).run()
