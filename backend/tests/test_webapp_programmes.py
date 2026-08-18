import importlib

from starlette.testclient import TestClient

import database


def test_api_programmes_endpoint(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    database.init_db(db_file)
    monkeypatch.setenv("DATABASE_PATH", db_file)
    monkeypatch.setenv("WEB_AUTH_SECRET", "")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "")

    import webapp.app as app_module
    importlib.reload(app_module)
    c = TestClient(app_module.app)

    # 1. GET /api/programmes
    res = c.get("/api/programmes")
    assert res.status_code == 200
    data = res.json()
    assert "templates" in data
    assert len(data["templates"]) > 0
    assert "key" in data["templates"][0]
    assert data["templates"][0]["key"] == "hybrid_powerbuilding"

    # 2. POST /api/programmes/select with template_key
    sel_res = c.post("/api/programmes/select", json={"template_key": "hybrid_powerbuilding"})
    assert sel_res.status_code == 200
    assert sel_res.json()["status"] == "ok"
    assert sel_res.json()["template_key"] == "hybrid_powerbuilding"

    # 3. Verify active programme was set in database
    legacy_uid = database.get_legacy_user_id(db_file)
    active = database.get_active_programme(legacy_uid, db_path=db_file)
    assert active is not None
    assert active["template_key"] == "hybrid_powerbuilding"
