# Contributing

Thank you for helping with Footage Offload Station.

## Running the tests

Install the test dependencies from apt. Do not use pip.

```sh
sudo apt install python3-flask python3-pyudev python3-pytest libgphoto2-6
```

Then run the tests from the top of the repository:

```sh
python3 -m pytest -q
```

## Simulated camera

Most tests use a simulated camera, so they run without any hardware. Use real hardware only for the checks in `docs/HARDWARE_CHECKLIST.md`.

## Building the package

Build the Debian package with:

```sh
packaging/build-deb.sh
```

## Coding rules

- Python 3.11 compatible.
- Dependencies come from apt only. Do not add pip dependencies.
- Use British English in code comments, documentation and user-facing text.
- User-facing strings are message ids. Each id is translated in all four files in `footage_offload/data/i18n/*.json` (English, German, French and Italian).
- French and Italian texts are reviewed by a native speaker before a release.
- The web UI has no explanatory text: no hints under fields, no notes, notices or descriptions next to controls. Labels, values, status, warnings and errors only. Explanations belong in the README.

## Device reports

If you test a camera or other device, please send a device report using the "Device report" issue template. It asks for the device, the firmware, the Pi and its operating system, and the results of each test. Do not attach footage.
