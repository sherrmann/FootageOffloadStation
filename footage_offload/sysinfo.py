"""System information: power problems, NTP passthrough and the update check.

The update check is opt-in (`update_check_enabled`), runs at most once per
24 hours, sends only a User-Agent with the version and never installs
anything.
"""
import json
import logging
import re
import subprocess
import threading
import time
import urllib.request

from . import GITHUB_REPO, __version__

log = logging.getLogger(__name__)

THROTTLE_INTERVAL_S = 30
UPDATE_INTERVAL_S = 24 * 3600
HTTP_TIMEOUT_S = 10

_BITS = {
    "undervoltage_now": 0, "freq_capped_now": 1, "throttled_now": 2, "soft_temp_limit_now": 3,
    "undervoltage_occurred": 16, "freq_capped_occurred": 17, "throttled_occurred": 18,
    "soft_temp_limit_occurred": 19,
}


def parse_throttled(text):
    m = re.search(r"throttled=(0x[0-9a-fA-F]+|\d+)", text or "")
    if not m:
        return None
    value = int(m.group(1), 0)
    return {name: bool(value & (1 << bit)) for name, bit in _BITS.items()}


def parse_version(text):
    """"v1.2.3" -> (1, 2, 3); None if unparseable."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", text or "")
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def _default_fetch(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 - fixed https URL
        return json.loads(resp.read(1_000_000).decode("utf-8"))


class SysInfo:
    def __init__(self, config, status, clock_sync=None, run=subprocess.run,
                 fetch=None, clock=time.time):
        self.config = config
        self.status = status
        self.clock_sync = clock_sync
        self._run = run
        self._fetch = fetch or _default_fetch
        self._clock = clock
        self._latest = None
        self._last_check = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # Power -----------------------------------------------------------------
    def throttled(self):
        """Dict of throttle flags, or None when vcgencmd is missing or fails."""
        try:
            p = self._run(["vcgencmd", "get_throttled"], capture_output=True,
                          text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if p.returncode != 0:
            return None
        return parse_throttled(p.stdout)

    def check_throttled(self):
        t = self.throttled()
        self.status.update(throttled=t)
        for flag, mid in (("undervoltage_now", "warning.undervoltage_now"),
                          ("undervoltage_occurred", "warning.undervoltage_occurred")):
            if t and t[flag]:
                self.status.add_warning(mid)
            else:
                self.status.clear_warning(mid)
        return t

    def ntp_synchronised(self):
        return self.clock_sync.ntp_synchronised() if self.clock_sync else None

    # Updates ---------------------------------------------------------------
    def latest_version(self):
        with self._lock:
            return self._latest

    def update_available(self):
        latest = self.latest_version()
        a, b = parse_version(latest), parse_version(__version__)
        return bool(a and b and a > b)

    def check_update(self, force=False):
        """Query GitHub if enabled and due. Returns the latest version or None."""
        if not self.config.get("update_check_enabled"):
            self.status.clear_warning("warning.update_available")
            return None
        now = self._clock()
        with self._lock:
            if (not force and self._last_check is not None
                    and now - self._last_check < UPDATE_INTERVAL_S):
                return self._latest
            self._last_check = now
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        try:
            data = self._fetch(url, {"User-Agent": f"footage-offload/{__version__}",
                                     "Accept": "application/vnd.github+json"})
            tag = str(data.get("tag_name", ""))
        except Exception as e:  # noqa: BLE001 - offline is normal
            log.info("update check failed: %s", e)
            return self.latest_version()
        if parse_version(tag) is None:
            return self.latest_version()
        with self._lock:
            self._latest = tag.lstrip("vV")
        if self.update_available():
            self.status.add_warning("warning.update_available", version=self.latest_version())
        else:
            self.status.clear_warning("warning.update_available")
        return self.latest_version()

    # Lifecycle ---------------------------------------------------------------
    def start(self):
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sysinfo", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.check_throttled()
                self.check_update()
            except Exception:  # noqa: BLE001
                log.exception("sysinfo check failed")
            self._stop.wait(THROTTLE_INTERVAL_S)
