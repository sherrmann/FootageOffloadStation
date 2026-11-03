import io
import json
import os
import zipfile

import pytest

flask = pytest.importorskip("flask")

from footage_offload import paths  # noqa: E402
from footage_offload.camera.simulated import SimulatedBackend  # noqa: E402
from footage_offload.config import Config  # noqa: E402
from footage_offload.service import Service, wait_until  # noqa: E402
from footage_offload.state import State  # noqa: E402
from footage_offload.status import StatusBus  # noqa: E402
from footage_offload.stick import Stick  # noqa: E402

RUN = "2026-11-03_21-47"
PASSWORD = "correct horse"


class FakeHelper:
    """Records helper calls; actions in `fail` raise."""

    def __init__(self):
        self.calls = []
        self.fail = set()

    def call(self, action, **args):
        self.calls.append((action, args))
        if action in self.fail:
            raise RuntimeError(f"{action} failed")
        return {}


class FakeStickManager:
    def __init__(self, service, root):
        self.service = service
        self.root = root
        self.ejected = 0
        self.present = True

    def start(self):
        self.refresh()

    def stop(self):
        pass

    def stick(self):
        return Stick(self.root) if self.present else None

    def refresh(self):
        s = Stick(self.root)
        oldest, newest = s.oldest_newest()
        total, free = s.space()
        self.service.status.update(stick={
            "present": True, "adopted": True, "device": "/dev/sda1", "label": "TEST",
            "fstype": "ext4", "total": total, "free": free, "oldest": oldest,
            "newest": newest, "runs": len(s.run_folders()), "read_only": False, "ejected": False})

    def eject(self):
        self.ejected += 1


class FakeSysInfo:
    def throttled(self):
        return {"undervoltage_now": False, "undervoltage_occurred": True}

    def latest_version(self):
        raise RuntimeError("offline")
    # ntp_synchronised and update_available deliberately missing


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SECRET_KEY_FILE", str(tmp_path / "state" / "secret_key"))
    monkeypatch.setattr(paths, "STATE_DIR", str(tmp_path / "state"))
    webdir = tmp_path / "web"
    webdir.mkdir()
    (webdir / "index.html").write_text("<!doctype html><title>FOS</title>")
    (webdir / "app.js").write_text("console.log('hi');")
    monkeypatch.setattr(paths, "WEB_DIR", str(webdir))
    i18ndir = tmp_path / "i18n"
    i18ndir.mkdir()
    (i18ndir / "de.json").write_text(json.dumps({"status.waiting": "Wartet auf Kamera"}))
    monkeypatch.setattr(paths, "I18N_DIR", str(i18ndir))
    from footage_offload import i18n
    i18n.reload()

    stickdir = tmp_path / "stick"
    stickdir.mkdir()
    (stickdir / paths.MARKER_NAME).write_text("")
    cams = tmp_path / "cams"
    cams.mkdir()

    config = Config(str(tmp_path / "etc" / "config.json"))
    config.update({"min_free_mb": 0, "retry_base_delay_s": 0.0, "chunk_size_kb": 64})
    state = State(str(tmp_path / "state.db"))
    helper = FakeHelper()
    backend = SimulatedBackend(str(cams))
    service = Service(config, backend, helper, lambda svc: FakeStickManager(svc, str(stickdir)),
                      status=StatusBus(min_interval=0), state=state)
    from footage_offload.web import create_app
    app = create_app(service, config, state, service.stick_manager, helper, sysinfo=FakeSysInfo())
    app.testing = True

    class W:
        pass

    w = W()
    w.app, w.client, w.service, w.config, w.state = app, app.test_client(), service, config, state
    w.helper, w.stickdir, w.cams, w.backend, w.tmp = helper, stickdir, cams, backend, tmp_path
    yield w
    service.stop()
    state.close()
    i18n.reload()


def make_run(w, files, run=RUN):
    d = w.stickdir / run
    d.mkdir(exist_ok=True)
    for name, data in files.items():
        (d / name).write_bytes(data)
    return d


def csrf(client):
    return client.get("/api/v1/session").get_json()["csrf_token"]


def enable_login(w):
    r = w.client.put("/api/v1/settings", json={"password": PASSWORD, "login_enabled": True})
    assert r.status_code == 200, r.get_json()


