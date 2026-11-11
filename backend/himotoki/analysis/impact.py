"""What kind of impact a candidate has.

"Affected" covered three different things that need different work:

* `change`     — the element's content has to be edited (code, SQL, layout, document, data);
* `verify`     — nothing in it changes, but something it depends on does, so its
                 behaviour is checked by testing (a caller, a reader of a changed column,
                 the job after a changed program, a downstream interface);
* `coordinate` — no edit here, but people or procedures outside the code are involved
                 (the receiving system, production master data, the job schedule, a release step);

plus `none` (connected but unrelated to this change) and `unknown` (not enough to judge).

The AI judge decides the kind and gives evidence (what to edit / what to test / whom
to coordinate with); without the AI a rule gives a first guess from how the element was
reached (the relation and its direction). A reviewer's decision carries the final kind.
Downstream: the estimate splits implementation from test effort, the hand-off and the
issues list edits separately from test points and coordination items, and verification
counts only predicted edits as "predicted but not changed".
"""
from __future__ import annotations

from ..model import Kind, Rel

IMPACTS = ("change", "verify", "coordinate", "none", "unknown")
IMPACT_JA = {"change": "修正", "verify": "試験", "coordinate": "調整", "none": "影響なし", "unknown": "判断できず"}
AFFECTING = ("change", "verify", "coordinate")

NAMED = ("seed", "pattern", "human", "focus")          # found from the requirement itself
DOC_KINDS = {Kind.DOC, Kind.DOC_ENTITY}
TEST_KINDS = {Kind.TEST_CLASS, Kind.TEST_METHOD}

# (relation, direction of the step that reached the element) -> kind. "in": the element
# points at the one it was reached from (a caller, a reader); "out": it is pointed at.
STEP_KIND = {
    (Rel.WRITES, "in"): "change",          # writes the changed data: has to write the new value / column
    (Rel.POPULATES, "in"): "change",       # initial data / master rows
    (Rel.DEFINES, "in"): "change",         # the DDL / definition
    (Rel.MAPS_TO, "in"): "change", (Rel.MAPS_TO, "out"): "change",
    (Rel.BINDS, "in"): "change",           # a screen field bound to the changed item
    (Rel.CONTAINS, "in"): "change",        # the class / file holding a changed element
    (Rel.EXECUTES, "out"): "change",       # the SQL a changed method runs
    (Rel.DESCRIBES, "in"): "change", (Rel.DOC_CRUD, "in"): "change",
    (Rel.CALLS, "in"): "verify", (Rel.REFERENCES, "in"): "verify", (Rel.INCLUDES, "in"): "verify",
    (Rel.RENDERS, "in"): "verify", (Rel.HANDLES, "in"): "verify", (Rel.USES_MESSAGE, "in"): "verify",
    (Rel.CALLS, "out"): "verify", (Rel.REFERENCES, "out"): "verify",
    (Rel.READS, "in"): "verify",           # reads the changed data: behaviour may change, code usually not
    (Rel.TESTS, "in"): "verify",
    (Rel.RUNS, "in"): "verify", (Rel.RUNS, "out"): "verify",
    (Rel.PRECEDES, "in"): "verify", (Rel.PRECEDES, "out"): "verify",
    (Rel.EXTENDS, "in"): "verify", (Rel.IMPLEMENTED_BY, "out"): "verify", (Rel.IMPLEMENTED_BY, "in"): "verify",
    (Rel.CONTAINS, "out"): "verify",
}


def rule_impact(item: dict, verdict: str, intent_change: str = "modify") -> tuple[str, dict]:
    """A first guess without the AI, from how the element was reached."""
    if verdict == "not_impacted":
        return "none", {}
    kind, origin = item.get("kind"), item.get("origin")
    via, d = item.get("via_rel"), item.get("via_dir")
    basis = item.get("path_reason") or ""
    if kind in TEST_KINDS:
        return ("change" if origin in NAMED else "verify"), {"check": "既存のテストを実行し、必要なら期待値を直す", "basis": basis}
    if origin in NAMED or kind in DOC_KINDS:
        return "change", {"where": basis}
    k = STEP_KIND.get((via, d))
    if k == "verify" and intent_change == "delete" and via == Rel.READS:
        k = "change"                       # a reader of something being removed has to change
    if item.get("data_flow") and k != "change":
        return "verify", {"check": "連携先でレイアウト・桁・値の扱いを確認する", "basis": basis}
    if k == "change":
        return "change", {"where": basis}
    if k == "verify":
        return "verify", {"check": "変更される要素につながるため、動作を確認する", "basis": basis}
    return ("verify" if verdict == "impacted" else "unknown"), {"basis": basis}


def normalise(verdict: str | None, impact: str | None) -> tuple[str | None, str | None]:
    """Keep the verdict and the kind consistent with each other."""
    if impact not in IMPACTS:
        impact = None
    if impact == "none":
        return "not_impacted", impact
    if impact == "unknown":
        return ("uncertain" if verdict != "not_impacted" else verdict), impact
    if impact in AFFECTING and verdict == "not_impacted":
        return verdict, "none"
    return verdict, impact


def effective(c: dict) -> str | None:
    """The kind that counts: the reviewer's when decided, the judge's otherwise."""
    dec = c.get("decision") or {}
    if dec.get("status") == "excluded":
        return "none"
    if dec.get("status") == "confirmed":
        k = dec.get("impact") or c.get("impact")
        return k if k in AFFECTING else "change"
    return c.get("impact")
