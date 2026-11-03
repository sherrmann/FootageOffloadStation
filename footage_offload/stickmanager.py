"""Finds USB sticks, mounts the adopted one through the root helper, ejects it.

A stick is "adopted" when its file system contains the marker file
(paths.MARKER_NAME). Sticks without it are reported with the warning
`warning.stick_unadopted` and never written to.

Block devices come from a "source" (UdevBlockSource wraps pyudev); probing
and mounting run in a worker thread so neither service start nor the udev
monitor thread waits for slow USB sticks.
"""
import logging
import os
import queue
import re
import threading
from typing import Dict, Optional

from . import paths
from .stick import Stick

log = logging.getLogger(__name__)

FSTYPES = ("ext4", "exfat", "vfat")
_DEVNAME_RE = re.compile(r"^/dev/sd[a-z]+\d*$")


class StickManagerError(Exception):
    """Eject refused or failed. `message_id` is the id for the UI."""

    def __init__(self, message_id, detail=""):
        super().__init__(detail or message_id)
        self.message_id = message_id
        self.detail = detail


class StickBusy(StickManagerError):
    def __init__(self, detail="a transfer is running"):
        super().__init__("error.busy", detail)


def is_candidate(properties) -> bool:
    """Is this udev block device a USB stick file system we could use?

    Partitions, or whole disks that carry a file system directly (no
    partition table), on the USB bus, with ext4, exFAT or FAT.
    """
    p = properties or {}
    if p.get("SUBSYSTEM", "block") != "block":
        return False
    if p.get("ID_BUS") != "usb":
        return False
    if p.get("ID_FS_TYPE") not in FSTYPES:
        return False
    if p.get("ID_FS_USAGE", "filesystem") != "filesystem":
        return False
    devtype = p.get("DEVTYPE")
    if devtype not in ("partition", "disk"):
        return False
    return bool(_DEVNAME_RE.match(p.get("DEVNAME", "")))


def mount_source(mount_point, mountinfo="/proc/self/mountinfo") -> Optional[tuple]:
    """(source device, fstype) mounted at `mount_point`, or None."""
    found = None
    try:
        with open(mountinfo) as f:
            for line in f:
                if " - " not in line:
                    continue
                left, right = line.split(" - ", 1)
                lf, rf = left.split(), right.split()
                if len(lf) >= 5 and len(rf) >= 2:
                    mp = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), lf[4])
                    if mp == mount_point:
                        found = (rf[1], rf[0])        # last one wins (stacked mounts)
    except OSError:
        return None
    return found


class UdevBlockSource:
    """pyudev wrapper: lists block devices and monitors add/remove."""

    def __init__(self, context=None):
        import pyudev
        self._pyudev = pyudev
        self.context = context or pyudev.Context()
        self._observer = None

    def list(self):
        return [{k: str(v) for k, v in d.properties.items()}
                for d in self.context.list_devices(subsystem="block")]

    def start(self, callback):
        monitor = self._pyudev.Monitor.from_netlink(self.context)
        monitor.filter_by(subsystem="block")

        def on_event(device):
            callback(device.action, {k: str(v) for k, v in device.properties.items()})

        self._observer = self._pyudev.MonitorObserver(monitor, callback=on_event,
                                                      name="stick-monitor")
        self._observer.start()

    def stop(self):
        if self._observer is not None:
            self._observer.send_stop()
            self._observer = None


