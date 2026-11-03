"""StickManager with a fake block-device source and a recording helper."""

import pytest

from footage_offload.helper_client import RecordingHelper
from footage_offload.paths import MARKER_NAME
from footage_offload.status import StatusBus
from footage_offload.stickmanager import (StickBusy, StickManager, StickManagerError,
                                          is_candidate, mount_source)


def props(dev="/dev/sda1", fstype="exfat", devtype="partition", bus="usb", label="FOOTAGE"):
    return {"SUBSYSTEM": "block", "DEVNAME": dev, "DEVTYPE": devtype, "ID_BUS": bus,
            "ID_FS_TYPE": fstype, "ID_FS_USAGE": "filesystem", "ID_FS_LABEL": label}


class FakeSource:
    def __init__(self, devices=()):
        self.devices = list(devices)
        self.callback = None
        self.stopped = False

    def list(self):
        return list(self.devices)

    def start(self, callback):
        self.callback = callback

    def stop(self):
        self.stopped = True

    def emit(self, action, p):
        self.callback(action, p)


class FakeService:
    def __init__(self, helper):
        self.helper = helper
        self.status = StatusBus(min_interval=0)
        self.is_busy = False
        self.changed = 0

    def busy(self):
        return self.is_busy

    def stick_changed(self):
        self.changed += 1


@pytest.fixture
def setup(tmp_path):
    mnt = tmp_path / "stick"
    mnt.mkdir()
    adopted = {"/dev/sda1"}

    def probe(device):
        return {"marker": device in adopted, "fstype": "exfat", "label": "FOOTAGE", "uuid": "1"}

    def mount(device):
        (mnt / MARKER_NAME).write_text("")
        return {"mounted": True, "fstype": "exfat"}

    helper = RecordingHelper({"probe_stick": probe, "mount_stick": mount,
                              "eject_stick": {"ejected": True, "method": "sysfs"}})
    service = FakeService(helper)
    source = FakeSource()

    def make(**kw):
        kw.setdefault("ismount", lambda p: False)
        kw.setdefault("mountinfo", str(tmp_path / "mountinfo"))
        m = StickManager(service, source=source, mount_point=str(mnt), **kw)
        service.stick_manager = m
        return m

    class S:
        pass

    s = S()
    s.mnt, s.helper, s.service, s.source, s.make, s.adopted = mnt, helper, service, source, make, adopted
    yield s


def test_is_candidate():
    assert is_candidate(props())
    assert is_candidate(props(dev="/dev/sdb", devtype="disk", fstype="vfat"))
    assert is_candidate(props(fstype="ext4"))
    assert not is_candidate(props(fstype="ntfs"))
    assert not is_candidate(props(fstype=""))
    assert not is_candidate(props(bus="ata"))
    assert not is_candidate(props(dev="/dev/mmcblk0p1"))
    assert not is_candidate(dict(props(), DEVTYPE="disk", ID_FS_TYPE=None))
    assert not is_candidate(dict(props(), ID_FS_USAGE="raid"))
    assert not is_candidate({})


def test_adopted_stick_is_mounted_cleaned_and_announced(setup):
    run = setup.mnt / "2026-11-03_21-47"
    run.mkdir()
    (run / "GH010001.MP4").write_bytes(b"x" * 10)
    (run / ".GH010002.MP4.part").write_bytes(b"half")
    setup.source.devices = [props(dev="/dev/sda", devtype="disk", fstype=""), props()]
    m = setup.make()
    assert m.stick() is None
    m.start()
    m.wait_idle()
    assert setup.helper.actions() == ["probe_stick", "mount_stick"]
    assert setup.helper.calls_of("mount_stick") == [{"device": "/dev/sda1"}]
    assert m.stick() is not None and m.stick().root == str(setup.mnt)
    assert not (run / ".GH010002.MP4.part").exists()          # Stick.cleanup ran
    assert setup.service.changed == 1
    st = setup.service.status.snapshot()["stick"]
    assert st["present"] and st["adopted"] and not st["ejected"]
    assert st["device"] == "/dev/sda1" and st["fstype"] == "exfat" and st["label"] == "FOOTAGE"
    assert st["runs"] == 1 and st["oldest"] == st["newest"] == "2026-11-03_21-47"
    assert st["total"] > 0 and st["free"] > 0 and st["read_only"] is False
    m.stop()
    assert setup.source.stopped


def test_unadopted_stick_warns_and_is_not_mounted(setup):
    m = setup.make()
    m.start()
    setup.source.emit("add", props(dev="/dev/sdb1"))
    m.wait_idle()
    assert setup.helper.actions() == ["probe_stick"]
    assert m.stick() is None
    snap = setup.service.status.snapshot()
    assert snap["warnings"] == [{"id": "warning.stick_unadopted", "params": {"device": "/dev/sdb1"}}]
    assert snap["stick"]["adopted"] is False and snap["stick"]["device"] == "/dev/sdb1"
    assert setup.service.changed == 0
    setup.source.emit("remove", props(dev="/dev/sdb1"))
    m.wait_idle()
    snap = setup.service.status.snapshot()
    assert snap["warnings"] == [] and snap["stick"] is None
    m.stop()


