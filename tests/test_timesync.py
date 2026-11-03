import time
from types import SimpleNamespace

from footage_offload import timesync
from footage_offload.camera.base import RemoteFile
from footage_offload.camera.simulated import SimulatedBackend
from footage_offload.status import StatusBus
from footage_offload.timesync import ClockSync, gopro_date_time_param


class Helper:
    def __init__(self, fail_connect=False):
        self.calls = []
        self.fail_connect = fail_connect

    def call(self, action, **args):
        self.calls.append((action, args))
        if self.fail_connect and action == "wifi_connect":
            raise RuntimeError("no wifi")
        return {}


def run_returning(out, rc=0):
    return lambda argv, **kw: SimpleNamespace(returncode=rc, stdout=out, stderr="")


def make(cfg, tmp_path, synced="yes", helper=None, http_get=None, **cfgkw):
    cfg.update(cfgkw)
    status = StatusBus(min_interval=0)
    helper = helper or Helper()
    cs = ClockSync(cfg, status, helper, http_get=http_get, run=run_returning(synced + "\n"),
                   sleep=lambda s: None)
    backend = SimulatedBackend(str(tmp_path))
    (tmp_path / "cam").mkdir(exist_ok=True)
    info = backend.device("cam")
    return cs, status, helper, backend, info


def ids(status):
    return {w["id"]: w["params"] for w in status.snapshot()["warnings"]}


def test_param_format():
    ts = time.mktime((2026, 11, 3, 21, 47, 5, 0, 0, -1))
    assert gopro_date_time_param(ts) == "%1a%0b%03%15%2f%05"


def test_ntp_values():
    for out, rc, exp in (("yes\n", 0, True), ("no\n", 0, False), ("", 1, None)):
        cs = ClockSync({}, StatusBus(), None, run=run_returning(out, rc))
        assert cs.ntp_synchronised() is exp
    def boom(*a, **k):
        raise FileNotFoundError
    assert ClockSync({}, StatusBus(), None, run=boom).ntp_synchronised() is None


def test_ntp_warning_toggles(cfg, tmp_path):
    cs, status, *_ = make(cfg, tmp_path, synced="no")
    cs.check_ntp()
    assert "warning.ntp_not_synchronised" in ids(status)
    cs._run = run_returning("yes\n")
    cs.check_ntp()
    assert "warning.ntp_not_synchronised" not in ids(status)
    assert status.snapshot()["clock"] == {"ntp_synchronised": True}


def test_usb_set_clock(cfg, tmp_path):
    cs, status, helper, backend, info = make(cfg, tmp_path)
    cam = backend.open(info)
    cs.before_run(cam, info, [])
    assert cam.clock_set_to is not None
    assert ids(status)["warning.camera_clock_set"] == {"method": "usb"}
    assert helper.calls == []


def test_wrong_camera_clock_warns(cfg, tmp_path):
    cs, status, helper, backend, info = make(cfg, tmp_path)
    backend.faults[info.port] = __import__(
        "footage_offload.camera.simulated", fromlist=["Faults"]).Faults(
        clock_offset_s=-3 * 86400, clock_settable=False)
    cam = backend.open(info)
    cs.before_run(cam, info, [])
    w = ids(status)
    assert w["warning.camera_clock_wrong"]["difference_s"] < -3 * 86400 + 5
    assert "camera_time" in w["warning.camera_clock_wrong"]


def test_no_set_when_not_synced(cfg, tmp_path):
    cs, status, helper, backend, info = make(cfg, tmp_path, synced="no")
    cam = backend.open(info)
    cs.before_run(cam, info, [])
    assert cam.clock_set_to is None
    assert "warning.camera_clock_set" not in ids(status)


def test_future_mtime_and_old_year_hints(cfg, tmp_path):
    cs, status, helper, backend, info = make(cfg, tmp_path)
    cam = SimpleNamespace(get_clock=lambda: None, set_clock=lambda t: None)
    f = RemoteFile("/x", "a.mp4", 1, mtime=time.time() + 30 * 86400)
    cs._check_camera_clock(cam, [f], True)
    assert "warning.camera_clock_wrong" in ids(status)
    status.clear_warning("warning.camera_clock_wrong")
    cs._check_camera_clock(cam, [RemoteFile("/x", "a.mp4", 1, mtime=1.0e9)], False)
    assert "warning.camera_clock_wrong" in ids(status)


def test_wifi_fallback_success(cfg, tmp_path):
    urls = []
    cs, status, helper, backend, info = make(
        cfg, tmp_path, http_get=urls.append, camera_wifi_enabled=True,
        camera_wifi_ssid="GP1", camera_wifi_password="password1")
    backend.faults[info.port] = __import__("footage_offload.camera.simulated",
                                           fromlist=["Faults"]).Faults(clock_settable=False)
    cs.before_run(backend.open(info), info, [])
    cs.join_wifi(5)
    assert urls and urls[0].startswith("http://10.5.5.9/gp/gpControl/command/setup/date_time?p=%")
    assert [c[0] for c in helper.calls] == ["wifi_connect", "wifi_disconnect"]
    assert helper.calls[0][1] == {"ssid": "GP1", "password": "password1"}
    assert ids(status)["warning.camera_clock_set"] == {"method": "wifi"}


def test_wifi_fallback_retries_then_fails(cfg, tmp_path):
    n = []
    def get(url):
        n.append(1)
        raise OSError("unreachable")
    cs, status, helper, backend, info = make(
        cfg, tmp_path, http_get=get, camera_wifi_enabled=True, camera_wifi_ssid="GP1")
    backend.faults[info.port] = __import__("footage_offload.camera.simulated",
                                           fromlist=["Faults"]).Faults(clock_settable=False)
    cs.before_run(backend.open(info), info, [])
    cs.join_wifi(5)
    assert len(n) > 3
    assert helper.calls[-1][0] == "wifi_disconnect"
    assert "unreachable" in ids(status)["warning.camera_clock_set_failed"]["detail"]


def test_wifi_connect_failure_never_raises(cfg, tmp_path):
    cs, status, helper, backend, info = make(
        cfg, tmp_path, helper=Helper(fail_connect=True), camera_wifi_enabled=True,
        camera_wifi_ssid="GP1")
    backend.faults[info.port] = __import__("footage_offload.camera.simulated",
                                           fromlist=["Faults"]).Faults(clock_settable=False)
    cs.before_run(backend.open(info), info, [])
    cs.join_wifi(5)
    assert "warning.camera_clock_set_failed" in ids(status)
    assert [c[0] for c in helper.calls] == ["wifi_connect"]


def test_not_supported_without_wifi(cfg, tmp_path):
    cs, status, helper, backend, info = make(cfg, tmp_path)
    backend.faults[info.port] = __import__("footage_offload.camera.simulated",
                                           fromlist=["Faults"]).Faults(clock_settable=False)
    cs.before_run(backend.open(info), info, [])
    assert "warning.camera_clock_set_failed" in ids(status)
    assert helper.calls == []


def test_is_gopro():
    assert timesync.is_gopro(SimpleNamespace(vendor_id="2672", manufacturer=""))
    assert timesync.is_gopro(SimpleNamespace(vendor_id="", manufacturer="GoPro"))
    assert not timesync.is_gopro(SimpleNamespace(vendor_id="04a9", manufacturer="Canon"))
