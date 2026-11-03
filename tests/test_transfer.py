"""Stage 1 logic tests: the whole pipeline against a simulated camera with injected faults."""
import os
import stat

import pytest

from footage_offload.stick import Stick


def run_dirs(e):
    return sorted(p for p in os.listdir(e.stickdir) if not p.startswith("."))


def stick_files(e):
    out = {}
    for d in run_dirs(e):
        for n in os.listdir(e.stickdir / d):
            out[f"{d}/{n}"] = os.path.getsize(e.stickdir / d / n)
    return out


def camera_files(e):
    out = []
    for root, _, files in os.walk(e.cams / "cam"):
        out.extend(files)
    return sorted(out)


def test_happy_path_copies_verifies_and_wipes(env):
    env.add("GH010001.MP4", 300_000)
    env.add("GH010001.THM", 2_000)
    env.add("GL010001.LRV", 40_000)
    env.add("GOPR0002.JPG", 5_000)
    r = env.run()
    assert r.result == "ok" and r.files == 4 and r.files_left == 0
    assert camera_files(env) == []
    files = stick_files(env)
    assert len(run_dirs(env)) == 1
    assert sorted(os.path.basename(k) for k in files) == [
        "GH010001.MP4", "GH010001.THM", "GL010001.LRV", "GOPR0002.JPG"]
    runs = env.state.runs()
    assert runs[0]["files"] == 4 and runs[0]["result"] == "ok"
    assert env.state.devices()[0]["serial"] == env.info.serial
    assert env.status.snapshot()["state"] == "done"


def test_content_identical(env):
    p = env.add("GH010001.MP4", 1_000_003)
    data = p.read_bytes()
    env.run()
    (d,) = run_dirs(env)
    assert (env.stickdir / d / "GH010001.MP4").read_bytes() == data


def test_whole_file_fallback_when_partial_reads_unsupported(env):
    env.add("GH010001.MP4", 100_000)
    env.faults().no_partial_read = True
    r = env.run()
    assert r.result == "ok" and r.files == 1


def test_empty_camera_creates_no_folder_and_no_history(env):
    r = env.run()
    assert r.empty and r.result == "ok"
    assert run_dirs(env) == []
    assert env.state.runs() == []


def test_unplug_mid_transfer_loses_nothing(env):
    env.add("GH010001.MP4", 200_000)
    env.add("GH010002.MP4", 200_000)
    env.faults().unplug_after_bytes = 300_000
    r = env.run()
    assert r.error_id == "error.camera_gone"
    assert r.result == "partial"
    # First file copied and deleted, second still on the camera, no temp files left.
    assert camera_files(env) == ["GH010002.MP4"]
    names = [os.path.basename(k) for k in stick_files(env)]
    assert names == ["GH010001.MP4"]
    assert env.state.runs()[0]["result"] == "partial"


def test_unplug_during_first_file_leaves_no_folder(env):
    env.add("GH010001.MP4", 200_000)
    env.faults().unplug_after_bytes = 1000
    r = env.run()
    assert r.error_id == "error.camera_gone" and r.result == "error"
    assert run_dirs(env) == []
    assert camera_files(env) == ["GH010001.MP4"]


def test_size_mismatch_never_deletes(env):
    env.add("GH010001.MP4", 50_000)
    env.add("GOPR0002.JPG", 5_000)
    env.faults().size_mismatch.add("GH010001.MP4")
    r = env.run()
    assert r.result == "partial"
    assert camera_files(env) == ["GH010001.MP4"]
    assert [e["id"] for e in r.errors] == ["error.copy_failed"]
    assert all("GH010001" not in k for k in stick_files(env))


def test_transient_read_errors_are_retried(env):
    env.add("GH010001.MP4", 50_000)
    env.faults().read_failures["GH010001.MP4"] = 3   # three retries allowed
    r = env.run()
    assert r.result == "ok"


def test_retry_waits_increase(env):
    from footage_offload.transfer import TransferRun
    env.add("GH010001.MP4", 10_000)
    env.faults().read_failures["GH010001.MP4"] = 5
    env.cfg["retry_base_delay_s"] = 2.0
    waits = []
    r = TransferRun(env.backend.open(env.info), env.info, env.stick, env.cfg, env.status,
                    env.state, sleep=waits.append).execute()
    assert waits == [2.0, 4.0, 8.0]
    assert r.result == "partial" and camera_files(env) == ["GH010001.MP4"]


