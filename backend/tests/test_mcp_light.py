"""MCP for coding agents: a short first answer, compact elements, a warm start."""
import importlib
import json
import os
import subprocess
import time

import pytest

from himotoki.setup_cmds import index_workspace, prepare_demo
from himotoki.workspace import Workspace

NOTE = "受注の備考（NOTE）の上限を200文字から500文字に拡張する"


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    d = prepare_demo(str(tmp_path_factory.mktemp("mcp") / "sor"))
    for r in ("order-web", "inventory-svc", "shipping-svc"):
        p = os.path.join(d, "repos", r)
        for a in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "i"]):
            subprocess.run(["git", "-C", p, *a], check=True, capture_output=True)
    ws = Workspace(d)
    index_workspace(ws, d)
    return d


def _mcp(d, monkeypatch):
    monkeypatch.setenv("HIMOTOKI_DATA_DIR", d)
    import himotoki.mcp_server as m
    importlib.reload(m)
    return m


def test_impact_files_is_short_and_points_at_files(demo, monkeypatch):
    m = _mcp(demo, monkeypatch)
    r = m.impact_files(NOTE)
    assert len(json.dumps(r, ensure_ascii=False)) < 12000          # a first answer, not a dump
    assert 0 < len(r["files"]) <= 10
    f0 = r["files"][0]
    assert f0["file"] and f0["elements"] and ("→" in f0["why"] or "⇒" in f0["why"])
    files = {f["file"] for f in r["files"]}
    assert any(f.endswith("OrderForm.java") for f in files)
    assert r["more"]["candidates"] >= len(r["files"])
    # what it names can be opened compactly
    g = m.get_element(f0["elements"][0]["id"], lines=10)
    assert g["file"] == f0["file"] and len(g["code"].splitlines()) <= 10
    assert set(g) >= {"used_by", "uses", "docs", "tests"} and len(json.dumps(g, ensure_ascii=False)) < 8000


def test_preload_and_cached_search_index(demo, monkeypatch):
    m = _mcp(demo, monkeypatch)
    m._preload()
    e = m.ws().engine(None)
    assert os.path.exists(os.path.join(e.data_dir, "search.pkl"))
    # a second process opening the same graph reuses the index (same results, no refit)
    from himotoki.analysis.search import NodeSearch
    calls = []
    orig = NodeSearch._save
    monkeypatch.setattr(NodeSearch, "_save", lambda self, *a: calls.append(1) or orig(self, *a))
    w2 = Workspace(demo)
    e2 = w2.engine(None)
    assert not calls
    q = "受注 備考"
    assert [n.id for n, _ in e2.search.query(q, k=5)] == [n.id for n, _ in e.search.query(q, k=5)]
    # re-indexing changes the graph file, so the index is rebuilt rather than reused stale
    time.sleep(0.01)
    index_workspace(w2, demo)
    w3 = Workspace(demo)
    w3.engine(None)
    assert calls
