# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.1] - unreleased

### Changed

- Web UI: no explanatory text any more (field hints, notes, notices). The UI shows labels, values, status, warnings and errors only; the warnings box appears only when there is a warning. Checkbox labels are shorter.
- Web UI: the status headline after a run is a short "Done".

### Fixed

- Web UI: collapsible sections (gallery runs, error lists in History, Advanced settings) show an arrow, so collapsed gallery runs no longer look empty.

## [1.0.0] - 2026-11-03

### Added

- Unattended offload of MTP/PTP cameras on a Raspberry Pi Zero W: copy all files below `DCIM` to a USB stick, verify, delete on the camera, then release the camera from USB (sysfs de-authorise) so it keeps charging.
- Stick adoption with the marker file `.footage-offload-station`, created by `footage-offload init-stick`. Unmarked sticks are ignored.
- One folder per plug-in, named from the Pi's clock (`2026-11-03_21-47`); name collisions get a suffix; LRV and THM files are copied and deleted with their video.
- Safe copy: temporary name, fsync, size check against the camera, rename, then delete. Three retries with increasing waits. Leftover temporary files are removed at start.
- Rotation of the oldest run folders when space runs short, with a minimum free space setting.
- Optional onward copy to a NAS by rsync or SMB.
- Clock checks (Pi NTP status, camera clock) and best-effort setting of the camera clock over USB or, for the HERO6, over Wi-Fi.
- Web UI (Status, Stick, History, Gallery, Settings, System) in English, German, French and Italian with Swiss formats; usable on a phone.
- REST API under `/api/v1`, live status events and MQTT status publishing.
- Read-only Samba share "footage".
- Optional password (scrypt hash) with CSRF protection; `footage-offload reset-password`.
- Undervoltage warning, optional update hint, shutdown and reboot, and a diagnostics bundle without footage.
- Debian package `footage-offload_<version>_all.deb` with a main service and a small root helper; desktop edition udev rule against gvfs.
- Camera access through libgphoto2 via `ctypes` (no GPL python-gphoto2 bindings).
- Continuous integration in three stages (logic tests, armv6 .deb install under QEMU, full system boot on release tags) and a manual hardware checklist.

### Known limitations

- A switched-off camera only charges and does not appear on USB.
- Every MTP/PTP device plugged in is copied and wiped, with no approval step.
- GoPro HERO6 Black support is pending a hardware test.
