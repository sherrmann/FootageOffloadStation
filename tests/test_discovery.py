"""Camera discovery logic, with a fake udev source."""
from footage_offload.discovery import (Discovery, UsbRecord, device_info,
                                       interface_classes_from_property, is_camera, port_of)


def gopro(sys_name="1-1.2", bus="1", dev="5", **extra):
    props = {"DEVTYPE": "usb_device", "BUSNUM": bus.zfill(3), "DEVNUM": dev.zfill(3),
             "ID_VENDOR_ID": "2672", "ID_MODEL_ID": "0037", "ID_VENDOR": "GoPro",
             "ID_MODEL": "HERO6_Black", "ID_SERIAL_SHORT": "C3221324567890",
             "ID_USB_INTERFACES": ":060101:"}
    props.update(extra)
    attrs = {"idVendor": "2672", "idProduct": "0037", "manufacturer": "GoPro",
             "product": "HERO6 Black", "serial": "C3221324567890", "authorized": "1"}
    return UsbRecord(sys_name, f"/sys/devices/platform/soc/usb1/1-1/{sys_name}", props, attrs, ["06"])


def test_is_camera():
    assert is_camera({"ID_GPHOTO2": "1"}, [])
    assert is_camera({"ID_MTP_DEVICE": "1"}, [])
    assert is_camera({"FOS_CAMERA": "1"}, [])
    assert is_camera({}, ["06"])
    assert is_camera({}, ["6"])
    assert is_camera({"ID_USB_INTERFACES": ":ff0000:060101:"}, [])
    assert not is_camera({}, [])
    assert not is_camera({"ID_USB_INTERFACES": ":080650:"}, ["08"])      # mass storage
    assert not is_camera({"ID_GPHOTO2": "0"}, ["ff"])
    assert not is_camera({"FOS_CAMERA": "0", "ID_GPHOTO2": "1"}, ["06"])  # explicit opt-out
    assert not is_camera({"DEVTYPE": "usb_interface"}, ["06"])
    assert not is_camera({"TYPE": "9/0/3", "ID_GPHOTO2": "1"}, [])         # hub
    assert not is_camera(None, None)


def test_interface_classes_from_property():
    assert interface_classes_from_property(":060101:FFFF00:") == ["06", "ff"]
    assert interface_classes_from_property("") == []
    assert interface_classes_from_property(None) == []


def test_port_of():
    assert port_of({"BUSNUM": "001", "DEVNUM": "005"}) == "usb:001,005"
    assert port_of({"BUSNUM": "2", "DEVNUM": "17"}) == "usb:002,017"
    assert port_of({"BUSNUM": "x", "DEVNUM": "5"}) is None
    assert port_of({}) is None


def test_device_info():
    info = device_info(gopro())
    assert info.port == "usb:001,005"
    assert info.sysfs_path == "/sys/bus/usb/devices/1-1.2"
    assert (info.vendor_id, info.product_id) == ("2672", "0037")
    assert (info.manufacturer, info.model, info.serial) == ("GoPro", "HERO6 Black", "C3221324567890")
    # Without sysfs attributes the udev properties are used.
    r = gopro()
    r.attributes = {}
    info = device_info(r)
    assert info.model == "HERO6 Black" and info.manufacturer == "GoPro"


class FakeService:
    def __init__(self):
        self.added, self.removed = [], []

    def camera_added(self, info):
        self.added.append(info)

    def camera_removed(self, port):
        self.removed.append(port)


class FakeSource:
    def __init__(self, records):
        self.records = records
        self.callback = None
        self.stopped = False

    def list(self):
        return self.records

    def start(self, callback):
        self.callback = callback

    def stop(self):
        self.stopped = True


def test_discovery_existing_and_events():
    keyboard = UsbRecord("1-1.3", "", {"DEVTYPE": "usb_device", "BUSNUM": "001", "DEVNUM": "006",
                                       "ID_USB_INTERFACES": ":030101:"}, {}, ["03"])
    released = gopro("1-1.4", dev="7")
    released.attributes["authorized"] = "0"
    svc = FakeService()
    src = FakeSource([gopro(), keyboard, released])
    d = Discovery(svc, backend=None, source=src)
    d.start()
    assert [i.port for i in svc.added] == ["usb:001,005"]
    # A new camera appears, then the first one is unplugged.
    src.callback("add", gopro("1-1.1", dev="9"))
    src.callback("bind", gopro("1-1.1", dev="9"))
    src.callback("change", gopro("1-1.1", dev="9"))
    assert [i.port for i in svc.added] == ["usb:001,005", "usb:001,009"]
    src.callback("remove", UsbRecord("1-1.2", "", {"BUSNUM": "001", "DEVNUM": "005"}))
    src.callback("remove", UsbRecord("1-1.3", "", {"BUSNUM": "001", "DEVNUM": "006"}))
    assert svc.removed == ["usb:001,005"]
    d.stop()
    assert src.stopped


def test_discovery_survives_bad_events():
    class Broken(FakeService):
        def camera_added(self, info):
            raise RuntimeError("boom")

    src = FakeSource([gopro()])
    d = Discovery(Broken(), source=src)
    d.start()                                       # must not raise
    src.callback("add", gopro("1-1.1", dev="9"))     # must not raise either


def test_udev_source_enumerates_without_error():
    """Smoke test against the real udev database (may be empty in containers)."""
    import pytest
    pyudev = pytest.importorskip("pyudev")
    from footage_offload.discovery import UdevUsbSource
    try:
        records = UdevUsbSource(pyudev.Context()).list()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"udev not usable here: {e}")
    for r in records:
        assert isinstance(r.properties, dict)
