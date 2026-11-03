import json
import os

import pytest

from footage_offload import config as cfgmod
from footage_offload.camera.base import DeviceInfo, RemoteFile
from footage_offload.profiles import GenericProfile, GoProProfile, preview_names, profile_for
from footage_offload.service import Service, wait_until
from footage_offload.stick import Stick, is_run_folder, run_folder_name


# config ---------------------------------------------------------------------
def test_config_defaults_and_update(tmp_path):
    c = cfgmod.Config(str(tmp_path / "c.json"))
    assert c.get("web_port") == 8080
    c.update({"min_free_mb": 500})
    data = json.loads((tmp_path / "c.json").read_text())
    assert data["min_free_mb"] == 500
    assert oct(os.stat(tmp_path / "c.json").st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("change,key,msg", [
    ({"web_port": 0}, "web_port", "error.out_of_range"),
    ({"web_port": "80"}, "web_port", "error.type_int"),
    ({"login_enabled": 1}, "login_enabled", "error.type_bool"),
    ({"language": "es"}, "language", "error.invalid"),
    ({"nope": 1}, "nope", "error.unknown_setting"),
    ({"camera_wifi_password": "short"}, "camera_wifi_password", "error.wifi_password_length"),
    ({"nas_enabled": True}, "nas_target", "error.required"),
    ({"login_enabled": True}, "login_enabled", "error.password_not_set"),
    ({"nas_target": "-e sh"}, "nas_target", "error.invalid"),
    ({"mqtt_topic": "a b"}, "mqtt_topic", "error.invalid_topic"),
])
def test_config_validation(tmp_path, change, key, msg):
    c = cfgmod.Config(str(tmp_path / "c.json"))
    with pytest.raises(cfgmod.ConfigError) as ei:
        c.update(change)
    assert ei.value.errors[key] == msg
    assert not (tmp_path / "c.json").exists()


def test_config_broken_file_falls_back(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    assert cfgmod.Config(str(p)).get("web_port") == 8080
    p.write_text(json.dumps({"web_port": "bad", "min_free_mb": 7}))
    c = cfgmod.Config(str(p))
    assert c.get("web_port") == 8080 and c.get("min_free_mb") == 7


def test_public_hides_secrets(tmp_path):
    c = cfgmod.Config(str(tmp_path / "c.json"))
    c.update({"nas_password": "secret"})
    pub = c.public()
    assert "nas_password" not in pub and pub["nas_password_set"] is True


# profiles -------------------------------------------------------------------
def rf(name, size=10, folder="/store_00010001/DCIM/100GOPRO"):
    return RemoteFile(folder, name, size)


def test_gopro_grouping():
    p = GoProProfile()
    groups = p.group([rf("GH010001.MP4", 100), rf("GH010001.THM", 1), rf("GL010001.LRV", 50),
                      rf("GH020001.MP4", 100), rf("GH020001.THM", 1), rf("GL020001.LRV", 50),
                      rf("GOPR0002.JPG", 5), rf("GOPR0002.GPR", 20)])
    by_primary = {g.primary.name: sorted(f.name for f in g.companions) for g in groups}
    assert by_primary == {
        "GH010001.MP4": ["GH010001.THM", "GL010001.LRV"],
        "GH020001.MP4": ["GH020001.THM", "GL020001.LRV"],
        "GOPR0002.GPR": ["GOPR0002.JPG"],
    }
    assert p.recording_key("GH020001.MP4") == "0001" == p.recording_key("GH010001.MP4")
    assert p.chapter("GH020001.MP4") == 2 and p.chapter("GOPR0001.MP4") == 0


def test_hero4_style_names():
    p = GoProProfile()
    groups = p.group([rf("GOPR0001.MP4", 100), rf("GOPR0001.LRV", 10), rf("GOPR0001.THM", 1),
                      rf("GP010001.MP4", 100)])
    assert {g.primary.name for g in groups} == {"GOPR0001.MP4", "GP010001.MP4"}
    assert p.recording_key("GP010001.MP4") == "0001"


def test_profile_selection():
    gopro = DeviceInfo(port="usb:001,002", vendor_id="2672", manufacturer="GoPro")
    phone = DeviceInfo(port="usb:001,003", vendor_id="18d1", manufacturer="Google")
    assert profile_for(gopro).id == "gopro"
    assert profile_for(phone).id == "generic"


def test_preview_names():
    thumbs, proxies = preview_names("GH010001_1.MP4")
    assert "GH010001_1.THM" in thumbs and "GL010001_1.LRV" in proxies
    thumbs, proxies = preview_names("GOPR0001.MP4")
    assert "GOPR0001.LRV" in proxies


def test_generic_only_dcim(tmp_path):
    from footage_offload.camera.simulated import SimulatedBackend
    (tmp_path / "phone" / "DCIM" / "Camera").mkdir(parents=True)
    (tmp_path / "phone" / "Download").mkdir(parents=True)
    be = SimulatedBackend(str(tmp_path))
    cam = be.open(be.device("phone"))
    folders = GenericProfile().media_folders(cam)
    assert folders == ["/store_00010001/DCIM", "/store_00010001/DCIM/Camera"]


# stick ----------------------------------------------------------------------
def test_run_folder_names():
    assert is_run_folder("2026-11-03_21-47") and is_run_folder("2026-11-03_21-47_2")
    assert not is_run_folder("2026-11-03 21:47") and not is_run_folder("DCIM")
    assert ":" not in run_folder_name()


def test_ensure_space_raises_without_candidates(tmp_path, monkeypatch):
    from footage_offload.stick import StickFull
    s = Stick(str(tmp_path))
    monkeypatch.setattr(s, "free_bytes", lambda: 10)
    with pytest.raises(StickFull):
        s.ensure_space(100, 0)


# state ----------------------------------------------------------------------
def test_interrupted_runs_marked_on_start(tmp_path):
    from footage_offload.state import State
    info = DeviceInfo(port="x", serial="S1", model="M")
    st = State(str(tmp_path / "s.db"))
    st.start_run(info, "gopro")
    st.close()
    st = State(str(tmp_path / "s.db"))
    (run,) = st.runs()
    assert run["result"] == "error" and run["error_id"] == "error.interrupted"


# service --------------------------------------------------------------------
class FakeHelper:
    def __init__(self):
        self.calls = []

    def call(self, action, **args):
        self.calls.append((action, args))
        return {}


class FakeStickManager:
    def __init__(self, service, stick):
        self._stick = stick
        self.refreshed = 0

    def start(self):
        pass

    def stop(self):
        pass

    def stick(self):
        return self._stick

    def refresh(self):
        self.refreshed += 1


def make_service(env, stick=True):
    helper = FakeHelper()
    svc = Service(env.cfg, env.backend, helper,
                  lambda s: FakeStickManager(s, env.stick if stick else None),
                  status=env.status, state=env.state)
    return svc, helper


def test_service_runs_and_deauthorises(env):
    env.add("GH010001.MP4", 10_000)
    svc, helper = make_service(env)
    svc.start()
    try:
        info = env.info.__class__(**{**env.info.__dict__, "sysfs_path": "/sys/bus/usb/devices/1-1"})
        svc.camera_added(info)
        assert wait_until(lambda: helper.calls, 5)
        assert helper.calls == [("deauthorize_usb", {"sysfs_path": "/sys/bus/usb/devices/1-1"})]
        assert env.state.runs()[0]["files"] == 1
    finally:
        svc.stop()


def test_service_without_stick_touches_nothing(env):
    env.add("GH010001.MP4", 10_000)
    svc, helper = make_service(env, stick=False)
    svc.start()
    try:
        svc.camera_added(env.info)
        assert wait_until(lambda: env.status.snapshot()["state"] == "error", 5)
        assert env.status.snapshot()["error"]["id"] == "error.no_stick"
        assert helper.calls == []
        assert (env.camdir / "GH010001.MP4").exists()
    finally:
        svc.stop()


def test_reenumerated_empty_camera_not_in_history(env):
    svc, helper = make_service(env)
    svc.start()
    try:
        info = env.info.__class__(**{**env.info.__dict__, "sysfs_path": "/sys/bus/usb/devices/1-1"})
        svc.camera_added(info)
        assert wait_until(lambda: helper.calls, 5)
        assert env.state.runs() == []
    finally:
        svc.stop()
