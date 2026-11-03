"""The transfer run: copy, verify, delete, rotate.

Safety rule: a file is deleted on the camera only after its copy on the
stick has been written to a temporary name, fsync'ed, compared with the
size the camera reports, and renamed into place. Companion files (LRV, THM)
are deleted together with their video and only when every file of the group
has been verified.
"""
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import status as st
from .camera.base import CameraError, CameraGone, NotSupported
from .profiles import FileGroup, profile_for
from .stick import Stick, StickError, StickFull, classify_os_error

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    run_id: Optional[int] = None
    run_folder: Optional[str] = None
    files: int = 0                 # files copied and verified
    bytes: int = 0
    files_left: int = 0            # files still on the camera
    deleted: int = 0
    result: str = "ok"             # ok | partial | error
    error_id: Optional[str] = None
    error_detail: Optional[str] = None
    errors: List[dict] = field(default_factory=list)
    empty: bool = False            # camera had nothing to copy


class _Abort(Exception):
    def __init__(self, error_id, detail):
        super().__init__(detail)
        self.error_id = error_id
        self.detail = detail


def lower_priority():
    """Low CPU and I/O priority for the calling thread (#9)."""
    tid = threading.get_native_id()
    try:
        os.setpriority(os.PRIO_PROCESS, tid, 10)
    except OSError:
        pass
    try:
        subprocess.run(["ionice", "-c", "2", "-n", "7", "-p", str(tid)],
                       check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


class _Speed:
    """Moving average over the last few seconds."""

    def __init__(self, window=5.0):
        self.window = window
        self.samples = []

    def add(self, total_bytes):
        now = time.monotonic()
        self.samples.append((now, total_bytes))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.pop(0)

    def rate(self):
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0


class TransferRun:
    """One run for one device. Call `execute()` in a worker thread."""

    def __init__(self, camera, info, stick: Stick, config, status: st.StatusBus, state,
                 on_start: Optional[Callable] = None, sleep=time.sleep):
        self.camera = camera
        self.info = info
        self.stick = stick
        self.config = config
        self.status = status
        self.state = state
        self.on_start = on_start       # hook(camera, info, files) for clock checks; never fatal
        self.sleep = sleep
        self.profile = profile_for(info)
        self.cancelled = threading.Event()
        self.result = RunResult()
        self._speed = _Speed()
        self._bytes_before_file = 0

    # retry helper -----------------------------------------------------------
    def _retry(self, what, func, *args):
        attempts = self.config.get("retry_count") + 1
        base = self.config.get("retry_base_delay_s")
        for attempt in range(attempts):
            try:
                return func(*args)
            except CameraGone:
                raise
            except (StickError, StickFull):
                raise
            except CameraError as e:
                if attempt == attempts - 1:
                    raise
                delay = base * (2 ** attempt)
                log.warning("%s failed (%s); retry %d in %.0f s", what, e, attempt + 1, delay)
                self.sleep(delay)

    # main --------------------------------------------------------------------
    def execute(self) -> RunResult:
        r = self.result
        started = time.time()
        folder_name = None
        self.status.update(state=st.TRANSFERRING, error=None, started_at=started, finished_at=None,
                           device={"model": self.info.model, "serial": self.info.serial,
                                   "manufacturer": self.info.manufacturer,
                                   "profile": self.profile.id},
                           run_folder=None, current_file=None, files_total=0, files_done=0,
                           bytes_total=0, bytes_done=0, speed_bps=0.0, eta_s=None)
        try:
            groups = self._scan()
            if self.on_start:
                try:
                    self.on_start(self.camera, self.info, [f for g in groups for f in g.files])
                except Exception:  # noqa: BLE001 - clock sync is best effort (#21)
                    log.exception("clock check failed")
            total_files = sum(len(g.files) for g in groups)
            total_bytes = sum(g.size for g in groups)
            r.files_left = total_files
            if not groups:
                r.empty = True
                self.status.update(state=st.DONE, finished_at=time.time())
                return r
            r.run_id = self.state.start_run(self.info, self.profile.id, started)
            self.status.update(files_total=total_files, bytes_total=total_bytes)
            self.stick.check_writable()
            folder_name = self.stick.new_run_folder_name(started)
            r.run_folder = folder_name
            folder_path = None
            for group in groups:
                if self.cancelled.is_set():
                    raise _Abort("error.cancelled", "run cancelled")
                try:
                    self.stick.ensure_space(group.size, self.config.get("min_free_mb") * 1024 * 1024,
                                            keep={folder_name}, on_delete=self._rotated)
                except StickFull as e:
                    raise _Abort("error.stick_full", str(e)) from e
                if folder_path is None:
                    folder_path = self.stick.create_run_folder(folder_name)
                    self.status.update(run_folder=folder_name)
                self._do_group(group, folder_path)
        except _Abort as e:
            r.error_id, r.error_detail = e.error_id, e.detail
        except CameraGone as e:
            r.error_id, r.error_detail = "error.camera_gone", str(e)
        except StickError as e:
            r.error_id, r.error_detail = "error.stick", str(e)
        except StickFull as e:
            r.error_id, r.error_detail = "error.stick_full", str(e)
        except CameraError as e:
            r.error_id, r.error_detail = "error.camera", str(e)
        except OSError as e:
            exc = classify_os_error(e)
            r.error_id = "error.stick_full" if isinstance(exc, StickFull) else "error.stick"
            r.error_detail = str(exc)
        except Exception as e:  # noqa: BLE001 - report anything unexpected in the UI
            log.exception("unexpected error in run")
            r.error_id, r.error_detail = "error.internal", repr(e)
        finally:
            if folder_name and r.files == 0:
                self.stick.remove_if_empty(os.path.join(self.stick.root, folder_name))
                r.run_folder = None
        if r.error_id:
            r.result = "partial" if r.files else "error"
        elif r.errors:
            r.result = "partial"
        if r.run_id is not None:
            self.state.finish_run(r.run_id, run_folder=r.run_folder, files=r.files, bytes_=r.bytes,
                                  files_left=r.files_left, result=r.result, error_id=r.error_id,
                                  error_detail=r.error_detail, errors=r.errors)
        error = {"id": r.error_id, "detail": r.error_detail} if r.error_id else (
            {"id": "error.some_files_failed", "detail": f"{len(r.errors)} file(s)"} if r.errors else None)
        self.status.update(state=st.ERROR if error else st.DONE, error=error,
                           current_file=None, speed_bps=0.0, eta_s=0 if not error else None,
                           finished_at=time.time())
        return r

    def _rotated(self, folder):
        log.info("rotation deleted %s (%d files, %d bytes)", folder.name, folder.files, folder.size)
        self.status.add_warning("warning.rotation_deleted", folder=folder.name)

    # scanning ------------------------------------------------------------------
    def _scan(self) -> List[FileGroup]:
        folders = self._retry("list folders", self.profile.media_folders, self.camera)
        groups = []
        for folder in folders:
            files = self._retry("list files", self.camera.list_files, folder)
            groups.extend(self.profile.group(files))
        return groups

    # copying -------------------------------------------------------------------
    def _suffix_for(self, group, folder_path):
        """Collision suffix (#12): "", "_1", "_2" ... applied to the whole group."""
        n = 0
        while True:
            suffix = "" if n == 0 else f"_{n}"
            names = [self.profile.local_name(f, group.primary, suffix) for f in group.files]
            if not any(os.path.exists(os.path.join(folder_path, x)) for x in names):
                return suffix, names
            n += 1

    def _do_group(self, group, folder_path):
        suffix, names = self._suffix_for(group, folder_path)
        failed = None
        for remote, local in zip(group.files, names):
            self.status.update(current_file=remote.name)
            try:
                self._retry(f"copy {remote.name}", self._copy_one, remote, folder_path, local)
            except (CameraGone, StickError, StickFull, OSError):
                raise
            except CameraError as e:
                failed = (remote, e)
                self.result.errors.append({"file": remote.name, "id": "error.copy_failed",
                                           "detail": str(e)})
                break
            self.result.files += 1
            self.result.bytes += remote.size
            self.status.update(files_done=self.result.files)
        if failed:
            # Nothing of this group is deleted; it stays on the camera.
            self._advance_bytes_skip(group, failed[0])
            return
        for remote in group.files:
            try:
                self._retry(f"delete {remote.name}", self.camera.delete, remote)
                self.result.deleted += 1
                self.result.files_left -= 1
            except CameraGone:
                raise
            except CameraError as e:
                self.result.errors.append({"file": remote.name, "id": "error.delete_failed",
                                           "detail": str(e)})

    def _advance_bytes_skip(self, group, failed):
        pass  # progress for skipped files is simply not counted

    def _copy_one(self, remote, folder_path, local_name):
        final = os.path.join(folder_path, local_name)
        part = Stick.part_path(folder_path, local_name)
        chunk = self.config.get("chunk_size_kb") * 1024
        mode = self.config.get("transfer_mode")
        base_done = self.result.bytes

        def progress(written):
            done = base_done + written
            self._speed.add(done)
            rate = self._speed.rate()
            left = self.status.snapshot()["bytes_total"] - done
            self.status.update(throttle=True, bytes_done=done, speed_bps=rate,
                               eta_s=(left / rate) if rate > 0 else None)

        try:
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        except OSError as e:
            raise classify_os_error(e) from e
        ok = False
        try:
            try:
                if mode == "auto":
                    try:
                        self.camera.read_to_fd(remote, fd, chunk, progress, "partial")
                    except NotSupported:
                        os.ftruncate(fd, 0)
                        os.lseek(fd, 0, os.SEEK_SET)
                        self.camera.read_to_fd(remote, fd, chunk, progress, "whole")
                else:
                    self.camera.read_to_fd(remote, fd, chunk, progress, mode)
                os.fsync(fd)
            except OSError as e:
                raise classify_os_error(e) from e
            finally:
                os.close(fd)
            size = os.stat(part).st_size
            if size != remote.size:
                raise CameraError(f"size mismatch on {remote.name}: camera {remote.size}, stick {size}")
            try:
                os.rename(part, final)
                Stick.fsync_dir(folder_path)
            except OSError as e:
                raise classify_os_error(e) from e
            # Check again after the rename, in case the file system lied.
            if os.stat(final).st_size != remote.size:
                os.rename(final, part)
                raise CameraError(f"size changed after rename on {remote.name}")
            ok = True
        finally:
            if not ok:
                try:
                    os.unlink(part)
                except OSError:
                    pass
