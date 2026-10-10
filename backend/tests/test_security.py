"""Security regression tests: each test is an attack that must fail."""
import hashlib
import hmac
import importlib
import json
import os
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient

from impact.security import check_ref, office_zip_ok, redact, safe_join

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "minirepo")


def _client(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("IMPACT_DATA_DIR", str(tmp_path / "data"))
    for k in ("IMPACT_API_TOKEN", "IMPACT_WEBHOOK_SECRET", "IMPACT_ALLOWED_ROOTS", "IMPACT_CORS_ORIGINS",
              "IMPACT_ALLOW_DB_FROM_API", "IMPACT_ALLOWED_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import impact.api as api
    importlib.reload(api)
    return api, TestClient(api.app)


def _indexed(api, client, repo=FIX):
    assert client.post("/api/index", json={"repo": repo}).status_code == 200
    import time
    for _ in range(200):
        if client.get("/api/index").json().get("status") == "done":
            return
        time.sleep(0.05)
    raise AssertionError("index did not finish")


def test_node_detail_cannot_read_outside_repository(tmp_path, monkeypatch):
    api, c = _client(tmp_path, monkeypatch)
    _indexed(api, c)
    for nid in ("file:../../../../../../etc/passwd", "file:/etc/passwd", "file:..%2F..%2Fetc%2Fpasswd",
                "file:design:../../../etc/passwd"):
        r = c.get(f"/api/nodes/{nid}")
        assert r.status_code == 404 or "root:" not in r.text, nid
    # a file inside the repository is still readable
    ok = c.get("/api/nodes/file:src/main/resources/db/schema.sql")
    assert ok.status_code == 200 and ok.json()["code"]


def test_no_wildcard_cors_and_host_check(tmp_path, monkeypatch):
    api, c = _client(tmp_path, monkeypatch)
    r = c.options("/api/index", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") is None
    # DNS rebinding: a page on attacker.example resolving to 127.0.0.1 sends its own Host
    assert c.get("/api/status", headers={"Host": "attacker.example"}).status_code == 400
    r = c.get("/api/status")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_token_required_when_configured(tmp_path, monkeypatch):
    api, c = _client(tmp_path, monkeypatch, IMPACT_API_TOKEN="s3cr3t-token")
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer s3cr3t-token"}).status_code == 200
    # browser flow: ?token= sets an HttpOnly SameSite=Strict cookie and strips the token from the URL
    r = c.get("/?token=s3cr3t-token&run=abc", follow_redirects=False)
    assert r.status_code == 303 and "token" not in r.headers["location"]
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc
    assert c.get("/api/status").status_code == 200          # cookie now present
    assert c.get("/?token=nope", follow_redirects=False).status_code == 401


def test_websocket_rejects_cross_site_and_unauthenticated(tmp_path, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    api, c = _client(tmp_path, monkeypatch, IMPACT_API_TOKEN="t0ken")
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/runs/x") as ws:
            ws.receive_json()
    api, c = _client(tmp_path, monkeypatch)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/runs/x", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_paths_restricted_to_allowed_roots(tmp_path, monkeypatch):
    allowed = tmp_path / "repos"
    allowed.mkdir()
    api, c = _client(tmp_path, monkeypatch, IMPACT_ALLOWED_ROOTS=str(allowed))
    assert c.post("/api/index", json={"repo": "/etc"}).status_code == 403
    assert c.post("/api/index", json={"repo": str(allowed) + "/../../etc"}).status_code == 403
    assert c.post("/api/sources/runtime", json={"kind": "csv", "path": "/etc/passwd"}).status_code == 403
    # names become file names: no traversal
    assert c.post("/api/sources/runtime", json={"kind": "csv", "name": "../x", "path": str(allowed)}).status_code == 400
    assert c.get("/api/status", params={"graph": "../../../tmp/evil"}).status_code == 200
    assert not (tmp_path / "evil").exists()


def test_db_connections_from_api_are_opt_in(tmp_path, monkeypatch):
    api, c = _client(tmp_path, monkeypatch)
    r = c.post("/api/sources/catalog", json={"kind": "postgres", "dsn": "postgresql://u:pw@10.0.0.1/db"})
    assert r.status_code == 403


def test_webhook_requires_secret_and_valid_refs(tmp_path, monkeypatch):
    api, c = _client(tmp_path, monkeypatch)
    assert c.post("/api/hooks/git", json={"object_kind": "push"}).status_code == 503
    api, c = _client(tmp_path, monkeypatch, IMPACT_WEBHOOK_SECRET="hooksecret")
    body = json.dumps({"ref": "refs/heads/--upload-pack=touch /tmp/pwned", "after": "x"}).encode()
    sig = "sha256=" + hmac.new(b"hooksecret", body, hashlib.sha256).hexdigest()
    h = {"x-github-event": "push", "x-hub-signature-256": sig, "content-type": "application/json"}
    assert c.post("/api/hooks/git", content=body, headers=h).status_code == 400
    h["x-hub-signature-256"] = "sha256=" + "0" * 64
    assert c.post("/api/hooks/git", content=body, headers=h).status_code == 401
    assert c.post("/api/hooks/git", json={"object_kind": "push"}, headers={"x-gitlab-token": "nope"}).status_code == 401


def test_errors_do_not_leak_internals(tmp_path, monkeypatch):
    api, c = _client(tmp_path, monkeypatch)
    r = c.post("/api/runs", json={"requirement": "x" * 200_001})
    assert r.status_code == 422
    assert c.post("/api/runs", json={"requirement": "a", "depth": 99}).status_code == 422
    assert "postgresql://u:***@" in redact("psql: postgresql://u:hunter2@db/x") and "hunter2" not in \
        redact("password=hunter2 host=db")


def test_ref_validation():
    for bad in ("--upload-pack=x", "-x", "a..b", "a b", "a;b", "a\nb", "", "x" * 300, "a.lock"):
        with pytest.raises(ValueError):
            check_ref(bad)
    for good in ("main", "release/5.7.1.SP1.RELEASE", "feature/remarks-200", "v1.2", "HEAD~1", "abc123^"):
        assert check_ref(good) == good


def test_safe_join():
    assert safe_join("/repo", "a/b.java") == "/repo/a/b.java"
    for bad in ("../etc/passwd", "/etc/passwd", "a/../../x", "a\0b"):
        assert safe_join("/repo", bad) is None


def test_symlinks_and_oversized_files_are_not_indexed(tmp_path, monkeypatch):
    from impact.pipeline.run import Pipeline
    from impact.store import LocalStore
    repo = tmp_path / "r"
    (repo / "conf").mkdir(parents=True)
    secret = tmp_path / "secret.properties"
    secret.write_text("db.password=hunter2\n")
    os.symlink(secret, repo / "conf" / "app.properties")
    (repo / "conf" / "big.properties").write_text("k=" + "v" * (2 * 1024 * 1024))
    (repo / "conf" / "ok.properties").write_text("greeting=hello\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    monkeypatch.setenv("IMPACT_MAX_FILE_MB", "1")
    store = LocalStore(str(tmp_path / "g.json"))
    stats = Pipeline(str(tmp_path / "d"), workers=0).index(str(repo), store)
    text = json.dumps([n.to_dict() for n in store.nodes()])
    assert "hunter2" not in text
    assert "greeting" in text
    assert stats["errors"] >= 1                  # the oversized file is reported, not silently dropped


def test_xml_entities_and_zip_bombs_are_refused(tmp_path):
    from impact.extractors import xml as xmlx
    from impact.rules import load_rules
    bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;">]><l>&b;</l>'
    with pytest.raises(ValueError):
        xmlx.extract("x.xml", bomb, load_rules())
    xxe = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
    with pytest.raises(ValueError):
        xmlx.extract("y.xml", xxe, load_rules())
    p = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/big.xml", b"0" * (60 * 1024 * 1024))
    assert office_zip_ok(p.read_bytes(), limit_mb=50)
    assert office_zip_ok(b"not a zip")
