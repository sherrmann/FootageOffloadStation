"""Diagnostics bundle: logs, versions, device summary and history as a zip.

The zip never contains footage or secrets. It is written to a temporary
file on disk (the journal of a week can be large and the Pi Zero has
512 MB of RAM); command output is streamed to disk as well. Every step has
a timeout, and a failing step is recorded as text in the zip instead of
raising.
"""
import ctypes
import ctypes.util
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from . import APP_ID, APP_NAME, __version__, paths
from .config import SECRET_KEYS

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 30
JOURNAL_TIMEOUT_S = 120

DPKG_PACKAGES = (
    APP_ID, "python3", "python3-flask", "python3-werkzeug", "python3-pyudev",
    "python3-paho-mqtt", "libgphoto2-6", "libgphoto2-6t64", "libgphoto2-port12",
    "libgphoto2-port12t64", "gphoto2", "samba", "smbclient", "rsync", "exfatprogs",
    "network-manager", "systemd-timesyncd", "chrony", "raspberrypi-kernel", "linux-image-rpi-v6",
)


def _tmp_dir():
    """Prefer the state directory (SD card) over /tmp, which may be RAM."""
    for d in (paths.STATE_DIR, tempfile.gettempdir()):
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    return None


class _Bundle:
    def __init__(self, zf, work_dir):
        self.zf = zf
        self.work_dir = work_dir
        self.problems = []

    def text(self, name, text):
        self.zf.writestr(name, text)

    def json(self, name, data):
        self.zf.writestr(name, json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")

    def step(self, name, func):
        """Run a step; record failures as text instead of raising."""
        try:
            func()
        except Exception as e:  # noqa: BLE001 - diagnostics must never fail as a whole
            log.warning("diagnostics step %s failed: %s", name, e)
            self.problems.append(f"{name}: {type(e).__name__}: {e}")
            try:
                self.text(f"errors/{name}.txt", f"{type(e).__name__}: {e}\n")
            except Exception:  # noqa: BLE001
                pass

    def command(self, name, argv, timeout=COMMAND_TIMEOUT_S):
        """Run argv with output streamed to a temporary file, then add it."""
        fd, out_path = tempfile.mkstemp(prefix=".diag-", dir=self.work_dir)
        started = time.monotonic()
        try:
            with os.fdopen(fd, "w+b") as out:
                out.write(("$ " + " ".join(argv) + "\n").encode())
                out.flush()
                try:
                    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=out,
                                          stderr=subprocess.STDOUT, timeout=timeout,
                                          check=False, env=_command_env())
                    footer = f"\n[exit status {proc.returncode}"
                except FileNotFoundError:
                    footer = "\n[command not found"
                except subprocess.TimeoutExpired:
                    footer = f"\n[timed out after {timeout} s"
                except OSError as e:
                    footer = f"\n[could not run: {e}"
                footer += f", {time.monotonic() - started:.1f} s]\n"
                out.seek(0, os.SEEK_END)
                out.write(footer.encode())
            self.zf.write(out_path, name)
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    def file(self, name, path):
        try:
            self.zf.write(path, name)
        except OSError as e:
            self.text(name, f"[could not read {path}: {e.strerror or e}]\n")


def _command_env():
    env = dict(os.environ)
    env["LC_ALL"] = "C.UTF-8"
    env["SYSTEMD_PAGER"] = ""
    env["SYSTEMD_COLORS"] = "0"
    return env


def libgphoto2_version():
    """Version of the loaded libgphoto2 as text, or None."""
    names = [ctypes.util.find_library("gphoto2"), "libgphoto2.so.6"]
    for name in names:
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
            func = lib.gp_library_version
        except (OSError, AttributeError):
            continue
        func.restype = ctypes.POINTER(ctypes.c_char_p)
        func.argtypes = [ctypes.c_int]
        arr = func(0)               # GP_VERSION_SHORT
        if not arr:
            return None
        parts = []
        for i in range(16):
            item = arr[i]
            if item is None:
                break
            parts.append(item.decode("utf-8", "replace"))
        return " ".join(parts) or None
    return None


def _package_version(name):
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:  # noqa: BLE001
        return None


def redacted_config(config):
    data = config.snapshot()
    for key in SECRET_KEYS:
        if key in data:
            data[key] = "<set>" if data[key] else ""
    return data


