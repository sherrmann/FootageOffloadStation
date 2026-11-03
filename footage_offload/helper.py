"""Root helper: a fixed list of privileged actions for the service.

Run as root by footage-offload-helper.service:

    python3 -m footage_offload.helper

Protocol (docs/ARCHITECTURE.md): Unix stream socket paths.HELPER_SOCKET,
mode 0660, group footage-offload. Only peers whose SO_PEERCRED uid is the
footage-offload user or root are served. One request per connection, one
line of JSON each way.

Every argument is validated against a strict pattern and every command is
run with an argument list (never through a shell).

Design decisions worth knowing:

* eject_stick: after `sync` and unmounting, the USB device is powered off
  with `udisksctl power-off -b /dev/sdX` when udisks2 is installed and
  running (it flushes the drive cache with SCSI SYNCHRONIZE CACHE / STOP
  UNIT first). Otherwise we write 1 to the `remove` attribute of the USB
  device in sysfs, which detaches it logically; the stick has to be
  replugged to come back, which is what "eject" means. Writing to the SCSI
  `device/delete` attribute is not used: it leaves the USB device bound and
  is the more abrupt of the two.
* wifi_connect: the PSK is never put on a command line (visible in
  /proc/*/cmdline). The connection profile is created with
  `wifi-sec.psk-flags 2` ("not saved"), so NetworkManager never writes the
  password to disk either, and the secret is handed to
  `nmcli connection up ... passwd-file FILE` through a 0600 temporary file
  that is deleted straight afterwards.
* samba: the share lives in its own file; smb.conf only gets a marked
  include line at its end, so removing the share is clean. Guest access
  relies on `map to guest = bad user` in [global], which is Debian's
  default smb.conf.
"""
import errno
import grp
import json
import logging
import os
import pwd
import re
import shutil
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading

from . import APP_ID, paths

log = logging.getLogger("footage_offload.helper")

SERVICE_USER = APP_ID
MAX_REQUEST = 64 * 1024
FSTYPES = ("ext4", "exfat", "vfat")
WIFI_CON_NAME = "footage-offload-camera"
WIFI_IFACE = "wlan0"
SAMBA_INCLUDE_MARK = f"# {APP_ID}: footage share (managed automatically, do not edit)"

SYSFS_USB_RE = re.compile(r"^/sys/bus/usb/devices/(\d+-\d+(?:\.\d+)*)$")
USB_DEVICE_NAME_RE = re.compile(r"^\d+-\d+(?:\.\d+)*$")
DEVICE_RE = re.compile(r"^/dev/(sd[a-z]+)(\d*)$")
SSID_MAX_BYTES = 32


class ValidationError(Exception):
    """A request argument was refused."""


class ActionError(Exception):
    """The action itself failed."""


# --- peer credentials --------------------------------------------------------

def peer_credentials(sock):
    """(pid, uid, gid) of the process on the other end of a Unix socket."""
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", data)


def allowed_uids(user=SERVICE_USER):
    uids = {0}
    try:
        uids.add(pwd.getpwnam(user).pw_uid)
    except KeyError:
        log.warning("user %s does not exist; only root may use the helper", user)
    return uids


def peer_allowed(uid, allowed):
    return isinstance(uid, int) and uid >= 0 and uid in allowed


# --- command runner ----------------------------------------------------------

def run_command(argv, timeout=60, input_text=None):
    """Run argv (a list, never a shell). Returns stdout; raises ActionError."""
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a list of str")
    log.info("run: %s", " ".join(argv))
    try:
        proc = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                              timeout=timeout, check=False, shell=False)
    except FileNotFoundError as e:
        raise ActionError(f"{argv[0]} not installed") from e
    except subprocess.TimeoutExpired as e:
        raise ActionError(f"{argv[0]} timed out") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise ActionError(f"{' '.join(argv[:2])} failed ({proc.returncode})"
                          + (f": {detail[-1]}" if detail else ""))
    return proc.stdout


def parse_mountinfo(text):
    """[(major_minor, mount_point, fstype, source)] from /proc/self/mountinfo."""
    out = []
    for line in text.splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        lf, rf = left.split(), right.split()
        if len(lf) < 5 or len(rf) < 2:
            continue
        out.append((lf[2], _unescape_mount(lf[4]), rf[0], _unescape_mount(rf[1])))
    return out


