"""Command line: `footage-offload <command>`. Messages are English only."""
import argparse
import getpass
import logging
import os
import pwd
import signal
import subprocess
import sys
import tempfile
import threading
import time

from . import APP_ID, APP_NAME, __version__, paths


def _config():
    from .config import Config
    return Config()


def _chown_to_service(path):
    """Files written by root must stay owned by the service user."""
    if os.geteuid() != 0:
        return
    try:
        pw = pwd.getpwnam(APP_ID)
    except KeyError:
        return
    os.chown(path, pw.pw_uid, pw.pw_gid)


# ---------------------------------------------------------------------------
def cmd_version(args):
    print(f"{APP_NAME} {__version__}")
    return 0


def cmd_reset_password(args):
    from .auth import hash_password, MIN_PASSWORD_LENGTH
    cfg = _config()
    if args.disable:
        cfg.update({"login_enabled": False, "password_hash": ""})
        _chown_to_service(cfg.path)
        print("Login switched off and password removed.")
        _restart_hint()
        return 0
    for _ in range(3):
        pw1 = getpass.getpass("New password: ")
        if len(pw1) < MIN_PASSWORD_LENGTH:
            print(f"The password must have at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        if getpass.getpass("Repeat password: ") != pw1:
            print("The passwords do not match.")
            continue
        cfg.update({"password_hash": hash_password(pw1), "login_enabled": True})
        _chown_to_service(cfg.path)
        print("Password set; login is switched on.")
        _restart_hint()
        return 0
    print("Password not changed.", file=sys.stderr)
    return 1


def _restart_hint():
    if os.geteuid() == 0 and os.path.isdir("/run/systemd/system"):
        subprocess.run(["systemctl", "try-restart", f"{APP_ID}.service"], check=False)


def cmd_init_stick(args):
    """Write the marker file to a stick so the service adopts it."""
    device = args.device
    if os.geteuid() != 0:
        print("Run this command with sudo.", file=sys.stderr)
        return 1
    if not device.startswith("/dev/") or not os.path.exists(device):
        print(f"No such device: {device}", file=sys.stderr)
        return 1
    mnt = tempfile.mkdtemp(prefix="fos-init-")
    try:
        r = subprocess.run(["mount", device, mnt], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"Cannot mount {device}: {r.stderr.strip()}", file=sys.stderr)
            print("Format the stick first, e.g. sudo mkfs.ext4 -L FOOTAGE " + device,
                  file=sys.stderr)
            return 1
        try:
            marker = os.path.join(mnt, paths.MARKER_NAME)
            with open(marker, "w", encoding="utf-8") as f:
                f.write(f"This stick is used by {APP_NAME}.\n"
                        "Old run folders are deleted when space runs short.\n")
                f.flush()
                os.fsync(f.fileno())
            print(f"Marker written to {device}. Plug it into the Pi (or re-plug it) to use it.")
        finally:
            subprocess.run(["umount", mnt], check=False)
    finally:
        try:
            os.rmdir(mnt)
        except OSError:
            pass
    return 0


# ---------------------------------------------------------------------------
class _DirStickManager:
    """Stick manager for --simulate: a plain directory acts as the stick."""

    def __init__(self, service, root):
        from .stick import Stick
        self.service = service
        self._stick = Stick(root)

    def start(self):
        self._stick.cleanup()
        self.refresh()

    def stop(self):
        pass

    def stick(self):
        return self._stick if self._stick.has_marker() else None

    def refresh(self):
        s = self._stick
        total, free = s.space()
        oldest, newest = s.oldest_newest()
        self.service.status.update(stick={
            "present": True, "adopted": s.has_marker(), "device": "simulated", "label": "SIM",
            "fstype": "dir", "total": total, "free": free, "oldest": oldest, "newest": newest,
            "runs": len(s.run_folders()), "read_only": False, "ejected": False})

    def eject(self):
        self.refresh()


class _DirDiscovery:
    """Discovery for --simulate: every sub-directory of DIR is a camera."""

    def __init__(self, service, backend, base):
        self.service, self.backend, self.base = service, backend, base
        self._stop = threading.Event()
        self._seen = set()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="sim-discovery").start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            names = set(n for n in os.listdir(self.base)
                        if os.path.isdir(os.path.join(self.base, n)))
            for n in names - self._seen:
                self.service.camera_added(self.backend.device(n))
            for n in self._seen - names:
                self.service.camera_removed(f"sim:{n}")
            self._seen = names
            self._stop.wait(2)


