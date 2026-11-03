"""The real libgphoto2 backend, run against libgphoto2's "Directory Browse"
driver on a temporary directory. Skipped where libgphoto2 is not installed."""
import os
import time

import pytest

from footage_offload.camera import gphoto
from footage_offload.camera.base import CameraError, CameraGone, DeviceInfo, NotSupported

pytestmark = pytest.mark.skipif(not gphoto.available(), reason="libgphoto2 not loadable")

STORE = "store_00010001"
FOLDER = f"/{STORE}/DCIM/100GOPRO"


@pytest.fixture(scope="module")
def backend():
    return gphoto.GphotoBackend()


@pytest.fixture
def camdir(tmp_path):
    d = tmp_path / "cam" / STORE / "DCIM" / "100GOPRO"
    d.mkdir(parents=True)
    return d


def info_for(tmp_path):
    return DeviceInfo(port=f"disk:{tmp_path / 'cam'}", manufacturer="GoPro", model="HERO6 Black",
                      serial="DIRTEST", vendor_id="2672", product_id="0037")


def test_list_and_sizes(backend, tmp_path, camdir):
    (camdir / "GH010001.MP4").write_bytes(os.urandom(300_001))
    (camdir / "GH010001.THM").write_bytes(b"thumb")
    (camdir.parent / "101GOPRO").mkdir()
    with backend.open(info_for(tmp_path)) as cam:
        assert cam.list_folders("/") == [STORE]
        assert cam.list_folders(f"/{STORE}") == ["DCIM"]
        assert cam.list_folders(f"/{STORE}/DCIM") == ["100GOPRO", "101GOPRO"]
        files = cam.list_files(FOLDER)
        assert [(f.name, f.size) for f in files] == [("GH010001.MP4", 300_001),
                                                     ("GH010001.THM", 5)]
        assert all(f.folder == FOLDER for f in files)
        caps = cam.capabilities()
        assert caps.can_delete


def test_whole_read_streams_into_fd(backend, tmp_path, camdir):
    data = os.urandom(1_234_567)
    (camdir / "GH010001.MP4").write_bytes(data)
    out = tmp_path / "out.bin"
    seen = []
    with backend.open(info_for(tmp_path)) as cam:
        remote = cam.list_files(FOLDER)[0]
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            n = cam.read_to_fd(remote, fd, 65536, seen.append, "whole")
            os.fstat(fd)               # our descriptor must still be open
        finally:
            os.close(fd)
    assert n == len(data)
    assert out.read_bytes() == data
    assert seen and seen[-1] == len(data)