def test_companion_failure_keeps_whole_group_on_camera(env):
    env.add("GH010001.MP4", 50_000)
    env.add("GH010001.THM", 1_000)
    env.add("GL010001.LRV", 5_000)
    env.faults().size_mismatch.add("GL010001.LRV")
    env.run()
    assert camera_files(env) == ["GH010001.MP4", "GH010001.THM", "GL010001.LRV"]


def test_delete_failure_reported_file_stays(env):
    env.add("GH010001.MP4", 50_000)
    env.faults().delete_failures["GH010001.MP4"] = 999
    r = env.run()
    assert r.result == "partial"
    assert [e["id"] for e in r.errors] == ["error.delete_failed"]
    assert camera_files(env) == ["GH010001.MP4"]
    assert len(stick_files(env)) == 1          # copy exists: worst case the file exists twice


def test_no_marker_means_no_writes_and_no_deletes(env):
    from footage_offload.paths import MARKER_NAME
    os.unlink(env.stickdir / MARKER_NAME)
    env.add("GH010001.MP4", 50_000)
    r = env.run()
    assert r.error_id == "error.stick"
    assert camera_files(env) == ["GH010001.MP4"]
    assert os.listdir(env.stickdir) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_read_only_stick_stops_run(env):
    env.add("GH010001.MP4", 50_000)
    os.chmod(env.stickdir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        r = env.run()
    finally:
        os.chmod(env.stickdir, 0o755)
    assert r.error_id == "error.stick"
    assert camera_files(env) == ["GH010001.MP4"]


def test_read_only_stick_via_statvfs(env, monkeypatch):
    env.add("GH010001.MP4", 50_000)
    real = os.statvfs

    class RO:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, k):
            return getattr(self._st, k)

        f_flag = os.ST_RDONLY

    monkeypatch.setattr(os, "statvfs", lambda p: RO(real(p)))
    r = env.run()
    assert r.error_id == "error.stick"
    assert camera_files(env) == ["GH010001.MP4"]


def test_stick_io_error_during_write_stops_run(env, monkeypatch):
    import errno
    env.add("GH010001.MP4", 50_000)
    real_fsync = os.fsync

    def bad_fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "Input/output error")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", bad_fsync)
    r = env.run()
    assert r.error_id == "error.stick"
    assert camera_files(env) == ["GH010001.MP4"]


def _fake_space(monkeypatch, stick, capacity):
    """Free space = capacity - bytes in run folders."""
    def free():
        used = sum(r.size for r in stick.run_folders())
        return capacity - used
    monkeypatch.setattr(stick, "free_bytes", free)


def test_rotation_deletes_oldest_previous_runs(env, monkeypatch):
    for name in ("2026-01-01_10-00", "2026-02-01_10-00"):
        d = env.stickdir / name
        d.mkdir()
        (d / "OLD.MP4").write_bytes(b"x" * 60_000)
    (env.stickdir / "my-own-folder").mkdir()          # never touched
    _fake_space(monkeypatch, env.stick, 150_000)
    env.add("GH010001.MP4", 50_000)
    r = env.run()
    assert r.result == "ok"
    dirs = run_dirs(env)
    assert "2026-01-01_10-00" not in dirs and "2026-02-01_10-00" in dirs
    assert "my-own-folder" in dirs
    assert any(w["id"] == "warning.rotation_deleted" for w in env.status.snapshot()["warnings"])


def test_stick_full_never_deletes_current_run_and_stops(env, monkeypatch):
    _fake_space(monkeypatch, env.stick, 120_000)
    env.add("GH010001.MP4", 50_000)
    env.add("GH010002.MP4", 50_000)
    env.add("GH010003.MP4", 50_000)
    r = env.run()
    assert r.error_id == "error.stick_full"
    assert r.files == 2 and r.files_left == 1
    assert camera_files(env) == ["GH010003.MP4"]
    assert len(stick_files(env)) == 2


def test_min_free_space_respected(env, monkeypatch):
    _fake_space(monkeypatch, env.stick, 3 * 1024 * 1024)
    env.cfg["min_free_mb"] = 2
    env.add("GH010001.MP4", 2 * 1024 * 1024)
    r = env.run()
    assert r.error_id == "error.stick_full"
    assert camera_files(env) == ["GH010001.MP4"]