def _unescape_mount(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def parse_blkid_export(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# --- the helper --------------------------------------------------------------

class Helper:
    """The action handlers. Everything touching the system is injectable."""

    def __init__(self, runner=run_command, root=None, stick_mount=None, probe_mount=None,
                 config_dir=None, service_user=SERVICE_USER, which=shutil.which,
                 is_block=None, ismount=os.path.ismount):
        self.run = runner
        self.root = (os.environ.get("FOS_ROOT", "") if root is None else root).rstrip("/")
        self.stick_mount = stick_mount or paths.STICK_MOUNT
        self.probe_mount = probe_mount or paths.PROBE_MOUNT
        self.config_dir = config_dir or paths.CONFIG_DIR
        self.service_user = service_user
        self.which = which
        self._is_block = is_block or self._real_is_block
        self.ismount = ismount
        self.locks = {"stick": threading.Lock(), "wifi": threading.Lock(),
                      "samba": threading.Lock()}
        self.actions = {
            "deauthorize_usb": (self.deauthorize_usb, None, {"sysfs_path"}),
            "probe_stick": (self.probe_stick, "stick", {"device"}),
            "mount_stick": (self.mount_stick, "stick", {"device"}),
            "eject_stick": (self.eject_stick, "stick", set()),
            "shutdown": (self.shutdown, None, set()),
            "reboot": (self.reboot, None, set()),
            "samba": (self.samba, "samba", {"enabled"}),
            "wifi_connect": (self.wifi_connect, "wifi", {"ssid", "password"}),
            "wifi_disconnect": (self.wifi_disconnect, "wifi", set()),
        }

    # paths ---------------------------------------------------------------------
    def sys(self, path):
        """A system path (/sys, /dev, /proc, /etc) under the test root."""
        return self.root + path

    @staticmethod
    def _real_is_block(path):
        try:
            return stat.S_ISBLK(os.stat(path).st_mode)
        except OSError:
            return False

    # dispatch ------------------------------------------------------------------
    def handle(self, request):
        """Dict request -> dict reply. Never raises."""
        try:
            if not isinstance(request, dict):
                raise ValidationError("request must be an object")
            action = request.get("action")
            args = request.get("args", {})
            if args is None:
                args = {}
            if not isinstance(action, str) or action not in self.actions:
                raise ValidationError(f"unknown action {action!r}"[:100])
            if not isinstance(args, dict):
                raise ValidationError("args must be an object")
            func, lock_name, expected = self.actions[action]
            unknown = set(args) - expected
            missing = expected - set(args)
            if unknown or missing:
                raise ValidationError(f"{action}: expected arguments {sorted(expected)}")
            log.info("action %s", action)
            if lock_name:
                with self.locks[lock_name]:
                    result = func(**args)
            else:
                result = func(**args)
            return {"ok": True, "result": result}
        except ValidationError as e:
            log.warning("refused: %s", e)
            return {"ok": False, "error": f"invalid request: {e}"}
        except ActionError as e:
            log.warning("failed: %s", e)
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - the helper must answer and survive
            log.exception("internal error")
            return {"ok": False, "error": f"internal error: {type(e).__name__}"}

    # validation ------------------------------------------------------------------
    def validate_usb_sysfs(self, sysfs_path):
        """Return the resolved device directory of a USB device (not a hub)."""
        if not isinstance(sysfs_path, str):
            raise ValidationError("sysfs_path must be a string")
        m = SYSFS_USB_RE.match(sysfs_path)
        if not m:
            raise ValidationError("sysfs_path must look like /sys/bus/usb/devices/1-1.2")
        resolved = os.path.realpath(self.sys(sysfs_path))
        devices_root = os.path.realpath(self.sys("/sys/devices")) + "/"
        if not resolved.startswith(devices_root):
            raise ValidationError("sysfs_path does not resolve under /sys/devices")
        if os.path.basename(resolved) != m.group(1):
            raise ValidationError("sysfs_path resolves to an unexpected node")
        authorized = os.path.join(resolved, "authorized")
        if not os.path.isfile(authorized) or os.path.islink(authorized):
            raise ValidationError("not a USB device (no authorized attribute)")
        try:
            with open(os.path.join(resolved, "bDeviceClass")) as f:
                if f.read().strip().lower() == "09":
                    raise ValidationError("refusing to de-authorise a USB hub")
        except OSError:
            pass
        return resolved

    def validate_device(self, device):
        """Check `device` is /dev/sdXN, a block device, on USB. Returns its name."""
        if not isinstance(device, str):
            raise ValidationError("device must be a string")
        m = DEVICE_RE.match(device)
        if not m:
            raise ValidationError("device must look like /dev/sda1")
        name = m.group(1) + m.group(2)
        if not self._is_block(self.sys(device)):
            raise ValidationError(f"{device} is not a block device")
        node = os.path.realpath(self.sys(f"/sys/class/block/{name}"))
        if "/usb" not in node[len(self.root):]:
            raise ValidationError(f"{device} is not a USB device")
        return name

    # USB -------------------------------------------------------------------------
    def deauthorize_usb(self, sysfs_path):
        resolved = self.validate_usb_sysfs(sysfs_path)
        try:
            with open(os.path.join(resolved, "authorized"), "w") as f:
                f.write("0")
        except OSError as e:
            raise ActionError(f"de-authorising {sysfs_path} failed: {e.strerror}") from e
        log.info("de-authorised %s", sysfs_path)
        return {"deauthorized": True}

    # stick ---------------------------------------------------------------------
    def _blkid(self, device):
        info = parse_blkid_export(self.run(["blkid", "-p", "-o", "export", device], timeout=30))
        fstype = info.get("TYPE", "")
        if fstype not in FSTYPES:
            raise ActionError(f"{device}: unsupported file system {fstype or 'none'}")
        return fstype, info.get("LABEL", ""), info.get("UUID", "")

    def _mounts(self):
        try:
            with open(self.sys("/proc/self/mountinfo")) as f:
                return parse_mountinfo(f.read())
        except OSError:
            return []

    def _device_rdev(self, device):
        try:
            st = os.stat(self.sys(device))
            return f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
        except OSError:
            return None

    def _mount_points_of(self, device):
        rdev = self._device_rdev(device)
        return [mp for mm, mp, _fs, src in self._mounts()
                if src == device or (rdev and rdev != "0:0" and mm == rdev)]

    def _source_at(self, mount_point):
        for _mm, mp, _fs, src in reversed(self._mounts()):
            if mp == mount_point:
                return src
        return None

    def _marker_in(self, directory):
        path = os.path.join(directory, paths.MARKER_NAME)
        try:
            return stat.S_ISREG(os.lstat(path).st_mode)
        except OSError:
            return False

    def _service_ids(self):
        try:
            pw = pwd.getpwnam(self.service_user)
        except KeyError as e:
            raise ActionError(f"user {self.service_user} does not exist") from e
        gid = pw.pw_gid
        try:
            gid = grp.getgrnam(self.service_user).gr_gid
        except KeyError:
            pass
        return pw.pw_uid, gid

    def probe_stick(self, device):
        self.validate_device(device)
        fstype, label, uuid = self._blkid(device)
        mounted = self._mount_points_of(device)
        if mounted:
            # Already mounted (by us or a desktop): look there, do not mount twice.
            return {"marker": self._marker_in(mounted[0]), "fstype": fstype,
                    "label": label, "uuid": uuid}
        os.makedirs(self.probe_mount, mode=0o700, exist_ok=True)
        if self.ismount(self.probe_mount):
            self.run(["umount", self.probe_mount], timeout=60)
        self.run(["mount", "-t", fstype, "-o", "ro,nosuid,nodev,noexec", device,
                  self.probe_mount], timeout=60)
        try:
            marker = self._marker_in(self.probe_mount)
        finally:
            self.run(["umount", self.probe_mount], timeout=60)
        return {"marker": marker, "fstype": fstype, "label": label, "uuid": uuid}

    def mount_options(self, fstype, uid, gid):
        base = "rw,nosuid,nodev,noexec,noatime"
        if fstype == "ext4":
            return base
        return f"{base},uid={uid},gid={gid},fmask=0022,dmask=0022"

    def mount_stick(self, device):
        self.validate_device(device)
        fstype, _label, _uuid = self._blkid(device)
        uid, gid = self._service_ids()
        if self._source_at(self.stick_mount) == device and self.ismount(self.stick_mount):
            if not self._marker_in(self.stick_mount):
                raise ActionError(f"{device}: marker file missing")
            return {"mounted": True, "fstype": fstype}
        # Desktop automounters and stale mounts: unmount every other mount of
        # the device, and whatever occupies our mount point.
        for mp in self._mount_points_of(device):
            self.run(["umount", mp], timeout=120)
        if self.ismount(self.stick_mount):
            self.run(["umount", self.stick_mount], timeout=120)
        os.makedirs(self.stick_mount, mode=0o755, exist_ok=True)
        self.run(["mount", "-t", fstype, "-o", self.mount_options(fstype, uid, gid), device,
                  self.stick_mount], timeout=120)
        if not self._marker_in(self.stick_mount):
            self.run(["umount", self.stick_mount], timeout=120)
            raise ActionError(f"{device}: marker file missing; not adopted")
        if fstype == "ext4":
            try:
                os.chown(self.stick_mount, uid, gid)
                os.chmod(self.stick_mount, 0o755)
            except OSError as e:
                raise ActionError(f"chown of stick root failed: {e.strerror}") from e
        log.info("mounted %s (%s) at %s", device, fstype, self.stick_mount)
        return {"mounted": True, "fstype": fstype}

    def _usb_device_of(self, block_name):
        """sysfs directory of the USB device that carries block device `block_name`."""
        node = os.path.realpath(self.sys(f"/sys/class/block/{block_name}"))
        parts = node.split("/")
        for i in range(len(parts) - 1, 0, -1):
            if USB_DEVICE_NAME_RE.match(parts[i]):
                return "/".join(parts[:i + 1])
        return None

    def eject_stick(self):
        device = self._source_at(self.stick_mount)
        if not device or not self.ismount(self.stick_mount):
            raise ActionError("no stick mounted")
        m = DEVICE_RE.match(device)
        if not m:
            raise ActionError(f"unexpected device {device} at the stick mount point")
        disk = "/dev/" + m.group(1)
        self.run(["sync"], timeout=300)
        self.run(["umount", self.stick_mount], timeout=300)
        for mp in self._mount_points_of(device):
            self.run(["umount", mp], timeout=300)
        self.run(["sync"], timeout=300)
        if self.which("udisksctl"):
            try:
                self.run(["udisksctl", "power-off", "--no-user-interaction", "-b", disk],
                         timeout=60)
                return {"ejected": True, "method": "udisksctl"}
            except ActionError as e:
                log.info("udisksctl power-off failed (%s); using sysfs", e)
        usb = self._usb_device_of(m.group(1))
        if not usb or not usb.startswith(os.path.realpath(self.sys("/sys/devices")) + "/"):
            raise ActionError(f"cannot find the USB device of {disk}")
        try:
            with open(os.path.join(usb, "remove"), "w") as f:
                f.write("1")
        except OSError as e:
            raise ActionError(f"powering off {disk} failed: {e.strerror}") from e
        return {"ejected": True, "method": "sysfs"}

    # system ----------------------------------------------------------------------
    def shutdown(self):
        self.run(["systemctl", "--no-block", "poweroff"], timeout=30)
        return {}

    def reboot(self):
        self.run(["systemctl", "--no-block", "reboot"], timeout=30)
        return {}

    # samba -----------------------------------------------------------------------
    @property
    def samba_include(self):
        # Must live in a root-only directory: smbd runs as root and a share
        # definition can run commands (root preexec), so the service user
        # must never be able to write this file. /etc/footage-offload is
        # owned by the service user and therefore not suitable.
        return self.sys("/etc/samba/footage-offload.conf")

    def share_text(self):
        user = self.service_user
        return (f"# Written by {APP_ID}; changes are overwritten.\n"
                "[footage]\n"
                "   comment = Footage Offload Station\n"
                f"   path = {self.stick_mount}\n"
                "   read only = yes\n"
                "   guest ok = yes\n"
                "   guest only = yes\n"
                "   browseable = yes\n"
                f"   force user = {user}\n"
                f"   force group = {user}\n")

    def samba(self, enabled):
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be true or false")
        smb_conf = self.sys("/etc/samba/smb.conf")
        include_line = f"include = {self.samba_include}"
        try:
            with open(smb_conf) as f:
                lines = f.read().splitlines()
        except FileNotFoundError as e:
            raise ActionError("samba is not installed (no /etc/samba/smb.conf)") from e
        kept = [ln for ln in lines
                if ln.strip() != SAMBA_INCLUDE_MARK and ln.strip() != include_line]
        while kept and not kept[-1].strip():
            kept.pop()
        if enabled:
            os.makedirs(self.config_dir, exist_ok=True)
            _atomic_write(self.samba_include, self.share_text(), 0o644)
            kept += ["", SAMBA_INCLUDE_MARK, include_line]
        else:
            try:
                os.unlink(self.samba_include)
            except FileNotFoundError:
                pass
        if kept != lines:
            _atomic_write(smb_conf, "\n".join(kept) + "\n", 0o644)
        if enabled:
            self.run(["systemctl", "enable", "--now", "smbd"], timeout=120)
            self.run(["systemctl", "reload", "smbd"], timeout=60)
        else:
            self.run(["systemctl", "try-reload-or-restart", "smbd"], timeout=60)
        return {"enabled": enabled}

    # Wi-Fi -------------------------------------------------------------------------
    @staticmethod
    def validate_wifi(ssid, password):
        if not isinstance(ssid, str) or not isinstance(password, str):
            raise ValidationError("ssid and password must be strings")
        raw = ssid.encode("utf-8")
        if not 1 <= len(raw) <= SSID_MAX_BYTES:
            raise ValidationError("ssid must be 1 to 32 bytes")
        if any(ord(c) < 32 or ord(c) == 127 for c in ssid):
            raise ValidationError("ssid contains control characters")
        if re.fullmatch(r"[0-9A-Fa-f]{64}", password):
            return
        if not 8 <= len(password) <= 63 or not all(32 <= ord(c) <= 126 for c in password):
            raise ValidationError("password must be 8 to 63 printable ASCII characters")

    def wifi_connect(self, ssid, password):
        self.validate_wifi(ssid, password)
        self._delete_connection()
        self.run(["nmcli", "connection", "add", "type", "wifi", "ifname", WIFI_IFACE,
                  "con-name", WIFI_CON_NAME, "ssid", ssid,
                  "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk-flags", "2",
                  "ipv4.method", "auto", "ipv4.never-default", "yes",
                  "ipv4.ignore-auto-dns", "yes", "ipv6.method", "disabled",
                  "connection.autoconnect", "no"], timeout=30)
        fd, secret_file = tempfile.mkstemp(prefix="fos-wifi-", text=True)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(f"802-11-wireless-security.psk:{password}\n")
            self.run(["nmcli", "--wait", "60", "connection", "up", WIFI_CON_NAME,
                      "passwd-file", secret_file], timeout=90)
        except ActionError:
            self._delete_connection()
            raise
        finally:
            try:
                os.unlink(secret_file)
            except OSError:
                pass
        return {"connected": True}

    def _delete_connection(self):
        try:
            self.run(["nmcli", "connection", "delete", WIFI_CON_NAME], timeout=30)
            return True
        except ActionError:
            return False        # did not exist

    def wifi_disconnect(self):
        return {"removed": self._delete_connection()}


def _atomic_write(path, text, mode):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- server ---------------------------------------------------------------------

class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        server = self.server
        try:
            pid, uid, _gid = peer_credentials(sock)
        except OSError:
            return
        if not peer_allowed(uid, server.allowed):
            log.warning("refused peer pid %d uid %d", pid, uid)
            _send(sock, {"ok": False, "error": "permission denied"})
            return
        sock.settimeout(10)
        data = b""
        try:
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_REQUEST:
                    _send(sock, {"ok": False, "error": "request too large"})
                    return
        except OSError:
            return
        try:
            request = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
        except ValueError:
            _send(sock, {"ok": False, "error": "invalid request: malformed JSON"})
            return
        sock.settimeout(None)
        _send(sock, server.helper.handle(request))


def _send(sock, reply):
    try:
        sock.sendall(json.dumps(reply).encode("utf-8") + b"\n")
    except OSError:
        pass


class HelperServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path, helper, allowed, group=SERVICE_USER):
        self.helper = helper
        self.allowed = allowed
        directory = os.path.dirname(socket_path)
        os.makedirs(directory, mode=0o755, exist_ok=True)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        old = os.umask(0o117)
        try:
            super().__init__(socket_path, _Handler)
        finally:
            os.umask(old)
        try:
            gid = grp.getgrnam(group).gr_gid
            os.chown(socket_path, 0, gid)
        except KeyError:
            log.warning("group %s missing; socket stays root-only", group)
        except OSError as e:
            if e.errno != errno.EPERM:
                raise
        os.chmod(socket_path, 0o660)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    if os.geteuid() != 0 and not os.environ.get("FOS_ROOT"):
        log.error("the helper must run as root")
        return 1
    server = HelperServer(paths.HELPER_SOCKET, Helper(), allowed_uids())
    log.info("listening on %s", paths.HELPER_SOCKET)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            os.unlink(paths.HELPER_SOCKET)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
