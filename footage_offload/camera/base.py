"""Camera interface shared by the real (libgphoto2) and simulated backends.

The transfer engine only talks to these classes. Both backends must behave
the same way, in particular when it comes to errors:

* CameraGone   - the device disappeared (unplugged, powered off, USB reset).
                 The run stops; nothing more can be done with the device.
* CameraError  - any other failure on one operation. The engine retries it.
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional


class CameraError(Exception):
    """A single camera operation failed; it may succeed when retried."""


class CameraGone(CameraError):
    """The camera is no longer reachable. Not retried."""


class NotSupported(CameraError):
    """The camera or driver does not support this operation."""


@dataclass(frozen=True)
class DeviceInfo:
    """What discovery knows about a device before it is opened."""
    port: str                      # libgphoto2 port, e.g. "usb:001,005"; or "sim:<name>"
    sysfs_path: str = ""           # /sys/bus/usb/devices/1-1.2 (for de-authorising)
    vendor_id: str = ""            # "2672"
    product_id: str = ""
    manufacturer: str = ""
    model: str = ""
    serial: str = ""


@dataclass(frozen=True)
class RemoteFile:
    folder: str                    # absolute camera folder, e.g. /store_00010001/DCIM/100GOPRO
    name: str                      # GH010123.MP4
    size: int                      # bytes, as reported by the camera
    mtime: Optional[float] = None  # camera timestamp (unreliable, used only for clock warnings)

    @property
    def path(self):
        return self.folder.rstrip("/") + "/" + self.name


ProgressCallback = Callable[[int], None]   # called with bytes written so far


@dataclass
class CameraCapabilities:
    can_delete: bool = True
    partial_read: bool = True
    clock_settable: bool = False
    extra: dict = field(default_factory=dict)


class Camera:
    """An opened camera session. Not thread-safe; one run uses it at a time."""

    info: DeviceInfo

    def capabilities(self) -> CameraCapabilities:
        raise NotImplementedError

    def list_folders(self, folder: str) -> List[str]:
        """Names (not paths) of the sub-folders of `folder`."""
        raise NotImplementedError

    def list_files(self, folder: str) -> List[RemoteFile]:
        """Files directly in `folder`, with sizes."""
        raise NotImplementedError

    def read_to_fd(self, remote: RemoteFile, fd: int, chunk_size: int,
                   progress: ProgressCallback, mode: str = "auto") -> int:
        """Stream `remote` into the open file descriptor `fd`.

        Must never hold the whole file in memory. `mode` is "auto",
        "partial" (chunked partial reads) or "whole" (libgphoto2 writes to
        the descriptor itself). Returns the number of bytes written.
        """
        raise NotImplementedError

    def delete(self, remote: RemoteFile) -> None:
        raise NotImplementedError

    def get_clock(self) -> Optional[float]:
        """Camera time as a UNIX timestamp, or None if not available."""
        return None

    def set_clock(self, timestamp: float) -> None:
        """Set the camera clock. Raises NotSupported if not possible over USB."""
        raise NotSupported("clock not settable over USB")

    def summary(self) -> str:
        return ""

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Backend:
    """Opens cameras. Discovery hands it a DeviceInfo."""

    def open(self, info: DeviceInfo) -> Camera:
        raise NotImplementedError
