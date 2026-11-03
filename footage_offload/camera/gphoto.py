"""Real camera backend: libgphoto2 called directly through ctypes.

We deliberately do not use the python3-gphoto2 bindings (GPL-3.0+); the
LGPL library itself is fine to call from MIT code.

Only the handful of libgphoto2 functions we need are declared, each with
explicit argtypes/restype so 64-bit offsets and pointers are passed
correctly on armhf (Raspberry Pi OS) as well as on x86_64 and arm64.

Struct layouts we depend on (checked against the 2.5.x headers):

* ``CameraAbilities`` contains only chars and ints, so it is 2504 bytes on
  every architecture. ``gp_camera_set_abilities`` takes it *by value*,
  which is why it is modelled as an opaque, int-aligned array of that size.
  ``operations`` is at offset 392, ``file_operations`` at 396.
* ``CameraFileInfo``: the preview part is 88 bytes everywhere (it ends with
  two uint32 after an 8-byte aligned uint64), so ``file.fields`` is at 88
  and ``file.size`` (uint64) at 96 on every architecture. ``file.mtime`` is
  a ``time_t``, whose size differs between Bookworm armhf (32-bit) and
  Trixie armhf (64-bit, the "t64" transition); we therefore only read it on
  64-bit (LP64) platforms, where it is unambiguously at offset 184.

Error mapping (see ``_raise``):

* ``GP_ERROR_NOT_SUPPORTED`` -> ``NotSupported``.
* ``GP_ERROR_IO_USB_FIND`` -> ``CameraGone`` (libusb reports
  LIBUSB_ERROR_NO_DEVICE / NOT_FOUND this way: the device is not there).
* I/O class errors (``GP_ERROR_IO``, ``IO_READ``, ``IO_WRITE``,
  ``IO_INIT``, ``IO_USB_CLEAR_HALT``, ``TIMEOUT``, ``IO_UPDATE``) are
  ambiguous: an unplugged camera produces them, but so does a hiccup on a
  marginal cable. We look at sysfs: if ``DeviceInfo.sysfs_path`` no longer
  exists, or now belongs to a different bus/device number, the camera is
  gone (``CameraGone``); otherwise it is an ordinary, retried
  ``CameraError``. For ``disk:`` ports the directory takes the place of the
  sysfs node.
* ``GP_ERROR_IO_USB_CLAIM`` is a ``CameraError``: the device is there but
  another process (for example gvfs on a desktop) holds the interface.
* Everything else (``CAMERA_BUSY``, ``MODEL_NOT_FOUND``, ``FILE_NOT_FOUND``
  ...) is a ``CameraError`` - unless the device has visibly vanished, in
  which case any failure becomes ``CameraGone``. Once a session has seen
  ``CameraGone`` every further call raises it too.
"""
import ctypes
import ctypes.util
import logging
import os
import re
import threading
import time
from typing import List, Optional

from .base import (Backend, Camera, CameraCapabilities, CameraError, CameraGone,
                   DeviceInfo, NotSupported, RemoteFile)

log = logging.getLogger(__name__)

# --- constants from the libgphoto2 headers ----------------------------------

GP_OK = 0
GP_ERROR = -1
GP_ERROR_BAD_PARAMETERS = -2
GP_ERROR_UNKNOWN_PORT = -5
GP_ERROR_NOT_SUPPORTED = -6
GP_ERROR_IO = -7
GP_ERROR_TIMEOUT = -10
GP_ERROR_IO_INIT = -31
GP_ERROR_IO_READ = -34
GP_ERROR_IO_WRITE = -35
GP_ERROR_IO_UPDATE = -37
GP_ERROR_IO_USB_CLEAR_HALT = -51
GP_ERROR_IO_USB_FIND = -52
GP_ERROR_IO_USB_CLAIM = -53
GP_ERROR_MODEL_NOT_FOUND = -105
GP_ERROR_FILE_NOT_FOUND = -108
GP_ERROR_CAMERA_BUSY = -110

