"""Root helper: argument validation, handlers with a fake command runner,
peer credential checks and the socket protocol with HelperClient."""
import json
import os
import socket
import threading

import pytest

from footage_offload import helper as h
from footage_offload.helper_client import HelperClient, HelperError, RecordingHelper
from footage_offload.paths import MARKER_NAME

USB_DEV = "platform/soc/20980000.usb/usb1/1-1/1-1.2"
STICK_USB = "platform/soc/20980000.usb/usb1/1-1/1-1.5"
STICK_BLOCK = STICK_USB + "/1-1.5:1.0/host0/target0:0:0/0:0:0:0/block/sda"


class FakeRunner:
    """Records argv lists; answers blkid; simulates mount/umount."""

    def __init__(self, helper_ref, fstype="exfat", marker=True):
        self.calls = []
        self.fstype = fstype
        self.marker = marker
        self.fail = {}             # argv[0] or "argv0 argv1" -> message
        self.helper_ref = helper_ref

    def __call__(self, argv, timeout=60, input_text=None):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        self.calls.append(argv)
        for key in (" ".join(argv[:2]), argv[0]):
            if key in self.fail:
                raise h.ActionError(self.fail[key])
        if argv[0] == "blkid":
            return f"DEVNAME={argv[-1]}\nLABEL=FOOTAGE\nUUID=1234-ABCD\nTYPE={self.fstype}\n"
        if argv[0] == "mount" and self.marker:
            with open(os.path.join(argv[-1], MARKER_NAME), "w"):
                pass
        if argv[0] == "umount":
            try:
                os.unlink(os.path.join(argv[-1], MARKER_NAME))
            except OSError:
                pass
        return ""

    def argv0(self):
        return [c[0] if c[0] != "systemctl" else " ".join(c[:3]) for c in self.calls]


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    dev = r / "sys/devices" / USB_DEV
    dev.mkdir(parents=True)
    (dev / "authorized").write_text("1\n")
    (dev / "bDeviceClass").write_text("00\n")
    hub = r / "sys/devices/platform/soc/20980000.usb/usb1/1-1"
    (hub / "authorized").write_text("1\n")
    (hub / "bDeviceClass").write_text("09\n")
    stick_usb = r / "sys/devices" / STICK_USB
    (stick_usb / "1-1.5:1.0/host0/target0:0:0/0:0:0:0/block/sda/sda1").mkdir(parents=True)
    (stick_usb / "remove").write_text("")
    bus = r / "sys/bus/usb/devices"
    bus.mkdir(parents=True)
    os.symlink(f"../../../devices/{USB_DEV}", bus / "1-1.2")
    os.symlink("../../../devices/platform/soc/20980000.usb/usb1/1-1", bus / "1-1")
    os.symlink("/etc", bus / "1-1.3")                                   # escapes /sys/devices
    os.symlink(f"../../../devices/{USB_DEV}", bus / "1-1.4")            # wrong node name
    os.symlink("../../../devices/platform/soc/nothing/1-1.6", bus / "1-1.6")
    (r / "sys/devices/platform/soc/nothing/1-1.6").mkdir(parents=True)  # no authorized file
    blk = r / "sys/class/block"
    blk.mkdir(parents=True)
    os.symlink(f"../../devices/{STICK_BLOCK}/sda1", blk / "sda1")
    os.symlink(f"../../devices/{STICK_BLOCK}", blk / "sda")
    (r / "sys/devices/platform/ata/host1/block/sdb/sdb1").mkdir(parents=True)
    os.symlink("../../devices/platform/ata/host1/block/sdb/sdb1", blk / "sdb1")
    (r / "dev").mkdir()
    for name in ("sda", "sda1", "sdb1"):
        (r / "dev" / name).write_text("")
    (r / "proc/self").mkdir(parents=True)
    (r / "proc/self/mountinfo").write_text(
        "22 1 179:2 / / rw,noatime shared:1 - ext4 /dev/mmcblk0p2 rw\n")
    (r / "etc/samba").mkdir(parents=True)
    (r / "etc/samba/smb.conf").write_text("[global]\n   map to guest = bad user\n")
    return r


