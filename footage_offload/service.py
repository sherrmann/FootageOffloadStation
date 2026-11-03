"""Main service: ties discovery, stick, transfer runs, web UI and extras together.

Runs as the unprivileged system user. Privileged actions go through the
root helper (helper_client.HelperClient).
"""
import logging
import queue
import threading
import time

from . import paths
from . import status as st
from .config import Config
from .state import State
from .transfer import TransferRun, lower_priority

log = logging.getLogger(__name__)


class Service:
    def __init__(self, config: Config, backend, helper, stick_manager_factory,
                 status=None, state=None, extras=None):
        """
        backend: camera.base.Backend
        helper: helper_client.HelperClient (or a fake in tests)
        stick_manager_factory: callable(service) -> StickManager
        extras: list of objects with optional start(), stop(),
                before_run(camera, info, files), after_run(info, result) hooks
        """
        self.config = config
        self.backend = backend
        self.helper = helper
        self.status = status or st.StatusBus()
        self.state = state or State(paths.STATE_DB)
        self.extras = list(extras or [])
        self.stick_manager = stick_manager_factory(self)
        self._queue = queue.Queue()
        self._pending = set()          # ports queued or running
        self._lock = threading.Lock()
        self._worker = None
        self._stop = threading.Event()
        self.current_run = None
        self._present = {}             # port -> DeviceInfo of connected cameras

    # life cycle --------------------------------------------------------------
    def start(self):
        self._worker = threading.Thread(target=self._work, name="transfer", daemon=True)
        self._worker.start()
        self.stick_manager.start()
        for extra in self.extras:
            if hasattr(extra, "start"):
                extra.start()

    def stop(self):
        self._stop.set()
        self._queue.put(None)
        run = self.current_run
        if run:
            run.cancelled.set()
        for extra in self.extras:
            if hasattr(extra, "stop"):
                try:
                    extra.stop()
                except Exception:  # noqa: BLE001
                    log.exception("stopping %r", extra)
        self.stick_manager.stop()

    # discovery callbacks -------------------------------------------------------
    def camera_added(self, info):
        """Called by discovery when an MTP/PTP device appears."""
        with self._lock:
            self._present[info.port] = info
            if info.port in self._pending:
                return
            self._pending.add(info.port)
        log.info("camera %s %s (serial %s) on %s", info.manufacturer, info.model, info.serial, info.port)
        self._queue.put(info)

    def camera_removed(self, port):
        with self._lock:
            self._present.pop(port, None)

    def stick_changed(self):
        """Called by the stick manager when the adopted stick appears.
        Cameras that are still connected get another run."""
        with self._lock:
            present = list(self._present.values())
        for info in present:
            self.camera_added(info)

    # worker --------------------------------------------------------------------
    def _work(self):
        lower_priority()
        while not self._stop.is_set():
            info = self._queue.get()
            if info is None:
                break
            try:
                self._run_device(info)
            except Exception:  # noqa: BLE001 - the worker must survive anything
                log.exception("run failed")
            finally:
                with self._lock:
                    self._pending.discard(info.port)

    def _before_run(self, camera, info, files):
        for extra in self.extras:
            hook = getattr(extra, "before_run", None)
            if hook:
                try:
                    hook(camera, info, files)
                except Exception:  # noqa: BLE001 - extras are best effort
                    log.exception("before_run hook %r", extra)

    def _run_device(self, info):
        stick = self.stick_manager.stick()
        try:
            camera = self.backend.open(info)
        except Exception as e:  # noqa: BLE001
            log.warning("cannot open %s: %s", info.port, e)
            self.status.update(state=st.ERROR, error={"id": "error.camera_open", "detail": str(e)})
            return
        with camera:
            if stick is None:
                # Without the adopted stick nothing is copied and nothing deleted.
                self.status.update(state=st.ERROR, device={"model": info.model, "serial": info.serial,
                                                           "manufacturer": info.manufacturer},
                                   error={"id": "error.no_stick", "detail": ""})
                return
            run = TransferRun(camera, info, stick, self.config, self.status, self.state,
                              on_start=self._before_run)
            self.current_run = run
            try:
                result = run.execute()
            finally:
                self.current_run = None
        self.stick_manager.refresh()
        if result.empty or (result.result == "ok"):
            # Done: let the camera leave the system so it only charges (#18).
            if info.sysfs_path:
                try:
                    self.helper.call("deauthorize_usb", sysfs_path=info.sysfs_path)
                except Exception as e:  # noqa: BLE001
                    log.warning("de-authorising %s failed: %s", info.sysfs_path, e)
                    self.status.add_warning("warning.deauthorize_failed", detail=str(e))
            with self._lock:
                self._present.pop(info.port, None)
        for extra in self.extras:
            hook = getattr(extra, "after_run", None)
            if hook:
                try:
                    hook(info, result)
                except Exception:  # noqa: BLE001
                    log.exception("after_run hook %r", extra)

    def busy(self):
        return self.current_run is not None or not self._queue.empty()


def wait_until(predicate, timeout=10.0, step=0.05):
    """Small helper for tests."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