def test_collisions_get_suffix_for_whole_group(env):
    env.add("GH010001.MP4", 10_000)
    env.add("GH010001.THM", 100)
    env.add("GL010001.LRV", 1_000)
    env.add("GH010001.MP4", 20_000, folder="DCIM/101GOPRO")
    env.add("GH010001.THM", 200, folder="DCIM/101GOPRO")
    env.add("GL010001.LRV", 2_000, folder="DCIM/101GOPRO")
    r = env.run()
    assert r.result == "ok"
    names = sorted(os.path.basename(k) for k in stick_files(env))
    assert names == ["GH010001.MP4", "GH010001.THM", "GH010001_1.MP4", "GH010001_1.THM",
                     "GL010001.LRV", "GL010001_1.LRV"]


def test_two_runs_same_minute_get_separate_folders(env):
    env.add("GH010001.MP4", 1_000)
    env.run()
    env.add("GH010001.MP4", 2_000)
    env.run()
    assert len(run_dirs(env)) == 2
    assert len(stick_files(env)) == 2


def test_cleanup_removes_part_files_only(env):
    d = env.stickdir / "2026-01-01_10-00"
    d.mkdir()
    (d / ".GH010001.MP4.part").write_bytes(b"half")
    (d / "GH010002.MP4").write_bytes(b"full")
    (env.stickdir / "2026-01-02_10-00").mkdir()          # empty run folder
    other = env.stickdir / "notes"
    other.mkdir()
    (other / ".x.part").write_bytes(b"")
    assert env.stick.cleanup() == 1
    assert os.listdir(d) == ["GH010002.MP4"]
    assert not (env.stickdir / "2026-01-02_10-00").exists()
    assert (other / ".x.part").exists()


def test_files_outside_dcim_are_ignored(env):
    env.add("GH010001.MP4", 1_000)
    env.add("settings.json", 10, folder="MISC")
    r = env.run()
    assert r.files == 1
    assert "settings.json" in camera_files(env)


def test_resolve_rejects_traversal(env):
    s = Stick(str(env.stickdir))
    assert s.resolve("../etc") is None
    assert s.resolve("2026-01-01_10-00", "../../x") is None
    assert s.resolve("2026-01-01_10-00", ".hidden.part") is None
    assert s.resolve("2026-01-01_10-00", "GH010001.MP4").endswith("GH010001.MP4")


def test_progress_reported(env):
    env.add("GH010001.MP4", 300_000)
    seen = []
    env.status.subscribe(lambda s: seen.append((s["state"], s["bytes_done"])))
    env.run()
    assert any(st == "transferring" and b > 0 for st, b in seen)
    assert env.status.snapshot()["bytes_done"] == 300_000


def _dir_fsync(monkeypatch, err):
    """Make fsync fail with `err` on directories only."""
    real = os.fsync

    def fake(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(err, os.strerror(err))
        return real(fd)

    monkeypatch.setattr(os, "fsync", fake)


def test_directory_sync_io_error_keeps_camera_file(env, monkeypatch):
    import errno
    env.add("GH010001.MP4", 50_000)
    _dir_fsync(monkeypatch, errno.EIO)
    r = env.run()
    assert r.error_id == "error.stick"
    assert camera_files(env) == ["GH010001.MP4"]


def test_directory_sync_unsupported_is_ignored(env, monkeypatch):
    import errno
    env.add("GH010001.MP4", 50_000)
    _dir_fsync(monkeypatch, errno.EINVAL)
    r = env.run()
    assert r.result == "ok" and camera_files(env) == []


def test_storage_listing_error_is_retried(env):
    from footage_offload.camera.base import CameraError
    env.add("GH010001.MP4", 10_000)
    cam = env.backend.open(env.info)
    real = cam.list_folders
    fails = {"n": 2}

    def flaky(folder):
        if folder != "/" and fails["n"]:
            fails["n"] -= 1
            raise CameraError("busy")
        return real(folder)

    cam.list_folders = flaky
    from footage_offload.transfer import TransferRun
    r = TransferRun(cam, env.info, env.stick, env.cfg, env.status, env.state,
                    sleep=lambda s: None).execute()
    assert r.result == "ok" and r.files == 1


def test_unreadable_storage_is_an_error_not_an_empty_camera(env):
    from footage_offload.camera.base import CameraError
    env.add("GH010001.MP4", 10_000)
    cam = env.backend.open(env.info)
    real = cam.list_folders

    def broken(folder):
        if folder != "/":
            raise CameraError("storage not readable")
        return real(folder)

    cam.list_folders = broken
    from footage_offload.transfer import TransferRun
    r = TransferRun(cam, env.info, env.stick, env.cfg, env.status, env.state,
                    sleep=lambda s: None).execute()
    assert not r.empty and r.result == "error" and r.error_id == "error.camera"
    assert camera_files(env) == ["GH010001.MP4"]