class StickManager:
    def __init__(self, service, source=None, mount_point=None, ismount=os.path.ismount,
                 mountinfo="/proc/self/mountinfo"):
        self.service = service
        self.helper = service.helper
        self.status = service.status
        self.mount_point = mount_point or paths.STICK_MOUNT
        self._source = source
        self._ismount = ismount
        self._mountinfo = mountinfo
        self._lock = threading.RLock()
        self._stick: Optional[Stick] = None
        self._device = None                   # device of the adopted, mounted stick
        self._meta = {}                       # label, fstype of it
        self._unadopted: Dict[str, dict] = {} # device -> udev properties
        self._ejected = None                  # status dict after an eject
        self._queue = queue.Queue()
        self._worker = None

    @property
    def source(self):
        if self._source is None:
            self._source = UdevBlockSource()
        return self._source

    # life cycle ----------------------------------------------------------------
    def start(self):
        self._worker = threading.Thread(target=self._work, name="stick-manager", daemon=True)
        self._worker.start()
        self._adopt_existing_mount()
        try:
            for props in self.source.list():
                self._queue.put(("add", props))
            self.source.start(lambda action, props: self._queue.put((action, props)))
        except Exception:  # noqa: BLE001 - without udev we still serve a mounted stick
            log.exception("cannot watch block devices")
        self._publish()

    def stop(self):
        if self._source is not None:
            try:
                self._source.stop()
            except Exception:  # noqa: BLE001
                log.exception("stopping block monitor")
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=5)

    def wait_idle(self):
        """Block until queued udev events have been handled (for tests)."""
        self._queue.join()

    # public API ------------------------------------------------------------------
    def stick(self) -> Optional[Stick]:
        with self._lock:
            return self._stick

    def refresh(self):
        self._publish()

    def rescan(self):
        """Probe the connected unadopted sticks again (e.g. after adopting one)."""
        with self._lock:
            pending = list(self._unadopted.values())
            self._unadopted.clear()
        for props in pending:
            props = {k: v for k, v in props.items() if k != "_meta"}
            self._queue.put(("add", props))

    def eject(self):
        if self.service.busy():
            raise StickBusy()
        with self._lock:
            stick, device, meta = self._stick, self._device, dict(self._meta)
            if stick is None:
                raise StickManagerError("error.no_stick", "no adopted stick mounted")
            # From here on no new run gets the stick.
            self._stick = None
        try:
            self.helper.call("eject_stick")
        except Exception as e:
            with self._lock:
                self._stick = stick
            log.warning("eject of %s failed: %s", device, e)
            raise StickManagerError("error.helper", str(e)) from e
        log.info("stick %s ejected", device)
        with self._lock:
            self._device = None
            self._meta = {}
            self._ejected = self._base_status(device, meta, adopted=True)
            self._ejected.update(present=False, ejected=True)
        self._publish()

    # events ------------------------------------------------------------------------
    def _work(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                action, props = item
                if action == "add":
                    self._added(props)
                elif action == "remove":
                    self._removed(props)
                elif action == "change":
                    # e.g. a stick formatted while plugged in, or media change
                    if is_candidate(props) and props.get("DEVNAME") not in (
                            self._device, *self._unadopted):
                        self._added(props)
            except Exception:  # noqa: BLE001 - the worker must survive
                log.exception("stick event failed")
            finally:
                self._queue.task_done()

    def _adopt_existing_mount(self):
        """A stick mounted at our mount point before the service started."""
        if not self._ismount(self.mount_point):
            return
        stick = Stick(self.mount_point)
        if not stick.has_marker():
            return
        src = mount_source(self.mount_point, self._mountinfo)
        device, fstype = src if src else ("", "")
        log.info("adopted stick %s already mounted at %s", device or "?", self.mount_point)
        self._mounted(stick, device, {"fstype": fstype, "label": ""})

    def _added(self, props):
        if not is_candidate(props):
            return
        device = props["DEVNAME"]
        with self._lock:
            if device == self._device:
                if props.get("ID_FS_LABEL") and not self._meta.get("label"):
                    self._meta["label"] = props.get("ID_FS_LABEL", "")
                    self._publish()
                return
            busy_with_other = self._stick is not None
        try:
            probe = self.helper.call("probe_stick", device=device) or {}
        except Exception as e:  # noqa: BLE001
            log.warning("probing %s failed: %s", device, e)
            return
        meta = {"label": probe.get("label") or props.get("ID_FS_LABEL", ""),
                "fstype": probe.get("fstype") or props.get("ID_FS_TYPE", "")}
        if not probe.get("marker"):
            log.info("stick %s has no marker file; ignored", device)
            with self._lock:
                self._unadopted[device] = dict(props, _meta=meta)
            self.status.add_warning("warning.stick_unadopted", device=device)
            self._publish()
            return
        if busy_with_other:
            log.warning("second adopted stick %s ignored; %s is in use", device, self._device)
            return
        try:
            result = self.helper.call("mount_stick", device=device) or {}
        except Exception as e:  # noqa: BLE001
            log.warning("mounting %s failed: %s", device, e)
            return
        meta["fstype"] = result.get("fstype") or meta["fstype"]
        self._mounted(Stick(self.mount_point), device, meta)

    def _mounted(self, stick, device, meta):
        try:
            removed = stick.cleanup()
            if removed:
                log.info("removed %d unfinished file(s) from the stick", removed)
        except OSError as e:
            log.warning("clean-up of stick failed: %s", e)
        with self._lock:
            self._stick = stick
            self._device = device
            self._meta = dict(meta)
            self._ejected = None
            self._unadopted.pop(device, None)
            no_unadopted = not self._unadopted
        if no_unadopted:
            self.status.clear_warning("warning.stick_unadopted")
        log.info("stick %s (%s) ready at %s", device, meta.get("fstype"), stick.root)
        self._publish()
        self.service.stick_changed()

    def _removed(self, props):
        device = props.get("DEVNAME", "")
        with self._lock:
            was_adopted = device and device == self._device
            if was_adopted:
                log.warning("adopted stick %s removed without eject", device)
                self._stick = None
                self._device = None
                self._meta = {}
            had = self._unadopted.pop(device, None) is not None
            no_unadopted = not self._unadopted
        if had and no_unadopted:
            self.status.clear_warning("warning.stick_unadopted")
        elif had:
            last = next(reversed(list(self._unadopted)))
            self.status.add_warning("warning.stick_unadopted", device=last)
        if was_adopted or had:
            self._publish()

    # status --------------------------------------------------------------------------
    @staticmethod
    def _base_status(device, meta, adopted):
        return {"present": True, "adopted": adopted, "device": device or "",
                "label": meta.get("label", "") or "", "fstype": meta.get("fstype", "") or "",
                "total": 0, "free": 0, "oldest": None, "newest": None, "runs": 0,
                "read_only": False, "ejected": False}

    def _compute(self):
        with self._lock:
            stick, device, meta = self._stick, self._device, dict(self._meta)
            ejected = dict(self._ejected) if self._ejected else None
            unadopted = list(self._unadopted.items())
        if stick is not None:
            s = self._base_status(device, meta, adopted=True)
            try:
                s["total"], s["free"] = stick.space()
                s["read_only"] = bool(os.statvfs(stick.root).f_flag & os.ST_RDONLY)
                runs = stick.run_folders()
                s["runs"] = len(runs)
                if runs:
                    s["oldest"], s["newest"] = runs[0].name, runs[-1].name
            except OSError as e:
                log.warning("reading stick state failed: %s", e)
                s["read_only"] = True
            return s
        if unadopted:
            device, props = unadopted[-1]
            return self._base_status(device, props.get("_meta", {}), adopted=False)
        return ejected

    def _publish(self):
        self.status.update(stick=self._compute())