# pages, headers ------------------------------------------------------------------
def test_index_static_and_headers(web):
    r = web.client.get("/")
    assert r.status_code == 200 and b"FOS" in r.data
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert web.client.get("/static/app.js").status_code == 200
    assert web.client.get("/static/../app.py").status_code == 404
    assert web.client.get("/static/nope.js").status_code == 404


def test_secret_key_file_created_private(web):
    st = os.stat(paths.SECRET_KEY_FILE)
    assert st.st_mode & 0o777 == 0o600


def test_session_login_off(web):
    r = web.client.get("/api/v1/session", headers={"Accept-Language": "de-CH,de;q=0.9"})
    data = r.get_json()
    assert data["login_enabled"] is False and data["logged_in"] is True
    assert data["language"] == "de" and data["csrf_token"]
    cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    # No CSRF needed while login is off.
    assert web.client.get("/api/v1/status").status_code == 200
    assert web.client.post("/api/v1/stick/eject").status_code == 200


def test_i18n_endpoint(web):
    assert web.client.get("/api/v1/i18n/de").get_json() == {"status.waiting": "Wartet auf Kamera"}
    assert web.client.get("/api/v1/i18n/xx").status_code == 404


# login and CSRF ----------------------------------------------------------------------
def test_login_flow_and_csrf(web):
    enable_login(web)
    # The client that switched login on stays logged in.
    assert web.client.get("/api/v1/status").status_code == 200

    other = web.app.test_client()
    r = other.get("/api/v1/status")
    assert r.status_code == 401 and r.get_json()["error"] == "error.login_required"
    assert other.get("/media/x/y").status_code == 401
    assert other.get("/").status_code == 200                    # UI must load for the login form
    assert other.get("/api/v1/i18n/de").status_code == 200
    s = other.get("/api/v1/session").get_json()
    assert s["login_enabled"] is True and s["logged_in"] is False
    token = s["csrf_token"]

    r = other.post("/api/v1/login", json={"password": "wrong password"})
    assert r.status_code == 401 and r.get_json()["error"] == "error.wrong_password"
    r = other.post("/api/v1/login", json={"password": PASSWORD})
    assert r.status_code == 200
    assert other.get("/api/v1/session").get_json()["logged_in"] is True
    assert other.get("/api/v1/status").status_code == 200

    # Writes need the CSRF header.
    r = other.put("/api/v1/settings", json={"min_free_mb": 5})
    assert r.status_code == 403 and r.get_json()["error"] == "error.csrf"
    r = other.put("/api/v1/settings", json={"min_free_mb": 5}, headers={"X-CSRF-Token": "nope"})
    assert r.status_code == 403
    r = other.put("/api/v1/settings", json={"min_free_mb": 5}, headers={"X-CSRF-Token": token})
    assert r.status_code == 200 and r.get_json()["min_free_mb"] == 5

    assert other.post("/api/v1/logout", headers={"X-CSRF-Token": token}).status_code == 200
    assert other.get("/api/v1/status").status_code == 401


def test_password_change_ends_other_sessions(web):
    enable_login(web)
    other = web.app.test_client()
    other.get("/api/v1/session")
    assert other.post("/api/v1/login", json={"password": PASSWORD}).status_code == 200
    token = csrf(web.client)
    r = web.client.put("/api/v1/settings", json={"password": "another secret"},
                       headers={"X-CSRF-Token": token})
    assert r.status_code == 200
    assert web.client.get("/api/v1/status").status_code == 200   # changer stays logged in
    assert other.get("/api/v1/status").status_code == 401


def test_login_rate_limit(web):
    enable_login(web)
    other = web.app.test_client()
    for _ in range(5):
        assert other.post("/api/v1/login", json={"password": "bad password"}).status_code == 401
    r = other.post("/api/v1/login", json={"password": PASSWORD})
    assert r.status_code == 401
    assert r.get_json()["retry_after"] > 0 and int(r.headers["Retry-After"]) > 0