def cmd_serve(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("footage_offload")
    from .config import Config
    from .service import Service
    from .state import State
    from .status import StatusBus
    from .web.app import create_app
    from .web.server import serve

    config = Config()
    os.makedirs(paths.STATE_DIR, exist_ok=True)
    status = StatusBus()
    state = State(paths.STATE_DB)

    if args.simulate:
        from .camera.simulated import SimulatedBackend
        from .helper_client import RecordingHelper
        backend = SimulatedBackend(args.simulate)
        helper = RecordingHelper()
        stick_root = args.stick or os.path.join(args.simulate, os.pardir, "stick")
        os.makedirs(stick_root, exist_ok=True)
        marker = os.path.join(stick_root, paths.MARKER_NAME)
        if not os.path.exists(marker):
            open(marker, "w").close()
        stick_factory = lambda svc: _DirStickManager(svc, stick_root)  # noqa: E731
    else:
        from .camera.gphoto import GphotoBackend
        from .helper_client import HelperClient
        from .stickmanager import StickManager
        backend = GphotoBackend()
        helper = HelperClient()
        stick_factory = StickManager

    extras = _make_extras(config, status, helper, state, log)
    service = Service(config, backend, helper, stick_factory, status=status, state=state,
                      extras=extras)
    for extra in extras:
        if hasattr(extra, "attach"):
            extra.attach(service)

    if args.simulate:
        discovery = _DirDiscovery(service, backend, args.simulate)
    else:
        from .discovery import Discovery
        discovery = Discovery(service, backend)

    sysinfo = next((e for e in extras if type(e).__name__ == "SysInfo"), None)
    app = create_app(service, config, state, service.stick_manager, helper, sysinfo=sysinfo)
    port = args.port or config.get("web_port")

    stop = threading.Event()

    def _term(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    service.start()
    discovery.start()
    server = serve(app, port)
    log.info("%s %s listening on port %d", APP_NAME, __version__, port)
    try:
        while not stop.is_set():
            stop.wait(1)
    finally:
        log.info("stopping")
        discovery.stop()
        server.shutdown()
        service.stop()
        state.close()
    return 0


def _make_extras(config, status, helper, state, log):
    """Optional features. A missing module or dependency disables only that feature."""
    extras = []
    clock = None
    try:
        from .timesync import ClockSync
        clock = ClockSync(config, status, helper)
        extras.append(clock)
    except Exception:  # noqa: BLE001
        log.exception("clock sync disabled")
    try:
        from .sysinfo import SysInfo
        extras.append(SysInfo(config, status, clock_sync=clock))
    except Exception:  # noqa: BLE001
        log.exception("system info disabled")
    try:
        from .mqtt import MqttPublisher
        extras.append(MqttPublisher(config, status))
    except Exception:  # noqa: BLE001
        log.exception("MQTT disabled")
    try:
        from .nas import NasCopy
        extras.append(_LateNas(NasCopy, config, status, state))
    except Exception:  # noqa: BLE001
        log.exception("NAS copy disabled")
    return extras


class _LateNas:
    """NasCopy needs the stick manager, which exists only once the service does."""

    def __init__(self, cls, config, status, state):
        self.cls, self.args, self.inner = cls, (config, status, state), None

    def attach(self, service):
        self.inner = self.cls(*self.args, service.stick_manager)

    def after_run(self, info, result):
        if self.inner:
            self.inner.after_run(info, result)

    def stop(self):
        if self.inner and hasattr(self.inner, "stop"):
            self.inner.stop()


# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(prog=APP_ID, description=f"{APP_NAME} {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the service (started by systemd)")
    s.add_argument("--port", type=int, help="web port (default: from the config)")
    s.add_argument("--simulate", metavar="DIR",
                   help="use simulated cameras: every sub-directory of DIR is a camera")
    s.add_argument("--stick", metavar="DIR", help="with --simulate: directory used as the stick")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("reset-password", help="set a new web UI password (or --disable login)")
    s.add_argument("--disable", action="store_true", help="switch login off instead")
    s.set_defaults(func=cmd_reset_password)

    s = sub.add_parser("init-stick", help="adopt a USB stick by writing the marker file")
    s.add_argument("device", help="partition, e.g. /dev/sda1")
    s.set_defaults(func=cmd_init_stick)

    s = sub.add_parser("version", help="print the version")
    s.set_defaults(func=cmd_version)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
