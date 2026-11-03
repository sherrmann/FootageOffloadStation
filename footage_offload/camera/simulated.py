"""Simulated camera backed by a directory, with fault injection.

Used by the test suite and by `footage-offload serve --simulate DIR` for
trying the UI without hardware. The directory layout is what the camera
would show, e.g. DIR/DCIM/100GOPRO/GH010001.MP4. Folders are presented with
a leading "/store_00010001" like a real PTP camera.
"""
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from .base import (Backend, Camera, CameraCapabilities, CameraError, CameraGone,
                   DeviceInfo, NotSupported, RemoteFile)

STORE = "/store_00010001"


@dataclass
class Faults:
    """Faults to inject. All counters are decremented as they fire."""
    unplug_after_bytes: Optional[int] = None      # CameraGone once this many bytes were read in total
    size_mismatch: Set[str] = field(default_factory=set)   # write one byte fewer than reported
    read_failures: Dict[str, int] = field(default_factory=dict)    # name -> transient failures
    delete_failures: Dict[str, int] = field(default_factory=dict)  # name -> failures (999 = always)
    no_partial_read: bool = False
    clock_settable: bool = True
    clock_offset_s: float = 0.0                   # camera clock = real time + offset
    read_delay_s: float = 0.0                     # per chunk, to make the UI show progress


class SimulatedCamera(Camera):
    def __init__(self, root, info, faults):
        self.root = root
        self.info = info
        self.faults = faults
        self._bytes_read = 0
        self._closed = False
        self.clock_set_to = None
        self.deleted = []

    # helpers -------------------------------------------------------------
    def _local(self, folder):
        if not folder.startswith(STORE):
            raise CameraError(f"no such folder {folder}")
        rel = folder[len(STORE):].strip("/")
        return os.path.join(self.root, rel) if rel else self.root

    def _check(self):
        if self._closed:
            raise CameraGone("session closed")
        if not os.path.isdir(self.root):
            raise CameraGone("camera disappeared")

    # interface -------------------------------------------------------------
    def capabilities(self):
        return CameraCapabilities(can_delete=True,
                                  partial_read=not self.faults.no_partial_read,
                                  clock_settable=self.faults.clock_settable)

    def list_folders(self, folder):
        self._check()
        if folder == "/":
            return [STORE.strip("/")]
        local = self._local(folder)
        try:
            return sorted(n for n in os.listdir(local)
                          if os.path.isdir(os.path.join(local, n)))
        except OSError as e:
            raise CameraError(str(e)) from e

    def list_files(self, folder):
        self._check()
        if folder == "/":
            return []
        local = self._local(folder)
        out = []
        for n in sorted(os.listdir(local)):
            p = os.path.join(local, n)
            if os.path.isfile(p):
                st = os.stat(p)
                out.append(RemoteFile(folder, n, st.st_size, st.st_mtime))
        return out

    def read_to_fd(self, remote, fd, chunk_size, progress, mode="auto"):
        self._check()
        if mode == "partial" and self.faults.no_partial_read:
            raise NotSupported("partial reads not supported")
        left = self.faults.read_failures.get(remote.name, 0)
        if left:
            self.faults.read_failures[remote.name] = left - 1
            raise CameraError(f"simulated read error on {remote.name}")
        local = os.path.join(self._local(remote.folder), remote.name)
        written = 0
        limit = remote.size - 1 if remote.name in self.faults.size_mismatch else None
        try:
            with open(local, "rb") as f:
                while True:
                    want = chunk_size
                    if limit is not None:
                        want = min(want, limit - written)
                        if want <= 0:
                            break
                    data = f.read(want)
                    if not data:
                        break
                    if self.faults.unplug_after_bytes is not None:
                        room = self.faults.unplug_after_bytes - self._bytes_read
                        if room < len(data):
                            if room > 0:
                                os.write(fd, data[:room])
                                self._bytes_read += room
                            self._closed = True
                            raise CameraGone("simulated unplug")
                    view = memoryview(data)
                    while view:
                        n = os.write(fd, view)
                        view = view[n:]
                    written += len(data)
                    self._bytes_read += len(data)
                    progress(written)
                    if self.faults.read_delay_s:
                        time.sleep(self.faults.read_delay_s)
        except FileNotFoundError as e:
            raise CameraError(str(e)) from e
        return written

    def delete(self, remote):
        self._check()
        left = self.faults.delete_failures.get(remote.name, 0)
        if left:
            self.faults.delete_failures[remote.name] = left - 1
            raise CameraError(f"simulated delete error on {remote.name}")
        os.unlink(os.path.join(self._local(remote.folder), remote.name))
        self.deleted.append(remote.path)

    def get_clock(self):
        return time.time() + self.faults.clock_offset_s

    def set_clock(self, timestamp):
        if not self.faults.clock_settable:
            raise NotSupported("clock not settable")
        self.clock_set_to = timestamp
        self.faults.clock_offset_s = timestamp - time.time()

    def summary(self):
        return f"Simulated camera\nModel: {self.info.model}\nSerial Number: {self.info.serial}\n"

    def close(self):
        self._closed = True


class SimulatedBackend(Backend):
    """Maps DeviceInfo.port "sim:<name>" to directories under `base`."""

    def __init__(self, base):
        self.base = base
        self.faults = {}          # port -> Faults
        self.opened = []
        self._lock = threading.Lock()

    def device(self, name, model="HERO6 Black", manufacturer="GoPro",
               serial=None, vendor_id="2672", product_id="0037"):
        return DeviceInfo(port=f"sim:{name}", sysfs_path="", vendor_id=vendor_id,
                          product_id=product_id, manufacturer=manufacturer,
                          model=model, serial=serial or f"SIM{name.upper()}")

    def open(self, info):
        name = info.port.split(":", 1)[1]
        root = os.path.join(self.base, name)
        if not os.path.isdir(root):
            raise CameraGone("no such simulated camera")
        with self._lock:
            faults = self.faults.setdefault(info.port, Faults())
        cam = SimulatedCamera(root, info, faults)
        self.opened.append(cam)
        return cam