#: Errors that mean "the device is not there at all".
GONE_ERRORS = frozenset({GP_ERROR_IO_USB_FIND})
#: Errors that may mean "unplugged" or may be transient; decided via sysfs.
IO_ERRORS = frozenset({GP_ERROR_IO, GP_ERROR_TIMEOUT, GP_ERROR_IO_INIT, GP_ERROR_IO_READ,
                       GP_ERROR_IO_WRITE, GP_ERROR_IO_UPDATE, GP_ERROR_IO_USB_CLEAR_HALT})

GP_FILE_TYPE_NORMAL = 1
GP_FILE_INFO_SIZE = 1 << 2
GP_FILE_INFO_MTIME = 1 << 5
GP_OPERATION_CONFIG = 1 << 4
GP_FILE_OPERATION_DELETE = 1 << 1
GP_WIDGET_TEXT = 2
GP_WIDGET_DATE = 8

ABILITIES_SIZE = 2504
ABILITIES_OPERATIONS_OFFSET = 392
ABILITIES_FILE_OPERATIONS_OFFSET = 396
FILEINFO_SIZE = 272              # largest variant (64-bit time_t)
FILEINFO_FILE_FIELDS_OFFSET = 88
FILEINFO_FILE_SIZE_OFFSET = 96
FILEINFO_FILE_MTIME_OFFSET_LP64 = 184
CAMERA_TEXT_SIZE = 32 * 1024

DIRECTORY_MODEL = "Directory Browse"
CLOCK_WIDGET = "datetime"
TEXT_CLOCK_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%dT%H%M%S",
                      "%Y/%m/%d %H:%M:%S")

_USB_PORT_RE = re.compile(r"^usb:(\d{3}),(\d{3})$")