# settings --------------------------------------------------------------------------
def test_settings_validation(web):
    r = web.client.put("/api/v1/settings", json={"mqtt_port": "x", "bogus": 1, "web_port": 0})
    assert r.status_code == 400
    assert r.get_json()["errors"] == {"mqtt_port": "error.type_int", "bogus": "error.unknown_setting",
                                      "web_port": "error.out_of_range"}
    r = web.client.put("/api/v1/settings", json={"login_enabled": True})
    assert r.get_json()["errors"] == {"login_enabled": "error.password_not_set"}
    r = web.client.put("/api/v1/settings", json={"login_enabled": True, "password": "short"})
    assert r.status_code == 400
    assert r.get_json()["errors"] == {"password": "error.password_too_short"}
    r = web.client.put("/api/v1/settings", json={"password_hash": "scrypt$x"})
    assert r.status_code == 400
    r = web.client.put("/api/v1/settings", data="[1]", content_type="application/json")
    assert r.status_code == 400
    assert web.config.get("login_enabled") is False and web.config.get("password_hash") == ""


def test_settings_secrets(web):
    r = web.client.put("/api/v1/settings", json={"nas_password": "s3cret", "nas_username": "bob"})
    assert r.status_code == 200
    data = r.get_json()
    assert "nas_password" not in data and data["nas_password_set"] is True
    assert "s3cret" not in web.client.get("/api/v1/settings").get_data(as_text=True)
    # Empty string keeps the old value; *_set echoes are ignored.
    r = web.client.put("/api/v1/settings", json={"nas_password": "", "nas_password_set": True,
                                                 "nas_username": "alice"})
    assert r.status_code == 200
    assert web.config.get("nas_password") == "s3cret"
    assert web.config.get("nas_username") == "alice"
    # Password is stored hashed only.
    web.client.put("/api/v1/settings", json={"password": PASSWORD})
    assert web.config.get("password_hash").startswith("scrypt$")
    assert web.client.get("/api/v1/settings").get_json()["password_hash_set"] is True


def test_settings_samba_calls_helper(web):
    r = web.client.put("/api/v1/settings", json={"samba_enabled": True})
    assert r.status_code == 200
    assert web.helper.calls == [("samba", {"enabled": True})]
    web.client.put("/api/v1/settings", json={"samba_enabled": True, "min_free_mb": 2})
    assert len(web.helper.calls) == 1                # unchanged: no call
    web.helper.fail.add("samba")
    r = web.client.put("/api/v1/settings", json={"samba_enabled": False})
    assert r.status_code == 500
    body = r.get_json()
    assert body["error"] == "error.helper" and "samba failed" in body["detail"]
    assert web.config.get("samba_enabled") is True   # reverted, so a retry calls again


# stick, eject, system ------------------------------------------------------------------
def test_eject_busy(web):
    web.service.current_run = object()
    r = web.client.post("/api/v1/stick/eject")
    assert r.status_code == 409 and r.get_json()["error"] == "error.busy"
    assert web.service.stick_manager.ejected == 0
    web.service.current_run = None
    assert web.client.post("/api/v1/stick/eject").status_code == 200
    assert web.service.stick_manager.ejected == 1


def test_shutdown_and_reboot(web):
    web.service.current_run = object()
    assert web.client.post("/api/v1/system/shutdown").status_code == 409
    assert web.client.post("/api/v1/system/reboot", json={"force": "yes"}).status_code == 409
    assert web.helper.calls == []
    assert web.client.post("/api/v1/system/shutdown", json={"force": True}).status_code == 202
    web.service.current_run = None
    assert web.client.post("/api/v1/system/reboot").status_code == 202
    assert [c[0] for c in web.helper.calls] == ["shutdown", "reboot"]
    web.helper.fail.add("reboot")
    r = web.client.post("/api/v1/system/reboot")
    assert r.status_code == 500 and r.get_json()["error"] == "error.helper"


def test_system_degrades(web):
    data = web.client.get("/api/v1/system").get_json()
    assert data["throttled"] == {"undervoltage_now": False, "undervoltage_occurred": True}
    assert data["latest_version"] is None and data["update_available"] is False
    assert data["ntp_synchronised"] is None and data["version"] and data["hostname"]