def test_rescan_after_adoption(setup):
    m = setup.make()
    m.start()
    setup.source.emit("add", props(dev="/dev/sdb1"))
    m.wait_idle()
    assert m.stick() is None
    setup.adopted.add("/dev/sdb1")
    m.rescan()
    m.wait_idle()
    assert m.stick() is not None
    assert setup.service.status.snapshot()["warnings"] == []
    m.stop()


def test_removal_without_eject(setup):
    m = setup.make()
    m.start()
    setup.source.emit("add", props())
    m.wait_idle()
    assert m.stick() is not None
    setup.source.emit("remove", props())
    m.wait_idle()
    assert m.stick() is None
    assert setup.service.status.snapshot()["stick"] is None
    m.stop()


def test_already_mounted_stick_at_start(setup, tmp_path):
    (setup.mnt / MARKER_NAME).write_text("")
    (tmp_path / "mountinfo").write_text(
        f"22 1 179:2 / / rw - ext4 /dev/mmcblk0p2 rw\n"
        f"41 22 8:1 / {setup.mnt} rw,nosuid - ext4 /dev/sda1 rw\n")
    setup.source.devices = [props(fstype="ext4", label="CAMSTICK")]
    m = setup.make(ismount=lambda p: p == str(setup.mnt))
    m.start()
    m.wait_idle()
    assert m.stick() is not None
    assert setup.helper.calls == []              # neither probed nor mounted again
    st = setup.service.status.snapshot()["stick"]
    assert st["device"] == "/dev/sda1" and st["fstype"] == "ext4" and st["label"] == "CAMSTICK"
    assert setup.service.changed == 1
    m.stop()


def test_mounted_dir_without_marker_is_not_adopted(setup):
    m = setup.make(ismount=lambda p: True)
    m.start()
    m.wait_idle()
    assert m.stick() is None
    m.stop()


def test_eject(setup):
    m = setup.make()
    m.start()
    setup.source.emit("add", props())
    m.wait_idle()
    m.eject()
    assert setup.helper.actions()[-1] == "eject_stick"
    assert m.stick() is None
    st = setup.service.status.snapshot()["stick"]
    assert st["ejected"] is True and st["present"] is False and st["device"] == "/dev/sda1"
    setup.source.emit("remove", props())          # the device goes away after power-off
    m.wait_idle()
    assert setup.service.status.snapshot()["stick"]["ejected"] is True
    with pytest.raises(StickManagerError) as exc:
        m.eject()
    assert exc.value.message_id == "error.no_stick"
    m.stop()


def test_eject_refused_while_busy(setup):
    m = setup.make()
    m.start()
    setup.source.emit("add", props())
    m.wait_idle()
    setup.service.is_busy = True
    with pytest.raises(StickBusy) as exc:
        m.eject()
    assert exc.value.message_id == "error.busy"
    assert "eject_stick" not in setup.helper.actions()
    assert m.stick() is not None
    m.stop()


def test_eject_failure_keeps_stick(setup):
    m = setup.make()
    m.start()
    setup.source.emit("add", props())
    m.wait_idle()
    setup.helper.fail("eject_stick", "target is busy")
    with pytest.raises(StickManagerError) as exc:
        m.eject()
    assert exc.value.message_id == "error.helper" and "busy" in exc.value.detail
    assert m.stick() is not None
    m.stop()


def test_helper_failures_are_survived(setup):
    setup.helper.fail("probe_stick", "blkid failed")
    m = setup.make()
    m.start()
    setup.source.emit("add", props())
    m.wait_idle()
    assert m.stick() is None
    setup.helper.set("probe_stick", {"marker": True, "fstype": "exfat"})
    setup.helper.fail("mount_stick", "mount failed")
    setup.source.emit("add", props())
    m.wait_idle()
    assert m.stick() is None and setup.service.changed == 0
    m.stop()


def test_second_adopted_stick_is_ignored(setup):
    setup.adopted.add("/dev/sdb1")
    m = setup.make()
    m.start()
    setup.source.emit("add", props())
    setup.source.emit("add", props(dev="/dev/sdb1"))
    m.wait_idle()
    assert setup.helper.calls_of("mount_stick") == [{"device": "/dev/sda1"}]
    m.stop()


def test_mount_source(tmp_path):
    p = tmp_path / "mi"
    p.write_text("41 22 8:1 / /media/a\\040b rw - vfat /dev/sda1 rw\n"
                 "42 41 8:17 / /media/a\\040b rw - exfat /dev/sdb1 rw\n")
    assert mount_source("/media/a b", str(p)) == ("/dev/sdb1", "exfat")
    assert mount_source("/elsewhere", str(p)) is None
    assert mount_source("/x", str(tmp_path / "missing")) is None