@pytest.fixture
def helper(root, tmp_path):
    mounted = set()
    hp = h.Helper(runner=None, root=str(root), stick_mount=str(tmp_path / "stick"),
                  probe_mount=str(tmp_path / "probe"), config_dir=str(root / "etc/footage-offload"),
                  service_user="root", which=lambda name: None,
                  is_block=lambda p: os.path.exists(p), ismount=lambda p: p in mounted)
    hp.run = FakeRunner(hp)
    hp.mounted = mounted
    return hp


# --- dispatch -------------------------------------------------------------------

def test_unknown_action_and_bad_args(helper):
    assert helper.handle({"action": "rm_rf", "args": {}})["ok"] is False
    assert helper.handle("nope")["ok"] is False
    assert helper.handle({"action": "deauthorize_usb", "args": []})["ok"] is False
    r = helper.handle({"action": "deauthorize_usb", "args": {"sysfs_path": "/sys/bus/usb/devices/1-1.2",
                                                              "extra": 1}})
    assert r["ok"] is False and "expected arguments" in r["error"]
    assert helper.handle({"action": "deauthorize_usb", "args": {}})["ok"] is False
    assert helper.handle({"action": "eject_stick"})["ok"] is False   # no stick mounted
    assert helper.run.calls == []


def test_internal_errors_are_reported_not_raised(helper):
    def boom():
        raise RuntimeError("x")
    helper.actions["shutdown"] = (boom, None, set())
    r = helper.handle({"action": "shutdown", "args": {}})
    assert r == {"ok": False, "error": "internal error: RuntimeError"}


# --- de-authorise -----------------------------------------------------------------

def test_deauthorize_writes_zero(helper, root):
    r = helper.handle({"action": "deauthorize_usb", "args": {"sysfs_path": "/sys/bus/usb/devices/1-1.2"}})
    assert r == {"ok": True, "result": {"deauthorized": True}}
    assert (root / "sys/devices" / USB_DEV / "authorized").read_text() == "0"


@pytest.mark.parametrize("path", [
    "/sys/bus/usb/devices/1-1.3",          # symlink out of /sys/devices
    "/sys/bus/usb/devices/1-1.4",          # resolves to a differently named node
    "/sys/bus/usb/devices/1-1.6",          # no authorized attribute
    "/sys/bus/usb/devices/1-1",            # a hub
    "/sys/bus/usb/devices/1-1.9",          # does not exist
    "/sys/bus/usb/devices/usb1",
    "/sys/bus/usb/devices/1-1.2/../1-1.2",
    "/sys/bus/usb/devices/1-1.2/",
    "/sys/bus/usb/devices/1-1.2\n",
    "/sys/devices/platform/soc/20980000.usb/usb1/1-1/1-1.2",
    "sys/bus/usb/devices/1-1.2",
    "/sys/bus/usb/devices/1-1:1.0",
    "",
    None,
    5,
])
def test_deauthorize_refuses(helper, root, path):
    r = helper.handle({"action": "deauthorize_usb", "args": {"sysfs_path": path}})
    assert r["ok"] is False, path
    assert (root / "sys/devices" / USB_DEV / "authorized").read_text() == "1\n"


# --- stick ----------------------------------------------------------------------------

@pytest.mark.parametrize("device", ["/dev/sdb1", "/dev/sda9", "/dev/mmcblk0p1", "/dev/sda1;rm",
                                    "/dev/../dev/sda1", "/dev/sd", "/dev/sda1 ", "/dev/SDA1",
                                    None, ["/dev/sda1"]])
def test_device_validation(helper, device):
    for action in ("probe_stick", "mount_stick"):
        r = helper.handle({"action": action, "args": {"device": device}})
        assert r["ok"] is False and r["error"].startswith("invalid request"), (action, device)
    assert helper.run.calls == []


def test_probe_mounts_read_only_and_unmounts(helper, tmp_path):
    r = helper.handle({"action": "probe_stick", "args": {"device": "/dev/sda1"}})
    assert r == {"ok": True, "result": {"marker": True, "fstype": "exfat", "label": "FOOTAGE",
                                        "uuid": "1234-ABCD"}}
    mount = [c for c in helper.run.calls if c[0] == "mount"][0]
    assert mount[:4] == ["mount", "-t", "exfat", "-o"]
    assert "ro" in mount[4].split(",") and "noexec" in mount[4]
    assert mount[-2:] == ["/dev/sda1", str(tmp_path / "probe")]
    assert helper.run.calls[-1] == ["umount", str(tmp_path / "probe")]