def build(config, state=None, service=None, dest_dir=None):
    """Write the diagnostics zip and return its path. The caller deletes it.

    `service` (optional) is used for `busy()` and the current status.
    """
    work_dir = dest_dir or _tmp_dir()
    fd, zip_path = tempfile.mkstemp(prefix=".diagnostics-", suffix=".zip", dir=work_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            b = _Bundle(zf, os.path.dirname(zip_path))
            _fill(b, config, state, service)
    except BaseException:
        try:
            os.unlink(zip_path)
        except OSError:
            pass
        raise
    return zip_path


def _fill(b, config, state, service):
    now = time.strftime("%Y-%m-%d %H:%M:%S %z")
    b.text("README.txt",
           f"{APP_NAME} diagnostics\nCreated: {now}\nVersion: {__version__}\n\n"
           "Contains logs, versions, configuration without secrets, USB and\n"
           "storage information and the transfer history. No footage.\n")

    def versions():
        info = {
            "package": __version__,
            "python": sys.version,
            "flask": _package_version("flask"),
            "werkzeug": _package_version("werkzeug"),
            "pyudev": _package_version("pyudev"),
            "paho-mqtt": _package_version("paho-mqtt"),
            "platform": platform.platform(),
        }
        try:
            info["libgphoto2"] = libgphoto2_version()
        except Exception as e:  # noqa: BLE001
            info["libgphoto2"] = f"unavailable: {e}"
        b.json("versions.json", info)
    b.step("versions", versions)
    b.step("uname", lambda: b.command("system/uname.txt", ["uname", "-a"]))
    b.step("os_release", lambda: b.file("system/os-release.txt", "/etc/os-release"))
    b.step("dpkg", lambda: b.command(
        "system/packages.txt", ["dpkg-query", "-W", "-f", "${Package} ${Version} ${db:Status-Abbrev}\\n",
                                *DPKG_PACKAGES]))
    b.step("config", lambda: b.json("config.json", redacted_config(config)))

    def status():
        if service is not None and getattr(service, "status", None) is not None:
            b.json("status.json", service.status.snapshot())
    b.step("status", status)

    b.step("journal", lambda: b.command(
        "logs/journal.txt",
        ["journalctl", "-u", APP_ID, "-u", f"{APP_ID}-helper", "--since", "-7 days",
         "--no-pager", "-o", "short-iso"], timeout=JOURNAL_TIMEOUT_S))

    def gphoto():
        busy = False
        if service is not None:
            try:
                busy = bool(service.busy())
            except Exception:  # noqa: BLE001
                busy = False
        if busy:
            b.text("camera/gphoto2-summary.txt",
                   "Skipped: a transfer was running, the camera must not be disturbed.\n")
        elif shutil.which("gphoto2") is None:
            b.text("camera/gphoto2-summary.txt", "gphoto2 command line tool not installed.\n")
        else:
            b.command("camera/gphoto2-summary.txt", ["gphoto2", "--summary"], timeout=60)
    b.step("gphoto2", gphoto)

    b.step("lsusb", lambda: b.command("system/lsusb.txt", ["lsusb"]))
    b.step("lsusb_tree", lambda: b.command("system/lsusb-tree.txt", ["lsusb", "-t"]))
    b.step("df", lambda: b.command("system/df.txt", ["df", "-h"]))
    b.step("mounts", lambda: b.file("system/mounts.txt", "/proc/mounts"))
    b.step("throttled", lambda: b.command("system/throttled.txt", ["vcgencmd", "get_throttled"]))
    b.step("uptime", lambda: b.file("system/uptime.txt", "/proc/uptime"))
    b.step("meminfo", lambda: b.file("system/meminfo.txt", "/proc/meminfo"))

    def history():
        if state is None:
            return
        b.json("history/runs.json", state.runs(limit=1000))
        b.json("history/devices.json", state.devices())
    b.step("history", history)

    b.text("errors/summary.txt", "\n".join(b.problems) + "\n" if b.problems else "No failed steps.\n")


def download_name(timestamp=None):
    return time.strftime(f"{APP_ID}-diagnostics-%Y-%m-%d_%H-%M.zip", time.localtime(timestamp))

