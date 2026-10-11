"""First steps: workspace layout, environment check, demo, and the viewer shipped inside
the package (no Node.js needed)."""
import hashlib
import json
import os

import pytest

from himotoki import cli
from himotoki.setup_cmds import MARKER, doctor, find_workspace, init_workspace, prepare_demo, workspace_repos


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch):
    # the CLI exports HIMOTOKI_DATA_DIR / HIMOTOKI_RULES for the process; undo after each test
    for k in ("HIMOTOKI_DATA_DIR", "HIMOTOKI_RULES"):
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)

HERE = os.path.dirname(__file__)
PKG = os.path.join(HERE, "..", "himotoki")
FRONT = os.path.join(HERE, "..", "..", "frontend")


def _source_hash(root):
    files = []
    for d, _, fs in os.walk(os.path.join(root, "src")):
        files += [os.path.join(d, f) for f in fs]
    files += [os.path.join(root, "index.html"), os.path.join(root, "package.json")]
    h = hashlib.sha256()
    for rel, p in sorted((os.path.relpath(p, root).replace(os.sep, "/"), p) for p in files):
        h.update((rel + "\0").encode())
        h.update(open(p, encoding="utf-8").read().replace("\r\n", "\n").encode())
        h.update(b"\0")
    return h.hexdigest()


def test_bundled_viewer_is_complete_and_current():
    web = os.path.join(PKG, "web")
    html = open(os.path.join(web, "index.html"), encoding="utf-8").read()
    import re
    for asset in re.findall(r'(?:src|href)="/(assets/[^"]+)"', html):
        assert os.path.isfile(os.path.join(web, asset)), asset
    if os.path.isdir(os.path.join(FRONT, "src")):
        info = json.load(open(os.path.join(web, "BUILD_INFO.json")))
        assert info["source_hash"] == _source_hash(FRONT), \
            "viewer sources changed: run `npm run build` in frontend/ (it refreshes backend/himotoki/web)"


def test_init_makes_a_workspace_found_from_subfolders(tmp_path):
    ws = init_workspace(str(tmp_path / "my-system"), quiet=True)
    for sub in ("repos", "docs", "db", "rules"):
        assert os.path.isdir(os.path.join(ws, sub))
    assert os.path.isfile(os.path.join(ws, MARKER)) and os.path.isfile(os.path.join(ws, "settings.yaml"))
    assert "SPOOL ALL_TAB_COLUMNS.csv" in open(os.path.join(ws, "db", "export_oracle.sql"), encoding="utf-8").read()
    (tmp_path / "my-system" / "settings.yaml").write_text("llm:\n  max_items: 5\n", encoding="utf-8")
    init_workspace(ws, quiet=True)                                   # never overwrites what the user edited
    assert "max_items: 5" in (tmp_path / "my-system" / "settings.yaml").read_text(encoding="utf-8")
    assert find_workspace(os.path.join(ws, "repos")) == ws
    assert find_workspace(str(tmp_path)) is None


def test_index_without_arguments_inside_a_workspace(tmp_path, monkeypatch, capsys):
    ws = prepare_demo(str(tmp_path / "demo"))
    import shutil
    shutil.copytree(os.path.join(HERE, "..", "..", "samples", "tour-env", "db", "oracle_catalog"),
                    os.path.join(ws, "db", "proddb"))                 # dictionary export picked up by index
    assert set(workspace_repos(ws)) == {"order-web", "inventory-svc", "shipping-svc"}
    monkeypatch.chdir(os.path.join(ws, "repos"))
    cli.main(["index"])
    meta = json.load(open(os.path.join(ws, "workspace.json"), encoding="utf-8"))
    g = meta["graphs"]["demo"]
    assert set(g["repos"]) == {"order-web", "inventory-svc", "shipping-svc"} and g["docs_dir"] is None
    assert os.path.isfile(os.path.join(ws, "sources", "catalog_proddb.json"))
    assert doctor(ws, ws) == 0
    out = capsys.readouterr().out
    assert "repos/" in out and "3 件" in out


def test_index_without_repo_outside_a_workspace_explains(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as ex:
        cli.main(["index"])
    assert "--repo" in str(ex.value)


def test_demo_runs_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["demo", "--no-serve"])
    err = capsys.readouterr().err
    assert "影響候補" in err and "himotoki init" in err
    assert os.path.isfile(tmp_path / "himotoki-demo" / "workspace.json")


def test_doctor_reports_an_empty_workspace(tmp_path, capsys):
    ws = init_workspace(str(tmp_path / "w"), quiet=True)
    assert doctor(ws, ws) == 1                     # repos/ is empty: the one blocking problem
    out = capsys.readouterr().out
    assert "git clone" in out


def test_shift_jis_sources_are_read():
    from himotoki.extractors import java, text
    from himotoki.rules import load_rules
    from himotoki.text import decode_source
    rules = load_rules()
    src = "package a;\n/** 受注サービス */\npublic class Foo { }\n".encode("cp932")
    f = java.extract("a/Foo.java", src, rules)
    assert "受注サービス" in f.nodes[0]["text"]
    f = text.extract("ops/x.sh", "#!/bin/sh\n# 受注出力\ncp /a/b.csv /c/d.csv\n".encode("cp932"), rules)
    assert "受注出力" in f.nodes[0]["text"]
    assert decode_source("備考".encode("utf-8")) == "備考" and decode_source(b"\xef\xbb\xbfx") == "x"
