"""Client for the root helper (see helper.py and docs/ARCHITECTURE.md).

One request per connection: a single line of JSON out, a single line back.
`RecordingHelper` is a drop-in fake for tests.
"""
import json
import logging
import socket
import threading

from . import paths

log = logging.getLogger(__name__)

MAX_REPLY = 1024 * 1024


class HelperError(Exception):
    """The helper refused or failed the action, or could not be reached."""


class HelperClient:
    def __init__(self, socket_path=None, timeout=120):
        self.socket_path = socket_path or paths.HELPER_SOCKET
        self.timeout = timeout

    def call(self, action, **args):
        """Run `action` in the helper; returns its `result` or raises HelperError."""
        request = json.dumps({"action": action, "args": args}).encode("utf-8") + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
                sock.sendall(request)
                sock.shutdown(socket.SHUT_WR)
                data = b""
                while not data.endswith(b"\n"):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > MAX_REPLY:
                        raise HelperError("reply from helper too long")
        except socket.timeout as e:
            raise HelperError(f"{action}: helper timed out") from e
        except OSError as e:
            raise HelperError(f"{action}: cannot reach helper: {e}") from e
        if not data:
            raise HelperError(f"{action}: no reply from helper")
        try:
            reply = json.loads(data.decode("utf-8"))
        except ValueError as e:
            raise HelperError(f"{action}: malformed reply from helper") from e
        if not isinstance(reply, dict):
            raise HelperError(f"{action}: malformed reply from helper")
        if not reply.get("ok"):
            raise HelperError(str(reply.get("error") or "unknown helper error"))
        return reply.get("result")


class RecordingHelper:
    """Fake helper for tests: records calls and returns configured results.

    `results` maps an action to either a value (returned), an Exception
    instance (raised), or a callable taking the keyword arguments (its return
    value is returned, or what it raises is raised). Unknown actions return
    `default`.

        helper = RecordingHelper({"probe_stick": {"marker": True, "fstype": "exfat",
                                                   "label": "STICK", "uuid": "1234"}})
        helper.call("probe_stick", device="/dev/sda1")
        helper.calls == [("probe_stick", {"device": "/dev/sda1"})]
    """

    def __init__(self, results=None, default=None):
        self.results = dict(results or {})
        self.default = default if default is not None else {}
        self.calls = []
        self._lock = threading.Lock()

    def set(self, action, result):
        self.results[action] = result

    def fail(self, action, message="failed"):
        self.results[action] = HelperError(message)

    def call(self, action, **args):
        with self._lock:
            self.calls.append((action, dict(args)))
            result = self.results.get(action, self.default)
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(**args)
        return result

    def actions(self):
        """Just the action names, in call order."""
        with self._lock:
            return [a for a, _ in self.calls]

    def calls_of(self, action):
        with self._lock:
            return [args for a, args in self.calls if a == action]