def test_stick_endpoint(web):
    web.service.stick_manager.refresh()
    make_run(web, {"a.MP4": b"x" * 10})
    make_run(web, {"b.JPG": b"y" * 3, "c.JPG": b"z"}, run="2026-11-04_08-00")
    data = web.client.get("/api/v1/stick").get_json()
    assert data["adopted"] is True
    assert data["runs"] == [{"name": "2026-11-04_08-00", "files": 2, "size": 4},
                            {"name": RUN, "files": 1, "size": 10}]


def test_history(web):
    assert web.client.get("/api/v1/history?limit=5&offset=0").get_json() == {"runs": [], "devices": []}
    assert web.client.get("/api/v1/history?limit=x").status_code == 400


# media -----------------------------------------------------------------------------
def test_range_download(web):
    data = bytes(range(256)) * 40
    make_run(web, {"GH010001.MP4": data, "GH010001.THM": b"\xff\xd8thumb"})
    r = web.client.get(f"/media/{RUN}/GH010001.MP4", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.data == data[100:200]
    assert r.headers["Content-Range"] == f"bytes 100-199/{len(data)}"
    assert r.headers["Content-Type"] == "video/mp4"
    assert r.headers["Content-Disposition"].startswith("attachment")
    r = web.client.get(f"/media/{RUN}/GH010001.MP4")
    assert r.status_code == 200 and r.data == data
    r = web.client.get(f"/media/{RUN}/GH010001.THM?inline=1")
    assert r.headers["Content-Type"] == "image/jpeg"
    assert r.headers["Content-Disposition"].startswith("inline")


def test_lrv_mimetype(web):
    make_run(web, {"GL010001.LRV": b"proxy"})
    r = web.client.get(f"/media/{RUN}/GL010001.LRV?inline=1")
    assert r.status_code == 200 and r.headers["Content-Type"] == "video/mp4"


@pytest.mark.parametrize("url", [
    f"/media/{RUN}/..%2F.footage-offload-station",
    f"/media/{RUN}/%2E%2E",
    "/media/..%2F..%2Fetc/passwd",
    "/media/..%2F/x",
    f"/media/{RUN}/.hidden",
    f"/media/{RUN}/missing.MP4",
    "/media/not-a-run/file.MP4",
    f"/media/{RUN}/link.MP4",
    f"/media/{RUN}/sub",
    "/media/%2Fetc/passwd",
])
def test_path_traversal_rejected(web, url):
    d = make_run(web, {"ok.MP4": b"ok", ".hidden": b"h"})
    (web.tmp / "outside.txt").write_text("secret")
    (d / "link.MP4").symlink_to(web.tmp / "outside.txt")
    (d / "sub").mkdir()
    r = web.client.get(url)
    assert r.status_code in (400, 404)
    assert b"secret" not in r.data


def test_media_without_stick(web):
    web.service.stick_manager.present = False
    r = web.client.get(f"/media/{RUN}/x.MP4")
    assert r.status_code == 404 and r.get_json()["error"] == "error.no_stick"
    assert web.client.get("/api/v1/gallery").get_json() == {"runs": []}


# gallery ---------------------------------------------------------------------------
def test_gallery_grouping(web):
    make_run(web, {
        "GH010001.MP4": b"v" * 100, "GH010001.THM": b"t", "GL010001.LRV": b"l" * 10,
        "GH020001.MP4": b"c" * 50,                       # second chapter, no previews
        "GOPR0002.JPG": b"p" * 20,
        "GH010003_1.MP4": b"v", "GH010003_1.THM": b"t", "GL010003_1.LRV": b"l",
        "ORPHAN.THM": b"o", "notes.txt": b"n",
    })
    make_run(web, {"GOPR0009.JPG": b"x"}, run="2026-11-02_10-00")
    runs = web.client.get("/api/v1/gallery").get_json()["runs"]
    assert [r["name"] for r in runs] == [RUN, "2026-11-02_10-00"]            # newest first
    items = {i["name"]: i for i in runs[0]["items"]}
    assert set(items) == {"GH010001.MP4", "GH020001.MP4", "GOPR0002.JPG", "GH010003_1.MP4",
                          "ORPHAN.THM", "notes.txt"}
    v = items["GH010001.MP4"]
    assert v["kind"] == "video" and v["size"] == 100
    assert v["thumb"] == f"/media/{RUN}/GH010001.THM?inline=1"
    assert v["preview"] == f"/media/{RUN}/GL010001.LRV?inline=1"
    assert v["download"] == f"/media/{RUN}/GH010001.MP4"
    assert v["recording"] == "0001" and items["GH020001.MP4"]["recording"] == "0001"
    assert items["GH020001.MP4"]["thumb"] is None and items["GH020001.MP4"]["preview"] is None
    c = items["GH010003_1.MP4"]
    assert c["thumb"].endswith("GH010003_1.THM?inline=1") and c["preview"].endswith("GL010003_1.LRV?inline=1")
    p = items["GOPR0002.JPG"]
    assert p["kind"] == "photo" and p["recording"] == "0002"
    assert items["ORPHAN.THM"]["kind"] == "other" and items["notes.txt"]["kind"] == "other"


def test_gallery_after_real_run(web):
    camdir = web.cams / "cam" / "DCIM" / "100GOPRO"
    camdir.mkdir(parents=True)
    (camdir / "GH010007.MP4").write_bytes(os.urandom(5000))
    (camdir / "GH010007.THM").write_bytes(os.urandom(100))
    (camdir / "GL010007.LRV").write_bytes(os.urandom(700))
    web.service.start()
    web.service.camera_added(web.backend.device("cam"))
    assert wait_until(lambda: web.service.status.snapshot()["state"] == "done" and not web.service.busy())
    runs = web.client.get("/api/v1/gallery").get_json()["runs"]
    assert len(runs) == 1
    (item,) = runs[0]["items"]
    assert item["name"] == "GH010007.MP4" and item["thumb"] and item["preview"]
    r = web.client.get(item["download"], headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.data) == 10
    hist = web.client.get("/api/v1/history").get_json()
    assert hist["runs"][0]["files"] == 3 and hist["devices"][0]["serial"] == "SIMCAM"


# SSE -----------------------------------------------------------------------------
def test_sse_first_event_and_unsubscribe(web):
    bus = web.service.status
    before = len(bus._subscribers)
    r = web.client.get("/api/v1/events", buffered=False)
    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    assert r.headers["Cache-Control"] == "no-cache" and r.headers["X-Accel-Buffering"] == "no"
    it = iter(r.response)
    chunk = next(it)
    chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
    assert "event: status\n" in chunk
    data_line = [ln for ln in chunk.splitlines() if ln.startswith("data: ")][0]
    assert json.loads(data_line[6:])["state"] == "waiting"
    assert len(bus._subscribers) == before + 1
    bus.update(state="transferring")
    chunk = next(it)
    chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
    assert '"state":"transferring"' in chunk
    r.close()
    assert len(bus._subscribers) == before


# diagnostics -----------------------------------------------------------------------
def test_diagnostics_zip(web, monkeypatch):
    from footage_offload import diagnostics
    monkeypatch.setattr(diagnostics, "COMMAND_TIMEOUT_S", 5)
    monkeypatch.setattr(diagnostics, "JOURNAL_TIMEOUT_S", 5)
    web.config.update({"mqtt_password": "topsecret", "mqtt_host": "broker"})
    web.service.current_run = object()          # busy: no gphoto2 summary
    r = web.client.get("/api/v1/diagnostics")
    web.service.current_run = None
    assert r.status_code == 200 and r.mimetype == "application/zip"
    assert r.headers["Content-Disposition"].startswith("attachment")
    zf = zipfile.ZipFile(io.BytesIO(r.data))
    names = set(zf.namelist())
    assert {"README.txt", "versions.json", "config.json", "history/runs.json",
            "logs/journal.txt", "system/df.txt"} <= names
    cfg = json.loads(zf.read("config.json"))
    assert cfg["mqtt_password"] == "<set>" and cfg["mqtt_host"] == "broker"
    for name in names:
        assert b"topsecret" not in zf.read(name)
    assert b"Skipped" in zf.read("camera/gphoto2-summary.txt")
    # Nothing left behind in the state directory.
    assert not [n for n in os.listdir(paths.STATE_DIR) if n.startswith(".diag")]


def test_server_binds_and_stops(web):
    import urllib.request
    from footage_offload.web.server import serve
    srv = serve(web.app, 0)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/api/v1/session", timeout=5) as resp:
            assert json.loads(resp.read())["version"]
    finally:
        srv.shutdown()