class CameraAbilities(ctypes.Structure):
    """Opaque CameraAbilities, passed by value to gp_camera_set_abilities."""
    _fields_ = [("data", ctypes.c_int * (ABILITIES_SIZE // ctypes.sizeof(ctypes.c_int)))]

    def int_at(self, offset):
        return ctypes.c_int.from_buffer(self, offset).value


class CameraFileInfo(ctypes.Structure):
    """Opaque CameraFileInfo; aligned for uint64 members."""
    _fields_ = [("data", ctypes.c_uint64 * (FILEINFO_SIZE // 8))]


class CameraText(ctypes.Structure):
    _fields_ = [("text", ctypes.c_char * CAMERA_TEXT_SIZE)]


_p = ctypes.c_void_p
_pp = ctypes.POINTER(ctypes.c_void_p)
_s = ctypes.c_char_p
_i = ctypes.c_int
ERROR_FUNC = ctypes.CFUNCTYPE(None, _p, _s, _p)

# name: (restype, argtypes)
_PROTOTYPES = {
    "gp_result_as_string": (_s, [_i]),
    "gp_context_new": (_p, []),
    "gp_context_unref": (None, [_p]),
    "gp_context_set_error_func": (None, [_p, ERROR_FUNC, _p]),
    "gp_camera_new": (_i, [_pp]),
    "gp_camera_init": (_i, [_p, _p]),
    "gp_camera_exit": (_i, [_p, _p]),
    "gp_camera_unref": (_i, [_p]),
    "gp_camera_set_abilities": (_i, [_p, CameraAbilities]),
    "gp_camera_get_abilities": (_i, [_p, ctypes.POINTER(CameraAbilities)]),
    "gp_camera_set_port_info": (_i, [_p, _p]),
    "gp_port_info_list_new": (_i, [_pp]),
    "gp_port_info_list_free": (_i, [_p]),
    "gp_port_info_list_load": (_i, [_p]),
    "gp_port_info_list_lookup_path": (_i, [_p, _s]),
    "gp_port_info_list_get_info": (_i, [_p, _i, _pp]),
    "gp_abilities_list_new": (_i, [_pp]),
    "gp_abilities_list_free": (_i, [_p]),
    "gp_abilities_list_load": (_i, [_p, _p]),
    "gp_abilities_list_detect": (_i, [_p, _p, _p, _p]),
    "gp_abilities_list_lookup_model": (_i, [_p, _s]),
    "gp_abilities_list_get_abilities": (_i, [_p, _i, ctypes.POINTER(CameraAbilities)]),
    "gp_list_new": (_i, [_pp]),
    "gp_list_unref": (_i, [_p]),
    "gp_list_reset": (_i, [_p]),
    "gp_list_count": (_i, [_p]),
    "gp_list_get_name": (_i, [_p, _i, ctypes.POINTER(_s)]),
    "gp_list_get_value": (_i, [_p, _i, ctypes.POINTER(_s)]),
    "gp_camera_folder_list_files": (_i, [_p, _s, _p, _p]),
    "gp_camera_folder_list_folders": (_i, [_p, _s, _p, _p]),
    "gp_camera_file_get_info": (_i, [_p, _s, _s, ctypes.POINTER(CameraFileInfo), _p]),
    "gp_camera_file_read": (_i, [_p, _s, _s, _i, ctypes.c_uint64, ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_uint64), _p]),
    "gp_camera_file_get": (_i, [_p, _s, _s, _i, _p, _p]),
    "gp_camera_file_delete": (_i, [_p, _s, _s, _p]),
    "gp_file_new_from_fd": (_i, [_pp, _i]),
    "gp_file_unref": (_i, [_p]),
    "gp_camera_get_single_config": (_i, [_p, _s, _pp, _p]),
    "gp_camera_set_single_config": (_i, [_p, _s, _p, _p]),
    "gp_widget_get_type": (_i, [_p, ctypes.POINTER(_i)]),
    "gp_widget_get_value": (_i, [_p, _p]),
    "gp_widget_set_value": (_i, [_p, _p]),
    "gp_widget_get_readonly": (_i, [_p, ctypes.POINTER(_i)]),
    "gp_widget_free": (_i, [_p]),
    "gp_camera_get_summary": (_i, [_p, ctypes.POINTER(CameraText), _p]),
}

_lib = None
_lib_lock = threading.Lock()


class LibraryUnavailable(CameraError):
    """libgphoto2 could not be loaded."""


def load_library():
    """Load libgphoto2 once and declare the prototypes. Raises LibraryUnavailable."""
    global _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        names = ["libgphoto2.so.6"]
        found = ctypes.util.find_library("gphoto2")
        if found:
            names.append(found)
        lib = None
        errors = []
        for name in names:
            try:
                lib = ctypes.CDLL(name)
                break
            except OSError as e:
                errors.append(str(e))
        if lib is None:
            raise LibraryUnavailable("cannot load libgphoto2: " + "; ".join(errors))
        # gp_port_* live in libgphoto2_port; dlsym on the libgphoto2 handle
        # searches its dependencies, but load it explicitly as a fallback.
        port = None
        for name, (restype, argtypes) in _PROTOTYPES.items():
            try:
                func = getattr(lib, name)
            except AttributeError:
                if port is None:
                    port = ctypes.CDLL(ctypes.util.find_library("gphoto2_port")
                                       or "libgphoto2_port.so.12")
                func = getattr(port, name)
            func.restype = restype
            func.argtypes = argtypes
            setattr(lib, name, func)
        _lib = lib
        return lib


def available():
    try:
        load_library()
        return True
    except (LibraryUnavailable, OSError, AttributeError):
        return False


def result_text(code):
    try:
        text = load_library().gp_result_as_string(code)
        return text.decode("utf-8", "replace") if text else f"error {code}"
    except Exception:  # noqa: BLE001 - only used for messages
        return f"error {code}"


def device_present(info: DeviceInfo) -> bool:
    """Whether the device behind `info` still seems to exist.

    For USB devices the sysfs node must exist and (when it can be read)
    still carry the bus and device numbers of the port; a camera that was
    unplugged and replugged gets a new device number. For ``disk:`` ports
    the directory must exist. Without any way to tell, assume present.
    """
    port = info.port or ""
    if port.startswith("disk:"):
        return os.path.isdir(port[len("disk:"):])
    if not info.sysfs_path:
        return True
    if not os.path.exists(info.sysfs_path):
        return False
    m = _USB_PORT_RE.match(port)
    if m:
        try:
            with open(os.path.join(info.sysfs_path, "busnum")) as f:
                bus = int(f.read().strip())
            with open(os.path.join(info.sysfs_path, "devnum")) as f:
                dev = int(f.read().strip())
        except (OSError, ValueError):
            return True
        return (bus, dev) == (int(m.group(1)), int(m.group(2)))
    return True


class _GList:
    """A CameraList owned by Python."""

    def __init__(self, lib):
        self.lib = lib
        self.ptr = ctypes.c_void_p()
        rc = lib.gp_list_new(ctypes.byref(self.ptr))
        if rc < GP_OK:
            raise CameraError(f"gp_list_new: {result_text(rc)}")

    def items(self):
        out = []
        for i in range(max(0, self.lib.gp_list_count(self.ptr))):
            name = ctypes.c_char_p()
            value = ctypes.c_char_p()
            self.lib.gp_list_get_name(self.ptr, i, ctypes.byref(name))
            self.lib.gp_list_get_value(self.ptr, i, ctypes.byref(value))
            out.append((_dec(name.value), _dec(value.value)))
        return out

    def names(self):
        return [n for n, _ in self.items()]

    def free(self):
        if self.ptr:
            self.lib.gp_list_unref(self.ptr)
            self.ptr = ctypes.c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.free()
        return False


def _dec(b):
    if b is None:
        return ""
    return b.decode("utf-8", "surrogateescape")


def _enc(s):
    return s.encode("utf-8", "surrogateescape")


class GphotoCamera(Camera):
    """One opened libgphoto2 camera session."""

    def __init__(self, backend, info: DeviceInfo, lib, camera_ptr, context_ptr, error_cb,
                 abilities: CameraAbilities, port_list=None):
        self.backend = backend
        self.info = info
        self._lib = lib
        self._cam = camera_ptr
        self._ctx = context_ptr
        self._error_cb = error_cb           # keep the callback alive
        self._port_list = port_list         # the port info points into this list
        self._lock = threading.RLock()
        self._closed = False
        self._gone = False
        self._partial_ok = True
        self._clock_settable = None
        ops = abilities.int_at(ABILITIES_OPERATIONS_OFFSET)
        file_ops = abilities.int_at(ABILITIES_FILE_OPERATIONS_OFFSET)
        self._can_delete = bool(file_ops & GP_FILE_OPERATION_DELETE)
        self._has_config = bool(ops & GP_OPERATION_CONFIG)

    # error handling ------------------------------------------------------------
    def _raise(self, rc, what):
        """Turn a libgphoto2 error code into the matching exception."""
        msg = f"{what}: {result_text(rc)} ({rc})"
        last = self.backend.last_error(self._ctx)
        if last:
            msg += f" [{last}]"
        if rc == GP_ERROR_NOT_SUPPORTED:
            raise NotSupported(msg)
        gone = rc in GONE_ERRORS or not device_present(self.info)
        if not gone and rc in IO_ERRORS:
            # The kernel may remove the sysfs node a moment after libusb
            # reported the failure; look once more before calling it transient.
            time.sleep(self.backend.gone_recheck_s)
            gone = not device_present(self.info)
        if gone:
            self._gone = True
            raise CameraGone(msg)
        raise CameraError(msg)

    def _check(self, rc, what):
        if rc < GP_OK:
            self._raise(rc, what)
        return rc

    def _ensure_open(self):
        if self._closed:
            raise CameraGone("camera session closed")
        if self._gone:
            raise CameraGone("camera disconnected")

    # interface -------------------------------------------------------------
    def capabilities(self):
        return CameraCapabilities(can_delete=self._can_delete, partial_read=self._partial_ok,
                                  clock_settable=bool(self._clock_writable()),
                                  extra={"driver": "libgphoto2"})

    def list_folders(self, folder) -> List[str]:
        with self._lock:
            self._ensure_open()
            with _GList(self._lib) as lst:
                self._check(self._lib.gp_camera_folder_list_folders(
                    self._cam, _enc(folder), lst.ptr, self._ctx), f"list folders {folder}")
                return sorted(lst.names())

    def list_files(self, folder) -> List[RemoteFile]:
        with self._lock:
            self._ensure_open()
            with _GList(self._lib) as lst:
                self._check(self._lib.gp_camera_folder_list_files(
                    self._cam, _enc(folder), lst.ptr, self._ctx), f"list files {folder}")
                names = sorted(lst.names())
            out = []
            for name in names:
                size, mtime = self._file_info(folder, name)
                out.append(RemoteFile(folder, name, size, mtime))
            return out

    def _file_info(self, folder, name):
        info = CameraFileInfo()
        self._check(self._lib.gp_camera_file_get_info(
            self._cam, _enc(folder), _enc(name), ctypes.byref(info), self._ctx),
            f"file info {folder}/{name}")
        fields = ctypes.c_int.from_buffer(info, FILEINFO_FILE_FIELDS_OFFSET).value
        if not fields & GP_FILE_INFO_SIZE:
            raise CameraError(f"camera reports no size for {folder}/{name}")
        size = ctypes.c_uint64.from_buffer(info, FILEINFO_FILE_SIZE_OFFSET).value
        mtime = None
        if fields & GP_FILE_INFO_MTIME and ctypes.sizeof(ctypes.c_void_p) == 8:
            value = ctypes.c_int64.from_buffer(info, FILEINFO_FILE_MTIME_OFFSET_LP64).value
            if value > 0:
                mtime = float(value)
        return size, mtime

    def read_to_fd(self, remote, fd, chunk_size, progress, mode="auto"):
        with self._lock:
            self._ensure_open()
            if mode == "auto":
                if self._partial_ok:
                    start = os.lseek(fd, 0, os.SEEK_CUR)
                    try:
                        return self._read_partial(remote, fd, chunk_size, progress)
                    except NotSupported:
                        log.info("partial reads not supported by %s; using whole-file reads",
                                 self.info.port)
                        os.ftruncate(fd, start)
                        os.lseek(fd, start, os.SEEK_SET)
                return self._read_whole(remote, fd, progress)
            if mode == "partial":
                return self._read_partial(remote, fd, chunk_size, progress)
            if mode == "whole":
                return self._read_whole(remote, fd, progress)
            raise ValueError(f"unknown transfer mode {mode!r}")

    def _read_partial(self, remote, fd, chunk_size, progress):
        """Chunked gp_camera_file_read; only one chunk is ever in memory."""
        chunk_size = max(4096, int(chunk_size))
        buf = ctypes.create_string_buffer(chunk_size)
        folder, name = _enc(remote.folder), _enc(remote.name)
        offset = 0
        while offset < remote.size:
            want = min(chunk_size, remote.size - offset)
            got = ctypes.c_uint64(want)
            rc = self._lib.gp_camera_file_read(self._cam, folder, name, GP_FILE_TYPE_NORMAL,
                                               offset, buf, ctypes.byref(got), self._ctx)
            if rc < GP_OK:
                if rc == GP_ERROR_NOT_SUPPORTED:
                    self._partial_ok = False
                self._raise(rc, f"read {remote.path} at {offset}")
            n = int(got.value)
            if n == 0:
                break               # camera has less than it announced; caller checks size
            if n > want:
                raise CameraError(f"read {remote.path}: driver returned {n} > {want} bytes")
            view = memoryview(buf)[:n]
            while view:
                written = os.write(fd, view)
                view = view[written:]
            offset += n
            progress(offset)
        return offset

    def _read_whole(self, remote, fd, progress):
        """gp_camera_file_get into a CameraFile wrapping `fd`.

        libgphoto2 streams the data into the descriptor itself (for PTP in
        chunks), so memory use stays small. Progress is reported by a small
        thread that polls the descriptor's size.
        """
        start = os.lseek(fd, 0, os.SEEK_CUR)
        file_ptr = ctypes.c_void_p()
        # gp_file_unref() closes the descriptor of an fd-backed CameraFile,
        # so libgphoto2 gets a duplicate it owns; ours stays open.
        dup = os.dup(fd)
        rc = self._lib.gp_file_new_from_fd(ctypes.byref(file_ptr), dup)
        if rc < GP_OK:
            os.close(dup)
            raise CameraError(f"gp_file_new_from_fd: {result_text(rc)}")
        try:
            stop = threading.Event()

            def poll():
                last = -1
                while not stop.wait(0.5):
                    try:
                        size = os.fstat(fd).st_size - start
                    except OSError:
                        return
                    if size != last:
                        last = size
                        try:
                            progress(max(0, size))
                        except Exception:  # noqa: BLE001 - progress must not kill the copy
                            log.exception("progress callback failed")

            poller = threading.Thread(target=poll, name="gphoto-progress", daemon=True)
            poller.start()
            try:
                rc = self._lib.gp_camera_file_get(self._cam, _enc(remote.folder), _enc(remote.name),
                                                  GP_FILE_TYPE_NORMAL, file_ptr, self._ctx)
            finally:
                stop.set()
                poller.join()
            if rc < GP_OK:
                self._raise(rc, f"get {remote.path}")
        finally:
            self._lib.gp_file_unref(file_ptr)    # also closes `dup`
        written = max(0, os.fstat(fd).st_size - start)
        os.lseek(fd, start + written, os.SEEK_SET)
        progress(written)
        return written

    def delete(self, remote):
        with self._lock:
            self._ensure_open()
            self._check(self._lib.gp_camera_file_delete(
                self._cam, _enc(remote.folder), _enc(remote.name), self._ctx),
                f"delete {remote.path}")

    # clock ---------------------------------------------------------------------
    def _get_widget(self):
        """(widget pointer, type, readonly) for the clock widget, or None."""
        widget = ctypes.c_void_p()
        rc = self._lib.gp_camera_get_single_config(self._cam, _enc(CLOCK_WIDGET),
                                                   ctypes.byref(widget), self._ctx)
        if rc < GP_OK:
            if rc in GONE_ERRORS or (rc in IO_ERRORS and not device_present(self.info)):
                self._raise(rc, "get clock")
            log.debug("no %s widget on %s: %s", CLOCK_WIDGET, self.info.port, result_text(rc))
            return None
        wtype = ctypes.c_int()
        ro = ctypes.c_int(0)
        self._lib.gp_widget_get_type(widget, ctypes.byref(wtype))
        self._lib.gp_widget_get_readonly(widget, ctypes.byref(ro))
        return widget, wtype.value, bool(ro.value)

    def _clock_writable(self):
        if self._clock_settable is None:
            if not self._has_config or self._closed or self._gone:
                return False
            with self._lock:
                try:
                    w = self._get_widget()
                except CameraError:
                    return False
                if w is None:
                    self._clock_settable = False
                else:
                    widget, wtype, ro = w
                    self._lib.gp_widget_free(widget)
                    self._clock_settable = (not ro) and wtype in (GP_WIDGET_DATE, GP_WIDGET_TEXT)
        return self._clock_settable

    def get_clock(self) -> Optional[float]:
        if not self._has_config:
            return None
        with self._lock:
            self._ensure_open()
            w = self._get_widget()
            if w is None:
                return None
            widget, wtype, _ = w
            try:
                if wtype == GP_WIDGET_DATE:
                    value = ctypes.c_int()
                    if self._lib.gp_widget_get_value(widget, ctypes.byref(value)) < GP_OK:
                        return None
                    return float(value.value) if value.value > 0 else None
                if wtype == GP_WIDGET_TEXT:
                    value = ctypes.c_char_p()
                    if self._lib.gp_widget_get_value(widget, ctypes.byref(value)) < GP_OK:
                        return None
                    return parse_text_clock(_dec(value.value))
                return None
            finally:
                self._lib.gp_widget_free(widget)

    def set_clock(self, timestamp):
        if not self._has_config:
            raise NotSupported("camera driver has no configuration")
        with self._lock:
            self._ensure_open()
            w = self._get_widget()
            if w is None:
                raise NotSupported("camera has no datetime setting")
            widget, wtype, ro = w
            try:
                if ro:
                    raise NotSupported("camera datetime setting is read-only")
                if wtype == GP_WIDGET_DATE:
                    value = ctypes.c_int(int(round(timestamp)))
                    rc = self._lib.gp_widget_set_value(widget, ctypes.byref(value))
                elif wtype == GP_WIDGET_TEXT:
                    text = ctypes.c_char_p(_enc(time.strftime(TEXT_CLOCK_FORMATS[0],
                                                              time.localtime(timestamp))))
                    rc = self._lib.gp_widget_set_value(widget, text)
                else:
                    raise NotSupported(f"unexpected datetime widget type {wtype}")
                if rc < GP_OK:
                    self._raise(rc, "set clock value")
                rc = self._lib.gp_camera_set_single_config(self._cam, _enc(CLOCK_WIDGET),
                                                           widget, self._ctx)
                if rc == GP_ERROR_BAD_PARAMETERS:
                    raise NotSupported(f"set clock: {result_text(rc)}")
                self._check(rc, "set clock")
            finally:
                self._lib.gp_widget_free(widget)

    # misc ----------------------------------------------------------------------
    def summary(self):
        with self._lock:
            self._ensure_open()
            text = CameraText()
            try:
                self._check(self._lib.gp_camera_get_summary(self._cam, ctypes.byref(text),
                                                            self._ctx), "summary")
            except NotSupported:
                return ""
            return _dec(text.text)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if not self._gone:
                    self._lib.gp_camera_exit(self._cam, self._ctx)
            finally:
                self._lib.gp_camera_unref(self._cam)
                if self._port_list:
                    self._lib.gp_port_info_list_free(self._port_list)
                    self._port_list = None
                self.backend.forget_context(self._ctx)
                self._lib.gp_context_unref(self._ctx)
                self._cam = None


def parse_text_clock(text) -> Optional[float]:
    """Parse a textual camera date (local time) into a UNIX timestamp."""
    text = (text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,11}", text):
        return float(text)
    for fmt in TEXT_CLOCK_FORMATS:
        try:
            return time.mktime(time.strptime(text, fmt))
        except (ValueError, OverflowError):
            continue
    return None


class GphotoBackend(Backend):
    """Opens cameras through libgphoto2.

    `info.port` is a libgphoto2 port path: ``usb:BUS,DEV`` from discovery,
    or ``disk:/some/dir`` (driver "Directory Browse", used by the tests).
    """

    gone_recheck_s = 0.5

    def __init__(self):
        self._lib = load_library()
        self._abilities = None
        self._abilities_lock = threading.Lock()
        self._errors = {}           # context address -> last error text
        self._errors_lock = threading.Lock()

        def on_error(ctx, text, data):
            msg = _dec(text).strip()
            log.debug("libgphoto2: %s", msg)
            with self._errors_lock:
                self._errors[ctx] = msg

        self._error_cb = ERROR_FUNC(on_error)

    # context errors ----------------------------------------------------------
    def last_error(self, ctx):
        with self._errors_lock:
            return self._errors.pop(ctx.value if isinstance(ctx, ctypes.c_void_p) else ctx, "")

    def forget_context(self, ctx):
        self.last_error(ctx)

    # abilities -----------------------------------------------------------------
    def _abilities_list(self, ctx):
        """Loading every camlib is slow on a Pi Zero; do it once."""
        with self._abilities_lock:
            if self._abilities is None:
                lst = ctypes.c_void_p()
                rc = self._lib.gp_abilities_list_new(ctypes.byref(lst))
                if rc < GP_OK:
                    raise CameraError(f"gp_abilities_list_new: {result_text(rc)}")
                rc = self._lib.gp_abilities_list_load(lst, ctx)
                if rc < GP_OK:
                    self._lib.gp_abilities_list_free(lst)
                    raise CameraError(f"loading camera drivers: {result_text(rc)}")
                self._abilities = lst
            return self._abilities

    def _detect_model(self, abilities_list, port_list, port, ctx):
        """libgphoto2's model name for the device on `port` (USB only)."""
        with _GList(self._lib) as found:
            rc = self._lib.gp_abilities_list_detect(abilities_list, port_list, found.ptr, ctx)
            if rc < GP_OK:
                raise CameraError(f"detect: {result_text(rc)}")
            for model, path in found.items():
                if path == port:
                    return model
        return None

    def open(self, info: DeviceInfo) -> Camera:
        lib = self._lib
        port = info.port
        if not (port.startswith("usb:") or port.startswith("disk:")):
            raise CameraError(f"unsupported port {port!r}")
        if port.startswith("usb:") and info.sysfs_path and not device_present(info):
            raise CameraGone(f"{port} is no longer connected")
        ctx = ctypes.c_void_p(lib.gp_context_new())
        if not ctx:
            raise CameraError("gp_context_new failed")
        lib.gp_context_set_error_func(ctx, self._error_cb, None)
        cam = ctypes.c_void_p()
        port_list = ctypes.c_void_p()
        ok = False
        try:
            if lib.gp_camera_new(ctypes.byref(cam)) < GP_OK:
                raise CameraError("gp_camera_new failed")
            abilities_list = self._abilities_list(ctx)

            rc = lib.gp_port_info_list_new(ctypes.byref(port_list))
            if rc < GP_OK:
                raise CameraError(f"gp_port_info_list_new: {result_text(rc)}")
            rc = lib.gp_port_info_list_load(port_list)
            if rc < GP_OK:
                raise CameraError(f"loading port drivers: {result_text(rc)}")

            if port.startswith("disk:"):
                model = DIRECTORY_MODEL
            else:
                model = self._detect_model(abilities_list, port_list, port, ctx)
                if model is None:
                    if not device_present(info):
                        raise CameraGone(f"{port} is no longer connected")
                    raise CameraError(f"no libgphoto2 driver recognises the device on {port}")
            idx = lib.gp_abilities_list_lookup_model(abilities_list, _enc(model))
            if idx < GP_OK:
                raise CameraError(f"model {model!r}: {result_text(idx)}")
            abilities = CameraAbilities()
            rc = lib.gp_abilities_list_get_abilities(abilities_list, idx, ctypes.byref(abilities))
            if rc < GP_OK:
                raise CameraError(f"abilities of {model!r}: {result_text(rc)}")
            rc = lib.gp_camera_set_abilities(cam, abilities)
            if rc < GP_OK:
                raise CameraError(f"set abilities: {result_text(rc)}")

            pidx = lib.gp_port_info_list_lookup_path(port_list, _enc(port))
            if pidx < GP_OK:
                if pidx in (GP_ERROR_IO_USB_FIND, GP_ERROR_UNKNOWN_PORT):
                    raise CameraGone(f"port {port} not found")
                raise CameraError(f"port {port}: {result_text(pidx)}")
            pinfo = ctypes.c_void_p()
            rc = lib.gp_port_info_list_get_info(port_list, pidx, ctypes.byref(pinfo))
            if rc < GP_OK:
                raise CameraError(f"port info {port}: {result_text(rc)}")
            rc = lib.gp_camera_set_port_info(cam, pinfo)
            if rc < GP_OK:
                raise CameraError(f"set port {port}: {result_text(rc)}")

            rc = lib.gp_camera_init(cam, ctx)
            if rc < GP_OK:
                msg = f"init {model} on {port}: {result_text(rc)} ({rc})"
                last = self.last_error(ctx)
                if last:
                    msg += f" [{last}]"
                if rc in GONE_ERRORS or (rc in IO_ERRORS and not device_present(info)):
                    raise CameraGone(msg)
                if rc == GP_ERROR_IO_USB_CLAIM:
                    msg += " (another process holds the device, e.g. gvfs)"
                raise CameraError(msg)
            # The returned abilities are those the driver settled on.
            lib.gp_camera_get_abilities(cam, ctypes.byref(abilities))
            log.info("opened %s (%s) on %s", info.model or model, model, port)
            camera = GphotoCamera(self, info, lib, cam, ctx, self._error_cb, abilities, port_list)
            ok = True
            return camera
        finally:
            if not ok:
                if port_list:
                    lib.gp_port_info_list_free(port_list)
                if cam:
                    lib.gp_camera_unref(cam)
                self.forget_context(ctx)
                lib.gp_context_unref(ctx)

