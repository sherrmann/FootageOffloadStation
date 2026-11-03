"""Clock handling: Pi NTP status, camera clock check and best-effort clock set.

The Pi has no battery-backed clock, so run folder names depend on NTP. The
camera clock matters because file names and timestamps in the footage come
from it. We never delay or fail a transfer because of any of this.

Open hardware question: whether the HERO6 Wi-Fi can be joined while the
camera is also attached over USB (it may switch off Wi-Fi in USB mode).
Everything that depends on that is controlled by the module constants below
so it can be tuned after testing on the real camera.
"""
import logging
import subprocess
import threading
import time
import urllib.request

from .camera.base import CameraError

log = logging.getLogger(__name__)

NTP_CHECK_INTERVAL_S = 60
MAX_CLOCK_DIFFERENCE_S = 3600          # warn above one hour
MIN_PLAUSIBLE_YEAR = 2019              # earlier than this means a reset camera clock
FUTURE_MTIME_SLACK_S = 24 * 3600       # time zone and rounding slack for "file from the future"

GOPRO_VENDOR_ID = "2672"               # 0x0A70
GOPRO_ADDRESS = "10.5.5.9"
# Wi-Fi fallback timing (see module docstring).
WIFI_START_DELAY_S = 0.0               # wait before joining; raise if the camera needs time to enable Wi-Fi
WIFI_RETRY_WINDOW_S = 60.0             # how long to keep trying the HTTP call while Wi-Fi comes up
WIFI_RETRY_INTERVAL_S = 3.0
HTTP_TIMEOUT_S = 3.0


def gopro_date_time_param(timestamp):
    """`%YY%MM%DD%hh%mm%ss`: each field a byte as two hex digits; year minus 2000."""
    t = time.localtime(timestamp)
    parts = (t.tm_year - 2000, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    return "".join("%%%02x" % p for p in parts)


def _default_http_get(url, timeout=HTTP_TIMEOUT_S):
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed http URL
        return resp.status


def is_gopro(info):
    return (str(getattr(info, "vendor_id", "")) == GOPRO_VENDOR_ID
            or "gopro" in (getattr(info, "manufacturer", "") or "").lower())


class ClockSync:
    def __init__(self, config, status, helper, http_get=None, clock=time.time,
                 run=subprocess.run, sleep=time.sleep):
        self.config = config
        self.status = status
        self.helper = helper
        self._http_get = http_get or _default_http_get
        self._clock = clock
        self._run = run
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread = None
        self._wifi_thread = None

    # NTP -------------------------------------------------------------------
    def ntp_synchronised(self):
        """True/False, or None when timedatectl is unavailable."""
        try:
            p = self._run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                          capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if p.returncode != 0:
            return None
        value = (p.stdout or "").strip().lower()
        if value == "yes":
            return True
        if value == "no":
            return False
        return None

    def check_ntp(self):
        synced = self.ntp_synchronised()
        self.status.update(clock={"ntp_synchronised": synced})
        if synced is False:
            self.status.add_warning("warning.ntp_not_synchronised")
        else:
            self.status.clear_warning("warning.ntp_not_synchronised")
        return synced

    def start(self):
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ntp-check", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.check_ntp()
            except Exception:  # noqa: BLE001
                log.exception("NTP check failed")
            self._stop.wait(NTP_CHECK_INTERVAL_S)

    # Hooks -----------------------------------------------------------------
    def before_run(self, camera, info, files):
        try:
            synced = self.ntp_synchronised()
            self._check_camera_clock(camera, files, synced)
            self._set_camera_clock(camera, info, synced)
        except Exception:  # noqa: BLE001 - must never disturb the transfer
            log.exception("clock handling failed")

    def _check_camera_clock(self, camera, files, synced):
        now = self._clock()
        camera_time = None
        try:
            camera_time = camera.get_clock()
        except CameraError as e:
            log.info("camera clock unavailable: %s", e)
        newest = max((f.mtime for f in files if f.mtime), default=None)
        if camera_time is None:
            camera_time = newest        # a hint only
        if camera_time is None:
            return
        wrong = time.gmtime(camera_time).tm_year < MIN_PLAUSIBLE_YEAR
        if synced:
            if abs(camera_time - now) > MAX_CLOCK_DIFFERENCE_S:
                wrong = True
            if newest is not None and newest > now + FUTURE_MTIME_SLACK_S:
                wrong = True
        if wrong:
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(camera_time))
            self.status.add_warning("warning.camera_clock_wrong", camera_time=iso,
                                    difference_s=int(camera_time - now))

    def _set_camera_clock(self, camera, info, synced):
        if not synced:
            log.info("not setting camera clock: Pi clock is not NTP-synchronised")
            return
        now = self._clock()
        try:
            camera.set_clock(now)
        except Exception as e:  # noqa: BLE001 - NotSupported or anything else
            log.info("setting camera clock over USB failed: %s", e)
        else:
            self.status.add_warning("warning.camera_clock_set", method="usb")
            return
        if self.config.get("camera_wifi_enabled") and is_gopro(info):
            self._wifi_thread = threading.Thread(target=self._wifi_set, args=(now,),
                                                 name="clock-wifi", daemon=True)
            self._wifi_thread.start()
        else:
            self.status.add_warning("warning.camera_clock_set_failed",
                                    detail="not supported over USB")

    def join_wifi(self, timeout=None):
        t = self._wifi_thread
        if t:
            t.join(timeout)

    def _wifi_set(self, timestamp):
        connected = False
        try:
            if WIFI_START_DELAY_S:
                self._sleep(WIFI_START_DELAY_S)
            self.helper.call("wifi_connect", ssid=self.config.get("camera_wifi_ssid"),
                             password=self.config.get("camera_wifi_password"))
            connected = True
            url = (f"http://{GOPRO_ADDRESS}/gp/gpControl/command/setup/date_time?p="
                   + gopro_date_time_param(timestamp))
            attempts = max(1, int(WIFI_RETRY_WINDOW_S / WIFI_RETRY_INTERVAL_S))
            last = "no answer"
            for attempt in range(attempts):
                try:
                    self._http_get(url)
                    self.status.add_warning("warning.camera_clock_set", method="wifi")
                    return
                except Exception as e:  # noqa: BLE001
                    last = str(e)
                if attempt < attempts - 1:
                    self._sleep(WIFI_RETRY_INTERVAL_S)
            self.status.add_warning("warning.camera_clock_set_failed", detail=last)
        except Exception as e:  # noqa: BLE001
            log.warning("Wi-Fi clock set failed: %s", e)
            self.status.add_warning("warning.camera_clock_set_failed", detail=str(e))
        finally:
            if connected:
                try:
                    self.helper.call("wifi_disconnect")
                except Exception as e:  # noqa: BLE001
                    log.warning("wifi_disconnect failed: %s", e)
