# Hardware test checklist

Run this checklist on a real Raspberry Pi Zero W with a GoPro HERO6 Black before each release. Use the Spotpear "Zero to 3B" USB/Ethernet board unless a section says otherwise.

Tick each box when the step passes. If a step fails, write the problem next to it and open an issue. Record the results in the table at the end.

Before you start:

- [ ] Note the package version under test: `dpkg -s footage-offload | grep Version`
- [ ] Note the camera firmware version.
- [ ] Use a stick you can afford to test with. Keep a copy of any footage you need.

## A. Open hardware questions

These must be answered before the design is relied on. Record the answer under each item.

- [ ] `gphoto2 --summary` reports "File Deletion" for the HERO6.
  Answer: ______
- [ ] The HERO6 supports partial reads (chunked streaming, files not held in RAM).
  - [ ] The service log shows the transfer mode for each file. Answer: ______
  - [ ] Copy a chapter of 4 GB or more while running `free -m` and `top` in a second session. Note the peak RAM use. Answer: ______
- [ ] The camera offers date and time over USB. Run `gphoto2 --list-config` and check for `datetime`. This decides whether the clock is set over USB or Wi-Fi. Answer: ______
- [ ] HERO6 Wi-Fi is reachable:
  - [ ] (a) during the USB session. Answer: ______
  - [ ] (b) after de-authorising, while the camera is charging. Answer: ______
  - [ ] For how long the Wi-Fi stays reachable before the camera powers off. Answer: ______
- [ ] What the camera does after de-authorising: switches off and charges, or stays on its USB screen. Answer: ______
- [ ] Transfer speed from camera to stick through the Spotpear hub. Answer: ______ MB/s
- [ ] Both devices are seen by `lsusb` while the camera and the stick are plugged into the hub. Answer: ______

## B. Installation

- [ ] Raspberry Pi OS Lite (Bookworm):
  - [ ] `sudo apt install ./footage-offload_<version>_all.deb` completes without errors.
  - [ ] `systemctl status footage-offload footage-offload-helper` shows both services as active.
  - [ ] The web interface opens at `http://<hostname>.local:8080` on a phone.
- [ ] Raspberry Pi OS Lite (Trixie): the same three checks as above.
- [ ] Raspberry Pi OS Desktop (Bookworm): the same three checks as above.
- [ ] Raspberry Pi OS Desktop (Trixie): the same three checks as above.

## C. Stick

- [ ] `sudo footage-offload init-stick /dev/sdX1` adopts an ext4 stick. The marker file `.footage-offload-station` is created.
- [ ] `sudo footage-offload init-stick /dev/sdX1` adopts an exFAT stick. The marker file is created.
- [ ] An unmarked stick is ignored. The camera is not wiped while only an unmarked stick is connected.
- [ ] Desktop only: a stick auto-mounted by the desktop is taken over by the service.
- [ ] Safe eject from the web interface unmounts the stick and it can be removed without data loss.

## D. Transfer

- [ ] The HERO6 has videos, including one recording over 4 GB that is split into chapters.
- [ ] The HERO6 has photos.
- [ ] The HERO6 has LRV and THM files.
- [ ] After the run, every file on the stick matches the size on the camera.
- [ ] After the run, the camera has no media left.
- [ ] LRV and THM files are present on the stick.
- [ ] The folder name on the stick is the Pi time at plug-in.
- [ ] An empty camera creates no folder on the stick and no entry in the history.
- [ ] After de-authorise, unplugging and plugging the camera in again is recognised as an empty run.

## E. Faults

- [ ] Unplug the camera in the middle of a file. No data is lost on the stick. The run is reported as interrupted. The temporary file is removed on the next start of the service.
- [ ] Pull the stick out in the middle of a run. The run stops and is reported. The camera keeps its files.
- [ ] Cut the power in the middle of a run. After restart, check the stick for partial files and check the camera for files it still holds. Note the result: ______
- [ ] Stick full, with older runs on it:
  - [ ] Rotation deletes the oldest run first.
  - [ ] The current run is never deleted.
- [ ] Stick full, with no older runs on it: the run stops and the rest of the footage stays on the camera.
- [ ] Read-only stick: the run does not start, and the web interface says why.

## F. Charging and power

- [ ] The camera charges overnight while de-authorised. The battery indicator on the camera shows charging in the morning.
- [ ] With a weak supply, the undervoltage warning appears in the web interface.
- [ ] One 2.5 A supply into the board only. The system runs through a full transfer without the undervoltage warning.

## G. Clock

- [ ] With the Ethernet cable unplugged at boot, the NTP warning appears.
- [ ] With the camera clock set to a wrong date, the camera clock warning appears.
- [ ] Clock set over USB: the camera time matches the Pi time after the run.
- [ ] Clock set over Wi-Fi on the 2.4 GHz band. The SSID and password are entered in Settings. The camera time matches the Pi time after the run.

## H. Desktop mode

- [ ] With the Desktop edition, the file manager and gvfs do not open the camera when it is plugged in.

## I. Web interface

- [ ] All pages work in English, German, French and Italian on a phone.
- [ ] Gallery thumbnails show for photos and videos.
- [ ] LRV files play back in the gallery.
- [ ] Original download works. A download that is interrupted resumes.
- [ ] Password can be switched on and off in Settings.
- [ ] `sudo footage-offload reset-password` resets the password.
- [ ] Shutdown and reboot work from the System page.
- [ ] The diagnostics bundle downloads and contains no footage.

## J. Other MTP device

Use a spare device only.

- [ ] Warning: an Android phone (or any other MTP device) in file transfer mode WILL be wiped. Only the DCIM folder is wiped. Do not test with a device that holds data you need.
- [ ] Test the wipe on the spare device and confirm only DCIM was emptied.

## K. Removal

- [ ] `sudo apt remove footage-offload` removes the programme and keeps the configuration.
- [ ] `sudo apt purge footage-offload` removes the programme, the configuration and the service user.
- [ ] Footage on the stick is untouched after both steps.

## Results

| Date | Tester | Pi model | OS release | Package version | Camera firmware | Result |
| ---- | ------ | -------- | ----------- | --------------- | --------------- | ------ |
|      |        |          |             |                 |                 |        |
