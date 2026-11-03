# Footage Offload Station

Footage Offload Station turns a Raspberry Pi Zero W into an unattended offload point for action cameras. Plug a camera into the Pi overnight. The Pi charges it, copies all its footage to a USB stick, checks every copy, wipes the camera, and then releases the camera from USB so that it carries on charging.

The project is open source under the MIT licence.

> **WARNING: every MTP/PTP device plugged into the Pi is copied and wiped automatically.**
> There is no approval step. Phones, tablets and other cameras that present themselves as MTP or PTP devices are treated exactly like your action camera. Only files below `DCIM` folders are copied and deleted, but for a phone or camera that is usually all your photos and videos. Do not plug in anything you have not backed up elsewhere.
>
> The only protection is on the stick side: nothing is written to a stick, and nothing is deleted on a camera, unless the stick carries the marker file described under [First steps](#first-steps).

## Contents

- [What it does](#what-it-does)
- [Tested devices](#tested-devices)
- [Hardware](#hardware)
- [Installation](#installation)
- [First steps](#first-steps)
- [Web interface](#web-interface)
- [Settings](#settings)
- [Security notes](#security-notes)
- [Stick layout and rotation](#stick-layout-and-rotation)
- [Time and clock](#time-and-clock)
- [NAS, Samba and MQTT](#nas-samba-and-mqtt)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)
- [Development](#development)
- [Licence and trademarks](#licence-and-trademarks)

## What it does

1. A camera is plugged into the Pi. The camera charges from the Pi's USB port.
2. The Pi sees a camera that speaks MTP or PTP over USB (the SD card stays in the camera) and starts a run.
3. Every file below the camera's `DCIM` folders is copied to the adopted USB stick, written under a temporary name, flushed to disk, size-checked against the camera's own value and then renamed.
4. Only after that is the file deleted on the camera.
5. When the run is finished, the Pi de-authorises the camera's USB port through sysfs. The camera is no longer an active USB device but keeps drawing charge.
6. Optionally, the run is copied on to a NAS with rsync or SMB.

The source can be any camera that appears as an MTP/PTP device. It has to work with the GoPro HERO6 Black; see [Tested devices](#tested-devices).

Known limitation: a camera that is switched off only charges and does not appear on USB. Switch the camera on after plugging it in.

## Tested devices

| Device | Status | Notes |
|---|---|---|
| GoPro HERO6 Black | pending hardware test | Reference device. LRV proxies and THM thumbnails are copied with the video. |

Any camera that shows up as an MTP/PTP device should in principle work, but only devices listed here have been looked at. If you try another device, please open a report with the **device report** issue template on GitHub, whether it worked or not. Say which camera and firmware, what the Pi did, and attach the diagnostics bundle (System page).

CI tests use a simulated camera. What CI cannot cover is listed in [docs/HARDWARE_CHECKLIST.md](docs/HARDWARE_CHECKLIST.md).

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi Zero W | Reference board (armv6). The Zero 2 W should also work. |
| Raspberry Pi OS 32-bit | Lite or Desktop. Bookworm is the reference; Trixie is being checked. |
| USB hub or hat | The Zero has one data port. The reference setup uses a Spotpear "Zero to 3B" board: four USB 2.0 ports and 10/100 Ethernet. |
| USB stick | ext4 recommended. exFAT is supported and can be read on Windows and macOS. |
| Ethernet cable | Recommended, see below. |
| Power supply | One 5 V supply, at least 2.5 A. |

### Power

- Use **one** 5 V supply of at least 2.5 A, connected to the micro-USB socket of the Spotpear board only. Never connect a second supply to the Pi Zero itself.
- That single supply feeds the Pi, the stick and the camera's charging current (about 1 A).
- The web UI shows an undervoltage warning if the Pi has seen one. See [Troubleshooting](#troubleshooting).

### Spotpear "Zero to 3B" board

On some versions of this board the right-hand pair of USB sockets shares one signal. Use the left pair for the camera and the stick.

### Ethernet

Ethernet is recommended. It keeps the Pi on your home network while its Wi-Fi joins the camera's own Wi-Fi (used for setting the camera clock), and it keeps the Pi's clock correct through NTP.

### Speed

The camera, the stick and Ethernet share one USB 2.0 link. Expect roughly 5 to 15 MB/s. A full 64 GB card takes two to three hours.

## Installation

> **Before you install:** once the service is running, **every MTP/PTP device plugged into the Pi is copied and wiped with no approval step**, phones and other cameras included. The stick must be adopted first (see [First steps](#first-steps)), but after that nothing asks you again.

1. Install Raspberry Pi OS 32-bit (Lite or Desktop) on the Pi and get it onto your network.
2. Download `footage-offload_<version>_all.deb` from the project's GitHub Releases page.
3. Install it:

   ```
   sudo apt install ./footage-offload_<version>_all.deb
   ```

apt pulls in the dependencies (Flask, pyudev, libgphoto2). Samba is a recommended package and is installed by default; `rsync`, `smbclient` and `python3-paho-mqtt` are used by the optional features.

How it runs:

- The main service runs as the system user `footage-offload` (`footage-offload.service`).
- A small root helper (`footage-offload-helper.service`) does the few privileged actions: de-authorise the camera, mount and eject the stick, shut down or reboot, switch the Samba share, and join the camera's Wi-Fi.
- Configuration is in `/etc/footage-offload/config.json`. Normally you change it through the Settings page.
- Logs: `journalctl -u footage-offload -u footage-offload-helper`

### Desktop edition

On Raspberry Pi OS with desktop, a udev rule stops the desktop (gvfs) from grabbing the camera. If the desktop mounts the adopted stick before the service does, the service unmounts the desktop's mount and mounts the stick itself.

## First steps

1. **Adopt the stick.** Plug the USB stick into the Pi. Find its partition with `lsblk` (for example `/dev/sda1`), then run:

   ```
   sudo footage-offload init-stick /dev/sda1
   ```

   This creates the marker file `.footage-offload-station` in the root of the stick. Only a stick with this file is used. Without it, nothing is written and nothing is deleted on the camera. A stick without the marker shows a warning in the UI and is otherwise ignored.

2. **Plug in the camera** and **switch it on**. A switched-off camera only charges.
3. **Open the web UI** at `http://<hostname>.local:8080` (the default hostname of a fresh Raspberry Pi OS install is whatever you set in the imager; the port is a setting). The Status page shows the run live.
4. When the run is done the camera is released from USB and keeps charging. You can leave it or unplug it.
5. To take the stick away, use **Safe eject** on the Stick page first.

## Web interface

The interface works on a phone. It is available in English, German, French and Italian, with Swiss formats (date `03.11.2026`, numbers `1'234.5`, 24-hour time).

| Page | What it shows |
|---|---|
| Status | Live state of the current run: files, progress, warnings and errors. |
| Stick | Capacity, free space, oldest and newest run, and safe eject. |
| History | Past runs, and statistics per camera by serial number. |
| Gallery | Thumbnails (THM) and low-resolution preview playback (LRV) of what is on the stick. Original files are downloaded, not streamed: they are 60 to 80 Mbit/s and partly HEVC, which a Pi Zero and many browsers cannot play live. |
| Settings | All configuration, see below. |
| System | Version, an optional hint that a newer release exists (there is no automatic update), undervoltage warning, shutdown and reboot, and the diagnostics bundle (logs and versions; no footage). |

A REST API is available under `/api/v1`; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Status can also be published over MQTT.

## Settings

All settings are on the Settings page. Defaults are in brackets.

| Setting | Meaning |
|---|---|
| Language | Follow the browser (default), or English, German, French, Italian. |
| Web port | Port of the web UI (8080). |
| Minimum free space | Space always left free on the stick (1024 MB). Rotation deletes old runs to keep it. |
| Password | Off by default. See [Security notes](#security-notes). |
| Camera Wi-Fi | SSID and password of the camera's Wi-Fi, for setting the camera clock (off). |
| Samba share | Read-only share "footage" (off). |
| NAS copy | Onward copy after each run, by rsync or SMB, with target, user name and password (off). |
| MQTT | Host, port, user name, password and topic (off, topic `footage-offload`). |
| Update check | Ask GitHub whether a newer release exists (off). It only shows a hint. |

There are also advanced transfer settings (transfer mode, chunk size, retry count and base delay). The defaults are three retries with increasing waits. Passwords are never shown again after saving; leaving a password field empty keeps the stored value.

## Security notes

- Login is **off by default**. Enable it in Settings by setting a password (at least 8 characters). While login is on, CSRF protection is active.
- The password is stored only as a scrypt hash.
- Lost password: `sudo footage-offload reset-password`
- The service always listens on all interfaces.
- The web UI uses plain HTTP, so the password crosses the network unencrypted. **Do not expose the port to the internet.** Use it only on a trusted home network.
- The camera Wi-Fi, NAS and MQTT passwords are stored in `/etc/footage-offload/config.json`, readable only by the service user and root.
- Anyone who can reach the UI can read your footage through the Gallery, trigger shutdown and reboot, and download diagnostics. Turn the password on if others share your network.

## Stick layout and rotation

Each plug-in produces one folder on the stick, named after the Pi's clock at the start of the run:

```
2026-11-03_21-47/
    GOPR0001.MP4
    GOPR0001.LRV
    GOPR0001.THM
    ...
```

- Names contain no colons, so they work on exFAT.
- A run with nothing to copy creates no folder.
- Camera dates are not used, because a camera's clock resets when its battery is flat.
- If a name already exists, a suffix is added (`_1`). Nothing is overwritten.
- LRV proxies and THM thumbnails are copied as well (the Gallery uses them) and are deleted from the camera together with their video.

### Safety of the copy

A camera file is deleted only after its copy has been written to a temporary name, flushed to disk (fsync), size-checked against the size the camera reports, and renamed to its final name. Pulling the plug or losing power never loses data. In the worst case a file exists twice, once on the stick and once on the camera. Half-written temporary files are removed at the next start.

If a file fails, the Pi retries three times with increasing waits, then reports the error. The file stays on the camera. A read-only or I/O error on the stick stops the run.

### Rotation

When space runs short, the oldest earlier run folders are deleted, and only folders whose names look like run folders. The current run is never touched. If there is still not enough room, the run stops and the remaining files stay on the camera. The amount of space kept free is the "minimum free space" setting.

## Time and clock

Run folders are named from the Pi's clock, so it matters.

- The UI warns when the Pi clock is not NTP-synchronised. Ethernet keeps NTP working.
- The UI warns when the camera's clock is obviously wrong.
- On every plug-in the Pi tries to set the camera clock, on a best-effort basis: over USB if the camera supports it; otherwise, for the HERO6, over Wi-Fi using the legacy GoPro API, with the SSID and password entered in Settings. Set the camera's Wi-Fi band to 2.4 GHz.
- A failed clock set does not affect the transfer.

## NAS, Samba and MQTT

- **NAS copy.** After each run the new folder can be sent on to a NAS with rsync or SMB. Set it up in Settings. A failure is shown as a warning; the footage on the stick is unaffected.
- **Samba.** A read-only share named `footage` exposes the stick to your network. Switch it on in Settings. Samba is a recommended package of the .deb.
- **MQTT.** Status is published to the broker and topic given in Settings.

## Troubleshooting

**The camera is not detected.**
- Is it switched on? A switched-off camera only charges.
- Run `lsusb`. If the camera is not listed, try another cable or one of the left-hand pair of ports on the Spotpear board.
- Look at the logs: `journalctl -u footage-offload -u footage-offload-helper`
- The camera must present itself as MTP/PTP. Some cameras have a USB mode setting.
- On the desktop edition, check that the desktop has not opened the camera (see below).
- Note: after a run the Pi de-authorises the camera. It may still be listed by `lsusb`, but no driver uses it until you unplug and replug it (or switch it off and on).

**The stick is not used.**
- Does it carry the marker file `.footage-offload-station` in its root? If not, run `sudo footage-offload init-stick /dev/sda1` (with the right device). An unadopted stick shows a warning on the Status page.
- Is it read-only or damaged? The Stick page and logs say so.
- Has it been ejected from the Stick page? Unplug and replug it.

**The run stopped with "not enough room".** Raise the free space by deleting old runs yourself, use a larger stick, or lower the minimum free space. Files not copied stay on the camera.

**Undervoltage warning.** The supply or cable is too weak. Use a 5 V supply of at least 2.5 A, a short good cable, and one supply only, into the board.

**Clock warnings.** Pi clock not synchronised: check the network and that Ethernet is connected. Camera clock wrong: enter the camera Wi-Fi details in Settings (HERO6) or set the clock on the camera.

**The desktop opens the camera or a file manager appears.** This should be prevented by the udev rule shipped with the .deb, which takes effect for cameras plugged in after installation; unplug and replug the camera. If it persists, attach a diagnostics bundle to a bug report.

**Reporting a problem.** On the System page, download the diagnostics bundle. It contains logs, version information and a gphoto2 summary, and no footage. Attach it to your GitHub issue.

## Uninstalling

```
sudo apt remove footage-offload
```

removes the program. `sudo apt purge footage-offload` also removes the configuration and the `footage-offload` system user. Neither touches the footage on the stick.

## Development

Run the tests (Python 3.11 or later):

```
python3 -m pytest
```

Run the service against a simulated camera, which reads files from a directory:

```
footage-offload serve --simulate DIR
```

Build the .deb:

```
packaging/build-deb.sh
```

The module layout and internal contracts are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the message ids that the backend emits are in [docs/MESSAGE_IDS.md](docs/MESSAGE_IDS.md).

### Continuous integration

- Stage 1: logic tests on x86 with a simulated camera and fault injection.
- Stage 2: the .deb is installed and tested on a Raspberry Pi OS armv6 userland under QEMU user emulation, followed by a purge check.
- Stage 3, on release tags: a full QEMU system boot with Playwright browser tests.

CI cannot cover real MTP behaviour, gvfs, charging and power, the Spotpear hub, or real speed and RAM use. Those are checked by hand with [docs/HARDWARE_CHECKLIST.md](docs/HARDWARE_CHECKLIST.md).

## Licence and trademarks

Released under the MIT licence. libgphoto2 (LGPL) is used through `ctypes`. The GPL-licensed python-gphoto2 bindings are deliberately not used.

GoPro and HERO are trademarks of GoPro, Inc. This project is not affiliated with, endorsed by or sponsored by GoPro, Inc. The name is used only to say which device is supported. The project name deliberately does not contain it.
