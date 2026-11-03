from types import SimpleNamespace

from footage_offload import __version__
from footage_offload.status import StatusBus
from footage_offload.sysinfo import SysInfo, parse_throttled, parse_version


def runner(out, rc=0):
    return lambda argv, **kw: SimpleNamespace(returncode=rc, stdout=out, stderr="")


def ids(status):
    return {w["id"]: w["params"] for w in status.snapshot()["warnings"]}


def test_parse_throttled():
    t = parse_throttled("throttled=0x50005\n")
    assert t["undervoltage_now"] and t["undervoltage_occurred"]
    assert t["throttled_now"] and t["throttled_occurred"]
    assert not t["freq_capped_now"] and not t["soft_temp_limit_now"]
    assert not any(parse_throttled("throttled=0x0").values())
    assert parse_throttled("garbage") is None


def test_throttled_missing_vcgencmd(cfg):
    def boom(*a, **k):
        raise FileNotFoundError
    assert SysInfo(cfg, StatusBus(), run=boom).throttled() is None


def test_throttle_warnings(cfg):
    status = StatusBus(min_interval=0)
    si = SysInfo(cfg, status, run=runner("throttled=0x50000\n"))
    si.check_throttled()
    w = ids(status)
    assert "warning.undervoltage_occurred" in w and "warning.undervoltage_now" not in w
    si._run = runner("throttled=0x50005\n")
    si.check_throttled()
    assert "warning.undervoltage_now" in ids(status)
    si._run = runner("throttled=0x0\n")
    si.check_throttled()
    assert ids(status) == {}
    assert status.snapshot()["throttled"]["undervoltage_now"] is False


def test_version_compare():
    assert parse_version("v1.10.0") > parse_version("1.9.9")
    assert parse_version("nonsense") is None


def test_update_check_disabled(cfg):
    calls = []
    si = SysInfo(cfg, StatusBus(), fetch=lambda u, h: calls.append(u))
    assert si.check_update() is None and calls == []


def test_update_check(cfg):
    cfg["update_check_enabled"] = True
    status = StatusBus(min_interval=0)
    seen = []
    now = [1000.0]

    def fetch(url, headers):
        seen.append((url, headers))
        return {"tag_name": "v99.0.0"}

    si = SysInfo(cfg, status, fetch=fetch, clock=lambda: now[0])
    assert si.check_update() == "99.0.0"
    assert si.update_available()
    assert ids(status)["warning.update_available"] == {"version": "99.0.0"}
    assert "api.github.com/repos/sherrmann/FootageOffloadStation/releases/latest" in seen[0][0]
    assert seen[0][1]["User-Agent"] == f"footage-offload/{__version__}"
    si.check_update()
    assert len(seen) == 1                     # throttled to once a day
    now[0] += 25 * 3600
    si.check_update()
    assert len(seen) == 2


def test_update_not_newer_and_errors(cfg):
    cfg["update_check_enabled"] = True
    si = SysInfo(cfg, StatusBus(), fetch=lambda u, h: {"tag_name": f"v{__version__}"})
    si.check_update()
    assert not si.update_available()
    def boom(u, h):
        raise OSError("offline")
    si2 = SysInfo(cfg, StatusBus(), fetch=boom)
    assert si2.check_update() is None


def test_ntp_delegates(cfg):
    cs = SimpleNamespace(ntp_synchronised=lambda: True)
    assert SysInfo(cfg, StatusBus(), clock_sync=cs).ntp_synchronised() is True
    assert SysInfo(cfg, StatusBus()).ntp_synchronised() is None
