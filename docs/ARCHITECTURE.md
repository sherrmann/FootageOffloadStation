# Architecture and internal contracts

This document is the contract between the modules. If code and this file
disagree, fix one of them in the same change.

Identifiers: package and system user `footage-offload`, Python package
`footage_offload`, display name "Footage Offload Station". The name does not
contain "GoPro" (trademark); GoPro is only named as a supported device.

## Ground rules

* Python 3.11 or later (Raspberry Pi OS Bookworm ships 3.11, Trixie 3.13).
  No syntax or stdlib features newer than 3.11.
* Dependencies from apt only: `python3-flask`, `python3-pyudev`,
  `libgphoto2-6` (Bookworm) or `libgphoto2-6t64` (Trixie), optional
  `python3-paho-mqtt`, `samba`, `smbclient`, `rsync`, `gphoto2` (CLI, only
  for the diagnostics summary). **No `python3-gphoto2`**: those bindings are
  GPL-3.0+, which does not fit the MIT licence. The camera backend calls the
  LGPL libgphoto2 directly through `ctypes`.
* Never hold a whole file in RAM (4 GB chapters, 512 MB RAM).
* All paths come from `footage_offload/paths.py`; tests set `FOS_ROOT`.
* User-facing text in Python is a message id (`error.stick_full`), never an
  English sentence. The UI translates ids. Logs and CLI output are English.
* Docs and comments in British English.

## Processes

| Process | User | Unit | Purpose |
|---|---|---|---|
| main service | `footage-offload` | `footage-offload.service` | discovery, transfer, rotation, web UI, REST API, MQTT, NAS copy |
| root helper | root | `footage-offload-helper.service` | fixed list of privileged actions |

## Module map

| Module | Purpose |
|---|---|
| `paths.py`, `config.py` | locations; configuration with validation |
| `camera/base.py` | `Camera`/`Backend` interface, `DeviceInfo`, `RemoteFile`, errors |
| `camera/simulated.py` | directory-backed simulated camera with fault injection |
| `camera/gphoto.py` | real backend: libgphoto2 through ctypes |
| `profiles.py` | generic DCIM rules and GoPro profile (groups, chapters) |
| `stick.py` | run folders, rotation, crash clean-up on a mounted stick |
| `stickmanager.py` | finds USB block devices, mounts the adopted stick via the helper, eject |
| `discovery.py` | pyudev monitor for MTP/PTP devices -> `Service.camera_added` |
| `transfer.py` | one run: copy, verify, delete |
| `service.py` | orchestration, worker thread, de-authorise after a run |
| `status.py` | live status bus (SSE, MQTT) |
| `state.py` | SQLite history and per-serial device statistics |
| `helper.py` | root helper server; `helper_client.py` the client |
| `timesync.py` | NTP status, camera clock check, clock set via USB or Wi-Fi |
| `nas.py` | optional onward copy (rsync or SMB) after a run |
| `mqtt.py` | MQTT status publishing |
| `sysinfo.py` | undervoltage (`vcgencmd get_throttled`), version and update check |
| `diagnostics.py` | diagnostics zip (logs, gphoto2 summary, versions; no footage) |
| `i18n.py` | catalogues, plural rules, Swiss number and date formats |
| `auth.py` | scrypt password hashing, CSRF tokens |
| `web/app.py` | Flask app: pages, REST API, SSE, downloads |
| `cli.py` | `footage-offload` command line |
| `data/web/` | `index.html`, `app.js`, `style.css` |
| `data/i18n/` | `en.json`, `de.json`, `fr.json`, `it.json` |

## Interfaces

### Extras (hooks called by `Service`)

Objects in `Service.extras` may implement any of:

```python
start()                                   # at service start
stop()
before_run(camera, info, files)           # in the transfer thread, before copying; must not raise
after_run(info, result: RunResult)        # after a run (also empty runs; check result.empty)
```

`timesync.ClockSync`, `nas.NasCopy`, `mqtt.MqttPublisher`,
`sysinfo.SysInfo` (undervoltage monitor and update check) are extras. Their constructors take
`(config, status, helper)` and, where needed, `state`/`stick_manager`.

### StickManager (`stickmanager.py`)

```python
class StickManager:
    def __init__(self, service): ...          # uses service.helper, service.status, service.config
    def start(self); def stop(self)
    def stick(self) -> Optional[stick.Stick]  # the mounted adopted stick, or None
    def refresh(self)                          # recompute status["stick"]
    def eject(self) -> None                    # sync, unmount, power off; raises StickBusy
                                               # or StickManagerError (.message_id, .detail)
    def rescan(self)                           # re-probe unadopted sticks
```

`status["stick"]` is `None` or:
`{"present": bool, "adopted": bool, "device": "/dev/sda1", "label": str,
"fstype": "ext4|exfat|vfat", "total": int, "free": int, "oldest": run name|None,
"newest": run name|None, "runs": int, "read_only": bool, "ejected": bool}`.
When the stick appears it runs `Stick.cleanup()` and calls
`service.stick_changed()`.

### Discovery (`discovery.py`)

`Discovery(service, backend)` with `start()`/`stop()`. On start it enumerates
existing devices, then monitors pyudev (`subsystem="usb"`,
`DEVTYPE=usb_device`). A device counts as camera when udev property
`FOS_CAMERA=1` (set by our udev rule) or `ID_GPHOTO2=1` or `ID_MTP_DEVICE=1`,
or one of its interfaces has class 06 (still image). It builds a
`DeviceInfo(port="usb:BUS,DEV", sysfs_path="/sys/bus/usb/devices/" + device.sys_name, vendor_id,
product_id, manufacturer, model, serial)` with BUS/DEV zero-padded to 3
digits, and calls `service.camera_added(info)` / `service.camera_removed(port)`.

