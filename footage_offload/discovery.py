"""Finds MTP/PTP cameras on USB and reports them to the service.

pyudev is wrapped in a small "source" (UdevUsbSource) that turns udev
devices into plain `UsbRecord`s; the decision logic below works on those
records only, so it can be tested without udev.
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .camera.base import DeviceInfo

log = logging.getLogger(__name__)

STILL_IMAGE_CLASS = "06"
SYSFS_USB_DEVICES = "/sys/bus/usb/devices/"
_ATTRS = ("idVendor", "idProduct", "manufacturer", "product", "serial", "authorized",
          "busnum", "devnum")


@dataclass
class UsbRecord:
    """What we need of one udev usb_device."""
    sys_name: str                                   # "1-1.2"
    sys_path: str = ""                              # "/sys/devices/.../1-1.2"
    properties: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, str] = field(default_factory=dict)
    interface_classes: List[str] = field(default_factory=list)   # ["06", "ff"]


def interface_classes_from_property(value) -> List[str]:
    """Classes from udev's ID_USB_INTERFACES, e.g. ":060101:ffff00:" -> ["06", "ff"]."""
    out = []
    for entry in (value or "").split(":"):
        if re.fullmatch(r"[0-9A-Fa-f]{6}", entry):
            out.append(entry[:2].lower())
    return out


def is_camera(properties, interface_classes) -> bool:
    """Does a USB device look like an MTP/PTP camera?

    `properties` are udev properties (any mapping), `interface_classes` the
    two-digit hex interface classes. Our own udev rule sets FOS_CAMERA=1;
    libgphoto2's rules set ID_GPHOTO2=1; libmtp's set ID_MTP_DEVICE=1.
    Interface class 06 is "still image" (PTP). Hubs (09) never count.
    """
    props = properties or {}
    if str(props.get("DEVTYPE", "usb_device")) != "usb_device":
        return False
    if str(props.get("ID_USB_CLASS_FROM_DATABASE", "")).lower() == "hub" or \
            str(props.get("TYPE", "")).startswith("9/"):
        return False
    if str(props.get("FOS_CAMERA", "")) == "0":
        return False                       # explicit opt-out in a local udev rule
    for key in ("FOS_CAMERA", "ID_GPHOTO2", "ID_MTP_DEVICE"):
        if str(props.get(key, "")) == "1":
            return True
    classes = {str(c).lower().zfill(2) for c in (interface_classes or [])}
    classes.update(interface_classes_from_property(props.get("ID_USB_INTERFACES")))
    return STILL_IMAGE_CLASS in classes


def port_of(properties) -> Optional[str]:
    """libgphoto2 port "usb:001,005" from BUSNUM/DEVNUM, or None."""
    try:
        bus = int(str(properties.get("BUSNUM", "")))
        dev = int(str(properties.get("DEVNUM", "")))
    except ValueError:
        return None
    if not (0 < bus < 1000 and 0 < dev < 1000):
        return None
    return f"usb:{bus:03d},{dev:03d}"


def _clean(value):
    return " ".join(str(value or "").replace("_", " ").split())


def device_info(record: UsbRecord) -> Optional[DeviceInfo]:
    props, attrs = record.properties, record.attributes
    port = port_of(props) or port_of({"BUSNUM": attrs.get("busnum", ""),
                                      "DEVNUM": attrs.get("devnum", "")})
    if port is None:
        return None
    return DeviceInfo(
        port=port,
        # The /sys/bus/usb/devices/<name> form is what the root helper accepts.
        sysfs_path=SYSFS_USB_DEVICES + record.sys_name if record.sys_name else "",
        vendor_id=(attrs.get("idVendor") or props.get("ID_VENDOR_ID", "")).lower(),
        product_id=(attrs.get("idProduct") or props.get("ID_MODEL_ID", "")).lower(),
        manufacturer=(attrs.get("manufacturer") or _clean(props.get("ID_VENDOR", ""))).strip(),
        model=(attrs.get("product") or _clean(props.get("ID_MODEL", ""))).strip(),
        serial=(attrs.get("serial") or props.get("ID_SERIAL_SHORT", "")).strip(),
    )


class UdevUsbSource:
    """pyudev wrapper: enumerates usb_device records and monitors them."""

    def __init__(self, context=None):
        import pyudev   # imported here so the module loads without pyudev
        self._pyudev = pyudev
        self.context = context or pyudev.Context()
        self._observer = None

    def _record(self, device, with_attrs=True):
        props = {k: str(v) for k, v in device.properties.items()}
        attrs, classes = {}, []
        if with_attrs:
            for name in _ATTRS:
                try:
                    attrs[name] = device.attributes.asstring(name).strip()
                except (KeyError, UnicodeDecodeError, OSError, AttributeError):
                    pass
            try:
                for child in device.children:
                    if child.device_type == "usb_interface":
                        try:
                            classes.append(child.attributes.asstring("bInterfaceClass").strip())
                        except (KeyError, UnicodeDecodeError, OSError):
                            pass
            except Exception:  # noqa: BLE001 - device may vanish while we look
                pass
        return UsbRecord(device.sys_name, device.sys_path, props, attrs, classes)

    def list(self):
        out = []
        for dev in self.context.list_devices(subsystem="usb", DEVTYPE="usb_device"):
            try:
                out.append(self._record(dev))
            except Exception:  # noqa: BLE001
                log.debug("cannot read %s", dev, exc_info=True)
        return out

    def start(self, callback):
        monitor = self._pyudev.Monitor.from_netlink(self.context)
        monitor.filter_by(subsystem="usb", device_type="usb_device")

        def on_event(device):
            action = device.action
            try:
                record = self._record(device, with_attrs=(action == "add"))
            except Exception:  # noqa: BLE001
                log.debug("cannot read event device", exc_info=True)
                return
            callback(action, record)

        self._observer = self._pyudev.MonitorObserver(monitor, callback=on_event,
                                                      name="usb-discovery")
        self._observer.daemon = True
        self._observer.start()

    def stop(self):
        if self._observer is not None:
            self._observer.send_stop()
            self._observer = None


class Discovery:
    """Reports cameras to `service.camera_added(info)` / `camera_removed(port)`."""

    def __init__(self, service, backend=None, source=None):
        self.service = service
        self.backend = backend
        self._source = source
        self._known = {}            # sys_name -> port of devices reported as cameras

    @property
    def source(self):
        if self._source is None:
            self._source = UdevUsbSource()
        return self._source

    def start(self):
        for record in self.source.list():
            self._safe(self.handle, "add", record, True)
        self.source.start(lambda action, record: self._safe(self.handle, action, record, False))

    def stop(self):
        if self._source is not None:
            self._source.stop()

    @staticmethod
    def _safe(func, *args):
        try:
            func(*args)
        except Exception:  # noqa: BLE001 - the monitor thread must survive
            log.exception("discovery event failed")

    def handle(self, action, record: UsbRecord, initial=False):
        if action == "add":
            if not is_camera(record.properties, record.interface_classes):
                return
            if record.attributes.get("authorized") == "0":
                # Already released after a run (e.g. the service restarted).
                log.debug("skipping de-authorised device %s", record.sys_name)
                return
            info = device_info(record)
            if info is None:
                log.warning("camera %s without bus/device number", record.sys_name)
                return
            self._known[record.sys_name] = info.port
            log.info("camera found on %s (%s %s)%s", info.port, info.manufacturer, info.model,
                     " at start" if initial else "")
            self.service.camera_added(info)
        elif action == "remove":
            port = self._known.pop(record.sys_name, None)
            if port is None:
                return
            log.info("camera on %s removed", port)
            self.service.camera_removed(port)