def test_probe_without_marker(helper):
    helper.run.marker = False
    r = helper.handle({"action": "probe_stick", "args": {"device": "/dev/sda1"}})
    assert r["ok"] and r["result"]["marker"] is False


def test_probe_rejects_unsupported_fs(helper):
    helper.run.fstype = "ntfs"
    r = helper.handle({"action": "probe_stick", "args": {"device": "/dev/sda1"}})
    assert r["ok"] is False and "ntfs" in r["error"]
    assert "mount" not in helper.run.argv0()


def test_probe_of_already_mounted_device_looks_in_place(helper, root, tmp_path):
    elsewhere = tmp_path / "media-pi-STICK"
    elsewhere.mkdir()
    (elsewhere / MARKER_NAME).write_text("")
    with open(root / "proc/self/mountinfo", "a") as f:
        f.write(f"40 22 8:1 / {elsewhere} rw - exfat /dev/sda1 rw\n")
    r = helper.handle({"action": "probe_stick", "args": {"device": "/dev/sda1"}})
    assert r["ok"] and r["result"]["marker"] is True
    assert "mount" not in helper.run.argv0()


def test_mount_unmounts_automount_and_uses_options(helper, root, tmp_path):
    with open(root / "proc/self/mountinfo", "a") as f:
        f.write("40 22 8:1 / /media/pi/MY\\040STICK rw - exfat /dev/sda1 rw\n")
    r = helper.handle({"action": "mount_stick", "args": {"device": "/dev/sda1"}})
    assert r == {"ok": True, "result": {"mounted": True, "fstype": "exfat"}}
    assert ["umount", "/media/pi/MY STICK"] in helper.run.calls
    mount = [c for c in helper.run.calls if c[0] == "mount"][0]
    opts = mount[4].split(",")
    for o in ("rw", "nosuid", "nodev", "noexec", "uid=0", "gid=0", "fmask=0022", "dmask=0022"):
        assert o in opts
    assert mount[-1] == str(tmp_path / "stick")


def test_mount_ext4_chowns_root(helper, tmp_path):
    helper.run.fstype = "ext4"
    r = helper.handle({"action": "mount_stick", "args": {"device": "/dev/sda1"}})
    assert r["ok"], r
    mount = [c for c in helper.run.calls if c[0] == "mount"][0]
    assert "uid=" not in mount[4] and "noexec" in mount[4]


def test_mount_without_marker_is_undone(helper, tmp_path):
    helper.run.marker = False
    r = helper.handle({"action": "mount_stick", "args": {"device": "/dev/sda1"}})
    assert r["ok"] is False and "marker" in r["error"]
    assert helper.run.calls[-1] == ["umount", str(tmp_path / "stick")]


def test_eject_falls_back_to_sysfs_remove(helper, root, tmp_path):
    stick = str(tmp_path / "stick")
    helper.mounted.add(stick)
    with open(root / "proc/self/mountinfo", "a") as f:
        f.write(f"41 22 8:1 / {stick} rw - exfat /dev/sda1 rw\n")
    r = helper.handle({"action": "eject_stick", "args": {}})
    assert r == {"ok": True, "result": {"ejected": True, "method": "sysfs"}}
    assert helper.run.calls[:2] == [["sync"], ["umount", stick]]
    assert (root / "sys/devices" / STICK_USB / "remove").read_text() == "1"


def test_eject_prefers_udisksctl(helper, root, tmp_path):
    stick = str(tmp_path / "stick")
    helper.mounted.add(stick)
    helper.which = lambda name: "/usr/bin/udisksctl"
    with open(root / "proc/self/mountinfo", "a") as f:
        f.write(f"41 22 8:1 / {stick} rw - exfat /dev/sda1 rw\n")
    r = helper.handle({"action": "eject_stick", "args": {}})
    assert r["result"]["method"] == "udisksctl"
    assert ["udisksctl", "power-off", "--no-user-interaction", "-b", "/dev/sda"] in helper.run.calls
    assert (root / "sys/devices" / STICK_USB / "remove").read_text() == ""


def test_eject_without_stick_fails(helper):
    r = helper.handle({"action": "eject_stick", "args": {}})
    assert r["ok"] is False and "no stick" in r["error"]


