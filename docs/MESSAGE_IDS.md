# Message ids emitted by the backend

Every catalogue in `footage_offload/data/i18n/` must translate all of these.
`{param}` placeholders are filled from the `params`/`detail` the backend sends.

## Errors (`status.error.id`, history `error_id`, per-file `errors[].id`)

| Id | Meaning |
|---|---|
| `error.no_stick` | No adopted stick (marker file missing); nothing copied or deleted |
| `error.stick` | Stick error: read-only, I/O error, removed. Run stopped |
| `error.stick_full` | Not enough room even after deleting old runs; rest stays on the camera |
| `error.camera_gone` | Camera disconnected during the run |
| `error.camera` | Camera error after retries |
| `error.camera_open` | Camera could not be opened |
| `error.copy_failed` | One file could not be copied after three retries; it stays on the camera |
| `error.delete_failed` | Copy verified, but deleting on the camera failed |
| `error.some_files_failed` | Run finished, some files failed (`detail` = count) |
| `error.cancelled` | Run cancelled (service stopping) |
| `error.interrupted` | Run interrupted by power loss or crash (found at start) |
| `error.internal` | Unexpected internal error |
| `error.busy` | Action not possible during a transfer |
| `error.wrong_password` | Login failed |
| `error.login_required` | Session expired / not logged in |
| `error.csrf` | Missing or wrong CSRF token |
| `error.helper` | Root helper failed (`detail`) |

## Settings validation (`errors[key]` from `PUT /api/v1/settings`)

`error.type_bool`, `error.type_int`, `error.type_number`, `error.type_string`,
`error.out_of_range`, `error.invalid`, `error.unknown_setting`,
`error.ssid_length`, `error.wifi_password_length`, `error.invalid_host`,
`error.invalid_topic`, `error.required`, `error.password_not_set`,
`error.password_too_short` (minimum 8 characters).

## Warnings (`status.warnings[].id`, with `params`)

| Id | Params | Meaning |
|---|---|---|
| `warning.ntp_not_synchronised` | – | Pi clock not NTP-synchronised; folder names may be wrong |
| `warning.camera_clock_wrong` | `camera_time` (ISO), `difference_s` | Camera clock far off |
| `warning.camera_clock_set_failed` | `detail` | Setting the camera clock failed (transfer unaffected) |
| `warning.camera_clock_set` | `method` (`usb` or `wifi`) | Camera clock set (informational) |
| `warning.rotation_deleted` | `folder` | Rotation deleted an old run folder |
| `warning.deauthorize_failed` | `detail` | Camera could not be released from USB |
| `warning.undervoltage_now` | – | Undervoltage right now |
| `warning.undervoltage_occurred` | – | Undervoltage since boot |
| `warning.stick_unadopted` | `device` | A USB stick without marker file is connected and ignored |
| `warning.nas_failed` | `detail` | Onward copy to NAS failed |
| `warning.update_available` | `version` | Newer release on GitHub |