### Root helper protocol

Unix stream socket `paths.HELPER_SOCKET` (`/run/footage-offload/helper.sock`),
mode 0660, group `footage-offload`. The helper checks `SO_PEERCRED`: only the
`footage-offload` user (and root, for the CLI) are accepted. One request per
connection: a single line of JSON, answered by a single line of JSON.

```json
{"action": "deauthorize_usb", "args": {"sysfs_path": "/sys/bus/usb/devices/1-1.2"}}
{"ok": true, "result": {...}}      or      {"ok": false, "error": "message"}
```

Actions (fixed list; every argument is validated, nothing is passed to a shell):

| Action | Args | Effect |
|---|---|---|
| `deauthorize_usb` | `sysfs_path` (must match `/sys/bus/usb/devices/\d+-[\d.]+` and resolve under `/sys/devices/`) | writes `0` to `authorized` |
| `probe_stick` | `device` (`/dev/sd[a-z]+\d*`) | mounts read-only at `paths.PROBE_MOUNT`, reports `{"marker", "fstype", "label", "uuid"}`, unmounts |
| `mount_stick` | `device` | unmounts other mounts of that device (desktop automount), mounts it read-write at `paths.STICK_MOUNT` so the service user can write (ext4: chown root of fs; exfat/vfat: `uid=,gid=,fmask=0022,dmask=0022`), only if the marker is present. Result `{"mounted": true, "fstype"}` |
| `eject_stick` | – | `sync`, unmount `STICK_MOUNT`, power off the USB device |
| `shutdown` / `reboot` | – | `systemctl poweroff` / `reboot` |
| `samba` | `enabled` bool | writes/removes a read-only `[footage]` share include and reloads smbd (guest access relies on Debian's default `map to guest = bad user`) |
| `wifi_connect` | `ssid`, `password` | `nmcli` joins the camera Wi-Fi on `wlan0`, never as default route |
| `wifi_disconnect` | – | removes that connection |

`HelperClient().call(action, **args)` returns `result` or raises
`HelperError(message)`.

## Status dict

See `status.initial_status()`. States: `waiting`, `transferring`, `done`,
`error`. `error` and each entry of `warnings` carry a message id plus
parameters, translated by the UI.

## REST API (`/api/v1`)

All JSON. When login is on, every endpoint except `GET /api/v1/i18n/<lang>`,
`GET /api/v1/session` and `POST /api/v1/login` needs a session; every
`POST`/`PUT`/`DELETE` also needs header `X-CSRF-Token` with the token from
`GET /api/v1/session`.

| Method | Path | Result |
|---|---|---|
| GET | `/api/v1/session` | `{"login_enabled", "logged_in", "csrf_token", "language", "version"}` |
| POST | `/api/v1/login` | body `{"password"}` -> 200 or 401 `{"error": "error.wrong_password"}` |
| POST | `/api/v1/logout` | |
| GET | `/api/v1/status` | status dict |
| GET | `/api/v1/events` | `text/event-stream`, `event: status` with the status dict as data; comment keep-alive every 15 s |
| GET | `/api/v1/stick` | `status["stick"]` plus `{"runs": [{"name", "files", "size"}]}` |
| POST | `/api/v1/stick/eject` | 200, or 409 `{"error": "error.busy"}` during a transfer |
| GET | `/api/v1/history?limit=&offset=` | `{"runs": [...], "devices": [...]}` (see `state.py`) |
| GET | `/api/v1/gallery` | `{"runs": [{"name", "items": [{"name", "size", "kind": "video|photo|other", "thumb": url|null, "preview": url|null, "download": url, "recording": key}]}]}` |
| GET | `/media/<run>/<file>` | download original, HTTP Range supported, `Content-Disposition: attachment` |
| GET | `/media/<run>/<file>?inline=1` | same, inline (used for THM as `image/jpeg`, LRV as `video/mp4`) |
| GET | `/api/v1/settings` | `config.public()` |
| PUT | `/api/v1/settings` | partial update; 400 `{"errors": {key: message id}}`; secrets: empty string keeps the old value; `password` (plain) sets `password_hash` |
| POST | `/api/v1/system/shutdown` / `reboot` | 202 |
| GET | `/api/v1/system` | `{"version", "latest_version"|null, "update_available", "throttled", "ntp_synchronised", "hostname"}` |
| GET | `/api/v1/diagnostics` | `application/zip` download |
| GET | `/api/v1/i18n/<lang>` | catalogue JSON |

## i18n catalogue format

`data/i18n/<lang>.json`:

```json
{
  "_meta": {"language": "de", "name": "Deutsch", "plural": "one_other"},
  "status.waiting": "Wartet auf Kamera",
  "files.count": {"one": "{n} Datei", "other": "{n} Dateien"}
}
```

Placeholders `{name}`. Plural entries are objects with CLDR categories
(`one`, `other`; French uses `one` for 0 and 1). Swiss conventions for all
four languages: dates `03.11.2026`, times 24-hour `21:47`, thousands
separator `'` and decimal point `.` (`1'234.5`). German: no `ß` (always
`ss`), generic masculine, Swiss terms. The same files are used by the
backend (`i18n.py`) and the frontend.

The full list of message ids the backend emits is in
`docs/MESSAGE_IDS.md`; every catalogue must contain all of them.