# --- system, samba, wifi ----------------------------------------------------------------

def test_shutdown_and_reboot(helper):
    assert helper.handle({"action": "shutdown"})["ok"]
    assert helper.handle({"action": "reboot", "args": {}})["ok"]
    assert helper.run.calls == [["systemctl", "--no-block", "poweroff"],
                                ["systemctl", "--no-block", "reboot"]]


def test_samba_enable_and_disable(helper, root, tmp_path):
    conf = root / "etc/samba/smb.conf"
    original = conf.read_text()
    r = helper.handle({"action": "samba", "args": {"enabled": True}})
    assert r == {"ok": True, "result": {"enabled": True}}
    share = (root / "etc/samba/footage-offload.conf").read_text()
    assert "[footage]" in share and "read only = yes" in share and "guest ok = yes" in share
    assert f"path = {tmp_path / 'stick'}" in share and "force user = root" in share
    text = conf.read_text()
    assert text.startswith(original.rstrip("\n"))
    assert text.rstrip("\n").splitlines()[-1] == \
        f"include = {root / 'etc/samba/footage-offload.conf'}"
    # enabling twice does not duplicate the include
    helper.handle({"action": "samba", "args": {"enabled": True}})
    assert conf.read_text() == text
    assert ["systemctl", "enable", "--now", "smbd"] in helper.run.calls
    r = helper.handle({"action": "samba", "args": {"enabled": False}})
    assert r["ok"]
    assert conf.read_text() == original
    assert not (root / "etc/samba/footage-offload.conf").exists()
    assert helper.run.calls[-1] == ["systemctl", "try-reload-or-restart", "smbd"]


@pytest.mark.parametrize("value", ["yes", 1, None, 0])
def test_samba_requires_bool(helper, value):
    assert helper.handle({"action": "samba", "args": {"enabled": value}})["ok"] is False


def test_wifi_connect_keeps_password_off_command_line(helper):
    seen = {}

    original = helper.run

    def runner(argv, timeout=60, input_text=None):
        if argv[:3] == ["nmcli", "--wait", "60"]:
            path = argv[argv.index("passwd-file") + 1]
            st = os.stat(path)
            seen["mode"] = st.st_mode & 0o777
            seen["content"] = open(path).read()
            seen["path"] = path
        return original(argv, timeout, input_text)

    helper.run = runner
    r = helper.handle({"action": "wifi_connect", "args": {"ssid": "GP12345678", "password": "secret-pw-1"}})
    assert r == {"ok": True, "result": {"connected": True}}
    for argv in original.calls:
        assert "secret-pw-1" not in " ".join(argv)
    add = [c for c in original.calls if c[:3] == ["nmcli", "connection", "add"]][0]
    for pair in (("ssid", "GP12345678"), ("ipv4.never-default", "yes"), ("ifname", "wlan0"),
                 ("wifi-sec.psk-flags", "2"), ("connection.autoconnect", "no"),
                 ("con-name", h.WIFI_CON_NAME)):
        assert add[add.index(pair[0]) + 1] == pair[1]
    assert seen["mode"] == 0o600
    assert seen["content"] == "802-11-wireless-security.psk:secret-pw-1\n"
    assert not os.path.exists(seen["path"])


def test_wifi_connect_failure_removes_connection(helper):
    helper.run.fail["nmcli --wait"] = "activation failed"
    r = helper.handle({"action": "wifi_connect", "args": {"ssid": "GP1", "password": "12345678"}})
    assert r["ok"] is False and "activation failed" in r["error"]
    assert helper.run.calls[-1] == ["nmcli", "connection", "delete", h.WIFI_CON_NAME]


@pytest.mark.parametrize("ssid,pw", [
    ("", "12345678"), ("x" * 33, "12345678"), ("a\nb", "12345678"), ("ok", "short"),
    ("ok", "x" * 64), ("ok", "pässwört123"), ("ok", "tab\tinside"), (5, "12345678"),
    ("ok", None), ("ü" * 17, "12345678"),
])
def test_wifi_validation(helper, ssid, pw):
    r = helper.handle({"action": "wifi_connect", "args": {"ssid": ssid, "password": pw}})
    assert r["ok"] is False and r["error"].startswith("invalid request")
    assert helper.run.calls == []


