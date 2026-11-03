"""Footage storage on the adopted USB stick.

Layout on the stick:

    /.footage-offload-station        marker file; without it nothing is written
    /2026-11-03_21-47/               one folder per run (Pi clock at run start)
        GH010123.MP4
        GH010123.THM
        GL010123.LRV
        .GH010124.MP4.part           file being copied (removed on next start)

Only folders whose name matches the run-folder pattern are ever deleted by
rotation, and never the folder of the current run.
"""
import errno
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import List, Optional

from .paths import MARKER_NAME

RUN_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}(_\d+)?$")
PART_PREFIX = "."
PART_SUFFIX = ".part"
# errno values meaning "this file system cannot fsync a directory"
_DIR_SYNC_UNSUPPORTED = (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF)


class StickError(Exception):
    """The stick cannot be used: missing, read-only, I/O error. Stops the run."""


class StickFull(Exception):
    """Not enough room even after rotation. Remaining files stay on the camera."""


@dataclass
class RunFolder:
    name: str
    path: str
    size: int
    files: int


def is_run_folder(name):
    return bool(RUN_DIR_RE.match(name))


def run_folder_name(timestamp=None):
    """Folder name for a run that starts now: 2026-11-03_21-47 (no colons)."""
    return time.strftime("%Y-%m-%d_%H-%M", time.localtime(timestamp))


def classify_os_error(exc):
    """Map an OSError on the stick to StickError/StickFull."""
    if exc.errno == errno.ENOSPC or exc.errno == errno.EDQUOT:
        return StickFull(str(exc))
    return StickError(f"{errno.errorcode.get(exc.errno, exc.errno)}: {exc.strerror}")


class Stick:
    def __init__(self, root):
        self.root = root

    # state -------------------------------------------------------------------
    def has_marker(self):
        return os.path.isfile(os.path.join(self.root, MARKER_NAME))

    def check_writable(self):
        """Raise StickError if the stick is missing, unmarked or read-only."""
        if not self.has_marker():
            raise StickError("stick not present or marker file missing")
        try:
            st = os.statvfs(self.root)
        except OSError as e:
            raise StickError(str(e)) from e
        if st.f_flag & os.ST_RDONLY:
            raise StickError("stick is mounted read-only")
        probe = os.path.join(self.root, ".write-test")
        try:
            with open(probe, "wb") as f:
                f.write(b"ok")
                f.flush()
                os.fsync(f.fileno())
            os.unlink(probe)
        except OSError as e:
            raise StickError(f"stick not writable: {e.strerror}") from e

    def space(self):
        """(total_bytes, free_bytes) available to the service."""
        st = os.statvfs(self.root)
        return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize

    def free_bytes(self):
        return self.space()[1]

    # run folders -------------------------------------------------------------
    def run_folders(self) -> List[RunFolder]:
        """Run folders, oldest first."""
        out = []
        try:
            names = os.listdir(self.root)
        except OSError:
            return out
        for name in sorted(names):
            path = os.path.join(self.root, name)
            if is_run_folder(name) and os.path.isdir(path):
                size = files = 0
                for entry in os.scandir(path):
                    if entry.is_file(follow_symlinks=False):
                        files += 1
                        size += entry.stat(follow_symlinks=False).st_size
                out.append(RunFolder(name, path, size, files))
        return out

    def new_run_folder_name(self, timestamp=None):
        """A run folder name not used yet. Does not create the folder."""
        base = run_folder_name(timestamp)
        name, n = base, 1
        while os.path.exists(os.path.join(self.root, name)):
            n += 1
            name = f"{base}_{n}"
        return name

    def create_run_folder(self, name):
        path = os.path.join(self.root, name)
        try:
            os.mkdir(path)
            self.fsync_dir(self.root)
        except OSError as e:
            raise classify_os_error(e) from e
        return path

    def remove_if_empty(self, path):
        try:
            os.rmdir(path)
            self.fsync_dir(self.root)
            return True
        except OSError:
            return False

    # rotation ------------------------------------------------------------------
    def ensure_space(self, needed, min_free, keep=(), on_delete=None):
        """Delete the oldest run folders until `needed` bytes fit while
        `min_free` bytes stay free. Folders named in `keep` are never deleted.
        Raises StickFull if that is not possible."""
        while self.free_bytes() - needed < min_free:
            candidates = [r for r in self.run_folders() if r.name not in keep]
            if not candidates:
                raise StickFull(f"need {needed} bytes plus {min_free} reserve, "
                                f"{self.free_bytes()} free")
            oldest = candidates[0]
            try:
                shutil.rmtree(oldest.path)
                self.fsync_dir(self.root)
            except OSError as e:
                raise classify_os_error(e) from e
            if on_delete:
                on_delete(oldest)

    # crash recovery ------------------------------------------------------------
    def cleanup(self):
        """Remove half-written temporary files and empty run folders.

        Returns the number of files removed. Only touches *.part files that
        follow our naming inside run folders."""
        removed = 0
        for run in self.run_folders():
            for entry in os.scandir(run.path):
                n = entry.name
                if n.startswith(PART_PREFIX) and n.endswith(PART_SUFFIX) and entry.is_file():
                    try:
                        os.unlink(entry.path)
                        removed += 1
                    except OSError:
                        pass
            self.remove_if_empty(run.path)
        return removed

    # helpers -----------------------------------------------------------------
    @staticmethod
    def part_path(folder, local_name):
        return os.path.join(folder, PART_PREFIX + local_name + PART_SUFFIX)

    @staticmethod
    def fsync_dir(path):
        """Flush a directory entry to disk. Raises OSError on real failures.

        Only "directory sync not supported" is ignored (some exFAT/vfat
        drivers); an I/O error must reach the caller so the camera original
        is not deleted."""
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        except OSError as e:
            if e.errno not in _DIR_SYNC_UNSUPPORTED:
                raise
        finally:
            os.close(fd)

    def oldest_newest(self):
        runs = self.run_folders()
        if not runs:
            return None, None
        return runs[0].name, runs[-1].name

    def resolve(self, run_name, file_name=None) -> Optional[str]:
        """Safe path for the web UI; None if the name is not acceptable."""
        if not is_run_folder(run_name):
            return None
        path = os.path.join(self.root, run_name)
        if file_name is not None:
            if (not file_name or "/" in file_name or "\\" in file_name
                    or file_name.startswith(".") or "\x00" in file_name):
                return None
            path = os.path.join(path, file_name)
        return path
