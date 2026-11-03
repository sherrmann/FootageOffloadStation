"""Configuration: defaults, validation, atomic load and save.

The configuration lives in one JSON file (/etc/footage-offload/config.json)
owned by the service user. The web UI writes it through `save()`, which
validates every value first; unknown keys are rejected.
"""
import copy
import json
import os
import re
import tempfile
import threading

from . import paths

LANGUAGES = ("en", "de", "fr", "it")

DEFAULTS = {
    "web_port": 8080,
    "language": "auto",                 # "auto" = follow the browser
    "min_free_mb": 1024,                # space always left free on the stick
    "login_enabled": False,
    "password_hash": "",                # scrypt hash, see auth.py
    "camera_wifi_enabled": False,
    "camera_wifi_ssid": "",
    "camera_wifi_password": "",
    "samba_enabled": False,
    "nas_enabled": False,
    "nas_method": "rsync",              # "rsync" or "smb"
    "nas_target": "",                   # rsync: host:/path or rsync://host/module/path; smb: //server/share/path
    "nas_username": "",
    "nas_password": "",
    "mqtt_enabled": False,
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
    "mqtt_topic": "footage-offload",
    "update_check_enabled": False,
    "transfer_mode": "auto",            # "auto", "partial" or "whole"
    "chunk_size_kb": 1024,
    "retry_count": 3,
    "retry_base_delay_s": 2.0,
}

# Keys that are never sent to the browser in clear text.
SECRET_KEYS = ("password_hash", "camera_wifi_password", "nas_password", "mqtt_password")


class ConfigError(ValueError):
    """Raised with a dict {key: message_id} of validation failures."""

    def __init__(self, errors):
        super().__init__(", ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-/]{1,100}$")


def _validate_one(key, value):
    """Return an error message id or None."""
    d = DEFAULTS[key]
    if isinstance(d, bool):
        return None if isinstance(value, bool) else "error.type_bool"
    if isinstance(d, int):
        if isinstance(value, bool) or not isinstance(value, int):
            return "error.type_int"
        limits = {
            "web_port": (1, 65535),
            "min_free_mb": (0, 1_000_000),
            "mqtt_port": (1, 65535),
            "chunk_size_kb": (64, 16384),
            "retry_count": (0, 10),
        }
        lo, hi = limits[key]
        return None if lo <= value <= hi else "error.out_of_range"
    if isinstance(d, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "error.type_number"
        return None if 0 <= value <= 600 else "error.out_of_range"
    if not isinstance(value, str):
        return "error.type_string"
    if len(value) > 1024 or "\n" in value or "\x00" in value:
        return "error.invalid"
    if key == "language":
        return None if value == "auto" or value in LANGUAGES else "error.invalid"
    if key == "nas_method":
        return None if value in ("rsync", "smb") else "error.invalid"
    if key == "transfer_mode":
        return None if value in ("auto", "partial", "whole") else "error.invalid"
    if key == "camera_wifi_ssid":
        return None if len(value.encode()) <= 32 else "error.ssid_length"
    if key == "camera_wifi_password":
        return None if value == "" or 8 <= len(value) <= 63 else "error.wifi_password_length"
    if key == "mqtt_host":
        return None if value == "" or _HOST_RE.match(value) else "error.invalid_host"
    if key == "mqtt_topic":
        return None if _TOPIC_RE.match(value) else "error.invalid_topic"
    if key == "nas_target":
        if value and value.startswith("-"):
            return "error.invalid"
        return None
    return None


def validate(data, partial=False):
    """Validate a dict of settings; return the merged, cleaned config."""
    errors = {}
    for key in data:
        if key not in DEFAULTS:
            errors[key] = "error.unknown_setting"
    for key, value in data.items():
        if key in DEFAULTS:
            msg = _validate_one(key, value)
            if msg:
                errors[key] = msg
    if errors:
        raise ConfigError(errors)
    merged = copy.deepcopy(DEFAULTS) if not partial else {}
    merged.update(data)
    for key, d in DEFAULTS.items():
        if isinstance(d, float) and key in merged:
            merged[key] = float(merged[key])
    # Cross-field checks
    full = dict(DEFAULTS)
    full.update(merged)
    if full["camera_wifi_enabled"] and not full["camera_wifi_ssid"]:
        errors["camera_wifi_ssid"] = "error.required"
    if full["nas_enabled"] and not full["nas_target"]:
        errors["nas_target"] = "error.required"
    if full["mqtt_enabled"] and not full["mqtt_host"]:
        errors["mqtt_host"] = "error.required"
    if full["login_enabled"] and not full["password_hash"]:
        errors["login_enabled"] = "error.password_not_set"
    if errors:
        raise ConfigError(errors)
    return merged


class Config:
    """Thread-safe access to the configuration file."""

    def __init__(self, path=None):
        self.path = path or paths.CONFIG_FILE
        self._lock = threading.Lock()
        self._data = copy.deepcopy(DEFAULTS)
        self._listeners = []
        self.load()

    def load(self):
        with self._lock:
            data = copy.deepcopy(DEFAULTS)
            try:
                with open(self.path, encoding="utf-8") as f:
                    stored = json.load(f)
                if isinstance(stored, dict):
                    for k, v in stored.items():
                        if k in DEFAULTS and _validate_one(k, v) is None:
                            data[k] = v
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                # A broken file must not stop the service; defaults apply.
                pass
            self._data = data

    def get(self, key):
        with self._lock:
            return self._data[key]

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def public(self):
        """Settings for the UI: secrets replaced by a 'set or not' flag."""
        data = self.snapshot()
        for key in SECRET_KEYS:
            data[key + "_set"] = bool(data.pop(key))
        return data

    def update(self, changes):
        """Validate and persist a partial update. Raises ConfigError."""
        with self._lock:
            new = dict(self._data)
            new.update(changes)
            cleaned = validate(new)
            self._write(cleaned)
            self._data = cleaned
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(changes)
            except Exception:  # noqa: BLE001 - a listener must not break saving
                pass
        return cleaned

    def on_change(self, callback):
        self._listeners.append(callback)

    def _write(self, data):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".config-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