def test_partial_and_auto(backend, tmp_path, camdir):
    """The directory driver has no partial reads: "partial" raises
    NotSupported, "auto" falls back to a whole-file read."""
    data = os.urandom(200_000)
    (camdir / "GOPR0001.JPG").write_bytes(data)
    out = tmp_path / "out.bin"
    with backend.open(info_for(tmp_path)) as cam:
        remote = cam.list_files(FOLDER)[0]
        fd = os.open(out, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            try:
                n = cam.read_to_fd(remote, fd, 65536, lambda n: None, "partial")
                partial_supported = True
            except NotSupported:
                partial_supported = False
                assert not cam.capabilities().partial_read
            if partial_supported:          # newer libgphoto2 may add it
                assert n == len(data)
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
            n = cam.read_to_fd(remote, fd, 65536, lambda n: None, "auto")
        finally:
            os.close(fd)
    assert n == len(data)
    assert out.read_bytes() == data


def test_delete(backend, tmp_path, camdir):
    (camdir / "GH010001.MP4").write_bytes(b"x" * 10)
    (camdir / "GH010002.MP4").write_bytes(b"y" * 10)
    with backend.open(info_for(tmp_path)) as cam:
        files = cam.list_files(FOLDER)
        cam.delete(files[0])
        assert [f.name for f in cam.list_files(FOLDER)] == ["GH010002.MP4"]
    assert not (camdir / "GH010001.MP4").exists()


def test_missing_file_is_camera_error(backend, tmp_path, camdir):
    from footage_offload.camera.base import RemoteFile
    with backend.open(info_for(tmp_path)) as cam:
        with pytest.raises(CameraError) as exc:
            cam.delete(RemoteFile(FOLDER, "NOPE.MP4", 1))
        assert not isinstance(exc.value, CameraGone)


def test_vanished_directory_is_camera_gone(backend, tmp_path, camdir):
    import shutil
    (camdir / "GH010001.MP4").write_bytes(b"x")
    with backend.open(info_for(tmp_path)) as cam:
        shutil.rmtree(tmp_path / "cam")
        with pytest.raises(CameraGone):
            cam.list_files(FOLDER)
        with pytest.raises(CameraGone):      # sticky once gone
            cam.list_folders("/")


def test_clock_and_summary_do_not_fail(backend, tmp_path, camdir):
    with backend.open(info_for(tmp_path)) as cam:
        assert cam.get_clock() is None
        with pytest.raises(NotSupported):
            cam.set_clock(time.time())
        assert isinstance(cam.summary(), str)


def test_close_is_idempotent(backend, tmp_path, camdir):
    cam = backend.open(info_for(tmp_path))
    cam.close()
    cam.close()
    with pytest.raises(CameraGone):
        cam.list_folders("/")


def test_full_run_through_transfer_engine(backend, tmp_path, camdir, cfg):
    """A complete run with transfer_mode "auto": copy, verify, delete."""
    from footage_offload.paths import MARKER_NAME
    from footage_offload.state import State
    from footage_offload.status import StatusBus
    from footage_offload.stick import Stick
    from footage_offload.transfer import TransferRun

    # The directory driver only lists files of media types it recognises
    # (it hides .LRV, for instance); a real PTP camera lists every object.
    payload = {"GH010001.MP4": os.urandom(700_000), "GH010001.THM": os.urandom(3_000),
               "GOPR0002.JPG": os.urandom(9_000), "GOPR0003.JPG": os.urandom(1)}
    for name, data in payload.items():
        (camdir / name).write_bytes(data)
    stickdir = tmp_path / "stick"
    stickdir.mkdir()
    (stickdir / MARKER_NAME).write_text("")
    cfg["transfer_mode"] = "auto"
    info = info_for(tmp_path)
    with backend.open(info) as cam:
        run = TransferRun(cam, info, Stick(str(stickdir)), cfg, StatusBus(min_interval=0),
                          State(str(tmp_path / "state.db")), sleep=lambda s: None)
        result = run.execute()
    assert result.result == "ok", result
    assert result.files == 4 and result.files_left == 0 and result.deleted == 4
    assert os.listdir(camdir) == []
    runs = [d for d in os.listdir(stickdir) if not d.startswith(".")]
    assert len(runs) == 1
    for name, data in payload.items():
        assert (stickdir / runs[0] / name).read_bytes() == data


# --- pure helpers, no libgphoto2 needed -------------------------------------------

def test_device_present_usb(tmp_path):
    node = tmp_path / "1-1.2"
    node.mkdir()
    (node / "busnum").write_text("1\n")
    (node / "devnum").write_text("5\n")
    info = DeviceInfo(port="usb:001,005", sysfs_path=str(node))
    assert gphoto.device_present(info)
    (node / "devnum").write_text("9\n")          # replugged: new device number
    assert not gphoto.device_present(info)
    node2 = tmp_path / "gone"
    assert not gphoto.device_present(DeviceInfo(port="usb:001,005", sysfs_path=str(node2)))
    assert gphoto.device_present(DeviceInfo(port="usb:001,005"))   # cannot tell


def test_parse_text_clock():
    ts = time.mktime(time.strptime("2026-11-03 21:47:00", "%Y-%m-%d %H:%M:%S"))
    assert gphoto.parse_text_clock("2026-11-03 21:47:00") == ts
    assert gphoto.parse_text_clock("20261103T214700") == ts
    assert gphoto.parse_text_clock("1793742420") == 1793742420.0
    assert gphoto.parse_text_clock("garbage") is None
    assert gphoto.parse_text_clock("") is None


class _FakeBackend:
    gone_recheck_s = 0

    def last_error(self, ctx):
        return "detail"


def _fake_camera(info):
    cam = gphoto.GphotoCamera.__new__(gphoto.GphotoCamera)
    cam.backend = _FakeBackend()
    cam.info = info
    cam._ctx = None
    cam._gone = False
    return cam


def test_error_mapping(tmp_path):
    node = tmp_path / "1-1"
    node.mkdir()
    present = DeviceInfo(port="usb:001,002", sysfs_path=str(node))
    absent = DeviceInfo(port="usb:001,002", sysfs_path=str(tmp_path / "missing"))

    with pytest.raises(NotSupported):
        _fake_camera(present)._raise(gphoto.GP_ERROR_NOT_SUPPORTED, "x")
    with pytest.raises(CameraGone):
        _fake_camera(present)._raise(gphoto.GP_ERROR_IO_USB_FIND, "x")
    for rc in (gphoto.GP_ERROR_IO, gphoto.GP_ERROR_IO_READ, gphoto.GP_ERROR_TIMEOUT,
               gphoto.GP_ERROR_CAMERA_BUSY, gphoto.GP_ERROR_IO_USB_CLAIM):
        with pytest.raises(CameraError) as exc:
            _fake_camera(present)._raise(rc, "x")
        assert not isinstance(exc.value, CameraGone)
        with pytest.raises(CameraGone):
            _fake_camera(absent)._raise(rc, "x")
    with pytest.raises(CameraError) as exc:
        _fake_camera(present)._raise(gphoto.GP_ERROR_IO, "read")
    assert "detail" in str(exc.value)