def test_wifi_hex_psk_accepted():
    h.Helper.validate_wifi("GoPro", "a" * 64)


def test_wifi_disconnect(helper):
    assert helper.handle({"action": "wifi_disconnect"}) == {"ok": True, "result": {"removed": True}}
    helper.run.fail["nmcli"] = "unknown connection"
    assert helper.handle({"action": "wifi_disconnect"}) == {"ok": True, "result": {"removed": False}}


# --- helpers --------------------------------------------------------------------------

def test_parse_mountinfo():
    text = ("36 35 98:0 /mnt1 /mnt/with\\040space rw,noatime master:1 - ext3 /dev/root rw\n"
            "40 22 8:1 / /media/x rw shared:3 - vfat /dev/sda1 rw,uid=1000\n")
    assert h.parse_mountinfo(text) == [("98:0", "/mnt/with space", "ext3", "/dev/root"),
                                       ("8:1", "/media/x", "vfat", "/dev/sda1")]


def test_run_command_never_uses_shell():
    with pytest.raises(TypeError):
        h.run_command("echo hi")
    assert h.run_command(["echo", "a;b"]) == "a;b\n"
    with pytest.raises(h.ActionError):
        h.run_command(["false"])
    with pytest.raises(h.ActionError):
        h.run_command(["/nonexistent/binary"])


# --- peer credentials and the socket ----------------------------------------------------

def test_peer_allowed():
    assert h.peer_allowed(0, {0, 999})
    assert h.peer_allowed(999, {0, 999})
    assert not h.peer_allowed(1000, {0, 999})
    assert not h.peer_allowed(-1, {0, 999, -1})
    assert not h.peer_allowed("0", {0})
    assert 0 in h.allowed_uids("no-such-user-hopefully")


def _serve(tmp_path, allowed):
    sock_path = str(tmp_path / "run" / "helper.sock")
    recording = RecordingHelper({"shutdown": {"bye": True}})

    class Dispatch:
        def handle(self, request):
            try:
                return {"ok": True, "result": recording.call(request["action"], **request["args"])}
            except HelperError as e:
                return {"ok": False, "error": str(e)}

    server = h.HelperServer(sock_path, Dispatch(), allowed)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, sock_path, recording


def test_socket_roundtrip_with_client(tmp_path):
    server, path, recording = _serve(tmp_path, {os.getuid()})
    try:
        assert oct(os.stat(path).st_mode & 0o777) == oct(0o660)
        client = HelperClient(socket_path=path, timeout=5)
        assert client.call("shutdown") == {"bye": True}
        recording.fail("samba", "smbd missing")
        with pytest.raises(HelperError, match="smbd missing"):
            client.call("samba", enabled=True)
        assert recording.calls == [("shutdown", {}), ("samba", {"enabled": True})]
        # peer credentials of the client as seen by the server side
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(path)
            pid, uid, gid = h.peer_credentials(s)
            assert uid == os.getuid() and pid > 0
        # malformed JSON
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(path)
            s.sendall(b"{nope\n")
            reply = json.loads(s.makefile().readline())
            assert reply["ok"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_socket_rejects_foreign_peer(tmp_path):
    server, path, recording = _serve(tmp_path, {os.getuid() + 12345})
    try:
        with pytest.raises(HelperError, match="permission denied"):
            HelperClient(socket_path=path, timeout=5).call("shutdown")
        assert recording.calls == []
    finally:
        server.shutdown()
        server.server_close()


def test_client_without_helper(tmp_path):
    with pytest.raises(HelperError, match="cannot reach"):
        HelperClient(socket_path=str(tmp_path / "missing.sock"), timeout=1).call("shutdown")


def test_recording_helper():
    rh = RecordingHelper({"probe_stick": lambda device: {"marker": device.endswith("1")},
                          "eject_stick": HelperError("busy")}, default={"ok": 1})
    assert rh.call("probe_stick", device="/dev/sda1") == {"marker": True}
    with pytest.raises(HelperError):
        rh.call("eject_stick")
    assert rh.call("other") == {"ok": 1}
    assert rh.actions() == ["probe_stick", "eject_stick", "other"]
    assert rh.calls_of("probe_stick") == [{"device": "/dev/sda1"}]
