# Tool reference

Every tool of both modes, with its parameters, what it returns and the rules
it applies. See the [README](../README.md) for installation, mode selection,
configuration and the safety design.

Kinds: **read** only queries; **local write** writes into
`SCANNER_DOWNLOAD_DIR` and leaves the scanner unchanged; **write** changes data
on the scanner or bridge without moving the instrument; **actuating** moves
the instrument and fires its laser; **destructive** stops work or deletes data.

Angles are radians throughout. HA is the horizontal angle, 0..2π. VA is the
vertical angle measured down from the zenith, 0..π.

- [Direct mode](#direct-mode)
  - [Always registered](#always-registered)
  - [Only with SCANNER_ALLOW_ACTUATION](#only-with-scanner_allow_actuation)
  - [Aiming an area scan](#aiming-an-area-scan-direct-mode)
- [Bridge mode](#bridge-mode)
  - [Prompt: run_recipe](#prompt-run_recipe)

## Direct mode

The server talks to the scanner's HTTP API at `SCANNER_BASE`. Paths below are
relative to it.

### Always registered

| Tool | Kind | Parameters | Returns |
|---|---|---|---|
| `scanner_status` | read | — | The scanner's status (battery, AC state, levelling, on-board storage, IMU, laser pointer, Wi-Fi configuration, active error list), with secret-looking fields masked as `***`. `GET status` |
| `scanner_projects` | read | — | List of projects: `id`, `projectName`, `projectUUID`, `nextProjectScanID`. `GET projects` |
| `scanner_scans` | read | — | List of compact scan records (below). `GET scans` |
| `scanner_scan_modes` | read | — | The scan-mode menu: mode name, range (m), point count, file size, scan time (s), spacing, rate. Use the names in launches. `GET scans/modes` |
| `scanner_errors` | read | — | Current errors: `type`, `errorLevel`, `description`. `GET errors` |
| `scanner_tasks` | read | — | Running or queued tasks: `id`, `taskUUID`, `type`, `state`, `percent` (0–100), `scanID`, `exitResult`. Empty list = idle. `GET tasks` |
| `scanner_wait_for_scan` | read | `timeout_s` int = 900 (clamped 1–7200); `poll_s` float = 2.0 (clamped 0.5–30) | Polls `tasks` until it is empty or the timeout passes. Sends MCP progress notifications (percent of the first task) when the client asked for them. Returns `{done, timed_out, elapsed_s, last}`; `last` is the last task snapshot seen (`null` if the scanner was already idle). |
| `scanner_scanfile_info` | read | `scan_file_id` int | Metadata of one point-cloud file: path, size, point count, whether downloaded. `GET tzfs/{id}` |
| `scanner_download_scanfile` | local write | `scan_file_id` int; `filename` str = `scan-<id>.tzf`; `overwrite` bool = false | Streams the `.tzf` into the download directory. Returns `{scanFileId, bytes, path}`. `GET tzfs/{id}/file` |
| `scanner_download_preview` | local write | `scan_file_id` int; `filename` str = `scan-<id>-preview.jpg`; `overwrite` bool = false | Streams the scan's panoramic preview JPEG into the download directory. Returns `{scanFileId, bytes, path}`. `GET tzfs/{id}/preview` |
| `scanner_preview_to_angles` | read (local only, no scanner call) | `scan_filename`, `preview_filename` (plain names in the download directory); `x0`, `y0`, `x1`, `y1` int (preview pixels, y down); `margin_deg` float = 0.5 | Area-scan angles for a box on the preview. Returns `start_ha`, `delta_ha`, `start_va`, `delta_va` (radians, 6 decimals), `degrees: {ha: [start, delta], va: [start, delta]}`, and `preview_cells_per_px: [x, y]`. |

**`scanner_scans` records**

| Field | Meaning |
|---|---|
| `id` | List position on the scanner. Changes when scans are deleted. |
| `scanUUID` | Stable identifier. Use it for `scanner_delete_scan`. |
| `scanMode` | Mode the scan was taken with. |
| `nbPoints` | Point count of its file. |
| `fileSizeMB` | File size in MB (10⁶ bytes), one decimal. |
| `scanFileId` | Id of the point-cloud file. **This** is the `scan_file_id` for `scanner_scanfile_info` and the download tools; it is not the same as `id`. |
| `file` | File path on the scanner, `<project folder>/<name>.tzf`. |
| `project` | The project folder (the part of `file` before the first `/`), or `null`. |
| `downloaded` | The scanner's own downloaded flag for the file. |

**Download rules** (both download tools): `filename` must be a plain name
(starts with a letter or digit, then up to 127 letters, digits, `.`, `_`,
`-`; no `..`). The target must resolve directly inside the download
directory. Existing files are kept unless `overwrite` is true. Data streams to
`<name>.part` and is renamed when complete.

**`scanner_preview_to_angles` rules**

- Both files must already be in the download directory: the `.tzf` and the
  preview of the **same** scan, taken from the station the instrument is still
  standing on. The tool reads the grid size, angle steps and start angles from
  the `.tzf` and the image size from the JPEG.
- The box must lie inside the preview with `y0 < y1`. `x1 < x0` means the box
  wraps across the panorama seam. `x1 == x0` is refused.
- `margin_deg` (negative values count as 0) pads every side.
- `start_ha` wraps into [0, 2π); `delta_ha` is capped at 2π; VA is clamped to
  [0, π].
- A truncated or malformed file fails with `<name> is truncated or not a scan
  file` or `<name> is a truncated or malformed JPEG`.

### Only with `SCANNER_ALLOW_ACTUATION`

These are not registered unless `SCANNER_ALLOW_ACTUATION` is `1`, `true` or
`yes`.

| Tool | Kind | Parameters | Returns / behaviour |
|---|---|---|---|
| `scanner_create_project` | write | `project_name` str; `project_uuid` str (supply a fresh UUIDv4) | `POST projects` with `{projectName, projectUUID}`; returns the created record. The UUID format and name uniqueness are not checked by this server. Allowed while a task is running (a new project does not touch it). |
| `scanner_launch_scan` | **actuating** | `project_name` str; `project_uuid` str; `scan_mode` str (from `scanner_scan_modes`); `color` bool = false (also capture images; slower) | Full-dome scan. Runs the launch checks, then `POST scans` with `scanType: FullDome`, `fastMode: false`, `colorMode: ON/OFF`. Returns the task summary (as in `scanner_tasks`). Follow with `scanner_wait_for_scan`. |
| `scanner_launch_area_scan` | **actuating** | as `scanner_launch_scan`, plus `start_ha`, `delta_ha`, `start_va`, `delta_va` float (radians; get them from `scanner_preview_to_angles`) | Scan of an angular window: `scanType: AreaScan` with `startHA`, `deltaHA`, `startVA`, `deltaVA`. Same checks. A small window without color takes about a minute, mostly levelling. Area scans are saved as `<station>_<n>.tzf` next to the station's full-dome scan. |
| `scanner_cancel_task` | **destructive** | `task_uuid` str (from `scanner_tasks`) | Looks up the task's current `id` and sends `DELETE tasks/{id}`. Returns `{cancelled, status}`; fails if no such task is running or the scanner answers 400 or above. See the note below on partial scans. |
| `scanner_delete_scan` | **destructive** | `scan_uuid` str (from `scanner_scans`) | Refused while any task is running or queued. Permanently deletes the scan and its file, by UUID, verified. Returns `{deleted: "scan", scanUUID, id_at_delete}`. |
| `scanner_delete_project` | **destructive** | `project_uuid` str (from `scanner_projects`) | Refused while any task is running or queued. Permanently deletes the project and its scans, by UUID, verified. Returns `{deleted: "project", projectUUID, id_at_delete}`. |

**Launch checks**, in order (both launch tools): angle ranges (area scan:
`delta_ha` in (0, 2π], `0 ≤ start_va`, `0 < delta_va`,
`start_va + delta_va ≤ π`; `start_ha` is wrapped into [0, 2π)); refuse if any
task is running or queued; `project_uuid` parses as a UUID and a project with
that name **and** UUID exists; `scan_mode` is a known mode. Nothing is sent
until all pass. After sending, every field must be echoed back unchanged in
the task's `scanRequest`, or the task is cancelled at once and the tool fails.

**Delete checks**: both delete tools first refuse, like the launch tools, if
any task is running or queued (`instrument is busy: …`); a scan being filed
renumbers the lists the verification relies on. Then the tool lists scans or
projects, finds exactly one record with the UUID, deletes it by its current
`id`, lists again, and succeeds only if the UUID is gone and the list is one
shorter.

**Cancelling a scan**: the scan stops, but the instrument still files the
partial sweep as a scan with a valid `.tzf` (confirmed on a real X9, see
[PROVENANCE.md](../PROVENANCE.md)). Check `scanner_scans` after cancelling
and delete the partial scan if it is not wanted.

### Aiming an area scan (direct mode)

A full-dome scan file records the angles of its first row and column, so a
region picked on its preview maps straight to instrument angles while the
instrument stays on the same station:

1. `scanner_launch_scan` (any mode) → `scanner_wait_for_scan`
2. `scanner_scans` to find the new scan's `scanFileId`
3. `scanner_download_scanfile` and `scanner_download_preview` of that file
4. Pick a box on the preview → `scanner_preview_to_angles`
5. `scanner_launch_area_scan` with the four returned angles

If the instrument is moved, take a new full-dome scan before aiming again.

## Bridge mode

Every tool is one call to the bridge's API at `SCANNER_BRIDGE_URL` with
`Authorization: Bearer <SCANNER_BRIDGE_TOKEN>`. The bridge applies its rules
and returns problem responses that this server turns into
`<code>: <title> — <detail> (hint: <hint>)` errors; an error reply without a
problem body becomes `HTTP <status>: <body>`, and a success reply that is not
JSON becomes `bridge sent a response that is not JSON (…)`. Fields named below are the
ones the bridge returns; see the bridge's own documentation for full schemas.

| Tool | Kind | Parameters | Returns / behaviour | Bridge call |
|---|---|---|---|---|
| `scanner_status` | read | — | Connection, busy, battery, storage, levelling, whether the scanner moved since its last scan, queue length | `GET /status` |
| `scanner_scan_modes` | read | — | Modes: name, range (m), angular step (rad), full-dome point count, fixed overhead (s), full-dome time (s) | `GET /scan-modes` |
| `scanner_projects` | read | — | Projects: `project_id`, `name`, `scan_count` | `GET /projects` |
| `scanner_create_project` | write | `name` str (1–64 characters, no `/`; checked by the bridge) | The project, with its `project_id` | `POST /projects` |
| `scanner_scans` | read | `project_id` str, optional | Scans, newest first: `scan_id`, `kind` (`full_dome` or `area`), station, mode, point count, parent station scan for area scans, timings when the bridge ran them | `GET /scans[?project_id=]` |
| `scanner_scan` | read | `scan_id` str | One scan record | `GET /scans/{id}` |
| `scanner_start_scan` | **actuating** (queued) | `project_id` str; `mode` str; `color` bool = false (adds about 150 s); `start_ha`, `delta_ha`, `start_va`, `delta_va` float, optional; `note` str, optional | Queues a scan and returns the job. No window = full dome; all four angles = area scan (`start_ha` is wrapped into [0, 2π)); giving only some angles is refused locally. With an agent token the job usually waits in `awaiting_approval` until a person approves it, unless a grant covers it. | `POST /scans` |
| `scanner_job` | read | `job_id` str | State (`awaiting_approval`, `queued`, `running`, `succeeded`, `failed`, `cancelled`), phase, progress 0–1, result, any problem | `GET /jobs/{id}` |
| `scanner_wait_for_job` | read | `job_id` str; `timeout_s` int = 900 (clamped 1–7200); `return_on_approval` bool = true | Polls every second. Returns `{job, done, timed_out, waiting_for_approval, elapsed_s}`, plus a `note` when it returns early because the job needs a person's approval (call it again after approval). Sends progress notifications (0–100) when the client asked for them. | `GET /jobs/{id}` |
| `scanner_cancel_job` | **destructive** | `job_id` str | Cancels a job of yours; a running scan is stopped; the partial sweep is kept as a scan, named by the job's `partial_scan_id` (bridge 0.1.1+) | `POST /jobs/{id}/cancel` |
| `scanner_preview` | read | `scan_id` str | Two content items: a text line with the image size in pixels, and the preview as a JPEG image the agent can look at (grey laser-intensity panorama for a full dome, close-up for an area scan). Boxes for `scanner_aim` use these pixels, y down; `x1 < x0` wraps across the seam. | `GET /scans/{id}/preview` |
| `scanner_aim` | read | `reference_scan_id` str; `x0`, `y0`, `x1`, `y1` int; `margin_deg` float = 0.5 (negative counts as 0; sent as `margin_rad`) | An area-scan `window` with four angles to pass to `scanner_start_scan`. The bridge refuses if the scanner moved since the reference scan. | `POST /aim` |
| `scanner_export` | write (on the bridge) | `scan_id` str; `format` `ply` (default) or `las`; `intensity` `auto` (default), `preview` or `none` | Starts decoding the scan to levelled points; returns a job. When it succeeds, `result.export_id` identifies the export. | `POST /scans/{id}/exports` |
| `scanner_export_info` | read | `export_id` str | Point count, bounds, `format`, and automatic checks (point count, levelled, coordinates, intensity source), each with `ok` | `GET /exports/{id}` |
| `scanner_download_export` | local write | `export_id` str; `filename` str = `<export_id>.<format>` (`.copc.laz` for COPC); `overwrite` bool = false | Downloads the export into the download directory. Returns `{export_id, bytes, path}`. | `GET /exports/{id}`, then `GET /exports/{id}/file` |
| `scanner_download_scanfile` | local write | `scan_id` str; `filename` str = `<scan_id>.tzf`; `overwrite` bool = false | Downloads the raw `.tzf`. Returns `{scan_id, bytes, path}`. | `GET /scans/{id}/tzf` |
| `scanner_recipes` | read | — | Installed recipes: `recipe_id`, `title`, `version`, `summary` | `GET /recipes` |
| `scanner_recipe` | read | `recipe_id` str | A recipe's inputs and steps (`action`, `person` and `check` steps) | `GET /recipes/{id}` |
| `scanner_start_run` | write (its steps may queue scans) | `recipe_id` str; `project_id` str; `inputs` object, optional | Starts a run; the bridge executes the steps. It pauses with `waiting_for: approval` (a person must approve a scan) or `waiting_for: input` (a person step; supply its `needs` with `scanner_advance_run`). | `POST /runs` |
| `scanner_run` | read | `run_id` str | State, what it is waiting for, current step, each step's state, job and output | `GET /runs/{id}` |
| `scanner_sign_off` | write (asks the person) | `run_id` str; `summary` str, optional; `person_said` str and `corrections` object, only when the chat couldn't ask and the person answered in plain chat (relayed; recorded as such) | Asks the person in the chat (MCP elicitation) to confirm the run's waiting sign-off step or type corrected numbers, then advances it with `SCANNER_PERSON_TOKEN`. Refuses a `review = final` step (the agent's own) and a step with no proposal. Returns `{signed_off, corrected, run}`, or `asked: false` when the chat can't ask | `GET /runs/{id}`, `GET /recipes/{id}`, `POST /runs/{id}/advance` |
| `scanner_advance_run` | write | `run_id` str; `step_id` str; `input` object | Completes a person step with its `needs`, e.g. `{"box": {"x0": …, "y0": …, "x1": …, "y1": …}}` drawn on the preview of the step named in `preview_of` | `POST /runs/{id}/advance` |
| `scanner_measure` | write | `scan_id` str; `kind` str, default `opening` | Starts a measurement job on the bridge: a window's finished opening from a levelled close-up with wall on every side. Bridge 0.4.0+ | `POST /scans/{id}/measurements` |
| `scanner_measurement` | read | `measurement_id` str | The measurement (values in metres with uncertainties, checks, details) as JSON, plus its evidence image (wall face grey, opening green, measuring row and column red) | `GET /measurements/{id}` and `/evidence` |
| `scanner_permissions` | read | `project_id` str, optional | Active grants: `project_id`, `expires_at`, `max_scans`, `scans_used`, `granted_by`, and `run_id` when it covers one run only. With a grant for the project, a run's scans need no approval | `GET /grants` |
| `scanner_registration` | read | `project_id` str | The registration: `summary`, `stations` (`placed`, `turn_deg`, `xyz_m`), fitted `pairs`, `walls` ({`wall_id`, `from`, `to`, `length_m`, `z_m`, `seen_by`}), `registering_job_id`; plus the plan picture once a station is placed. Bridge 0.9.0+ | `GET /projects/{id}/registration`, `GET …/registration/plan` |
| `scanner_processing` | read | none | `workers` (each worker's name, token name, seconds since seen, job and its `job_kind`: `register`, `check` or `save`), `runs_on` (the workers or "the Pi"), and `start_worker`: the release binary, the token file and the command for a laptop worker. Bridge 0.10.0+ | `GET /workers` |
| `scanner_minimap` | read | `project_id` str | The registration summary and the stations not yet trusted, and the line map as a PNG | `GET /projects/{id}/registration`, `GET /projects/{id}/minimap` |
| `scanner_wall_face` | read | `project_id` str; `wall_id` str, or `from_xy`, `to_xy` [x, y] (project frame, m) | `seen_share` of the face's length (0.8 is covered), `spacing_mm`, `height_m`, `stations`, `unseen` stretches. Changes nothing | `POST /projects/{id}/registration/face` |
| `scanner_propose_station` | write | `scan_id` str; `note` str (directions in prose, by landmarks); `marker_x`, `marker_y` int (preview pixels, on the ground); `marker_label` str; `labels` [{x, y, text, kind}]; `boxes` [{x0, y0, x1, y1, text, kind}]; kind seen / hidden / landmark | Draws "SET UP HERE" and the annotations on the preview, shown in the bridge web UI; returns the proposal JSON and the picture. Bridge 0.8.0+ | `PUT /scans/{id}/next-station`, `GET …/next-station/image` |
| `scanner_set_mission` | write | `project_id`; `goal` (the person's words); `kind` exterior / windows / room / custom; `done_when` (criteria in prose); `targets` [{target_id, name, kind, …}], optional | Sets the project's mission, replacing any earlier one. Bridge 0.8.0+ | `PUT /projects/{id}/mission` |
| `scanner_mission` | read | `project_id` | Goal, done_when, state (active / ready / signed_off), ledger | `GET /projects/{id}/mission` |
| `scanner_update_targets` | write | `project_id`; `targets` [{target_id, name, kind, status, stations, measurement_id, note}] | Upserts by target_id. covered/measured name their scans (measured also its measurement); declined says why | `POST /projects/{id}/mission/targets` |
| `scanner_ready_to_leave` | write (asks the person) | `project_id`; `summary`; `person_said` (relay only) | Ready (refused, naming them, while targets are open), then the person's sign-off in the chat or relayed; from bridge 0.18.0 also `save`, the save the sign-off started | `POST …/mission/ready`, `POST …/mission/sign-off`, `GET …/save` |
| `scanner_save` | write | `project_id` | Saves the project to a folder on the laptop worker (scan files, each placed station as COPC in the project frame, registration, mission, manifest with checksums); returns the save `job`, the `last` save and a `message`. Waits for a laptop however long it takes. Bridge 0.18.0+ | `POST /projects/{id}/save`, `GET …/save` |
| `scanner_saved` | read | `project_id` | `last` (laptop, folder, when, files, bytes, each station), `job` (a save running or waiting) and a `message`. Bridge 0.18.0+ | `GET /projects/{id}/save` |
| `scanner_load_list` | write | `project_id`, `schedule_csv` or `list_json`, `revision`, `title` | Loads a door and window schedule (mark, type, width, height, tolerance, wall) or a list in the bridge's list format as the project's mission, one line per mark; a new revision keeps the answers of unchanged lines. Bridge 0.19.0+ | `PUT /projects/{id}/list` |
| `scanner_record` | read | `project_id` | The list's record: each line's status, conformance, evidence and reason. Bridge 0.19.0+ | `GET /projects/{id}/record` |
| `scanner_bind_lines` | write (asks the person) | `project_id`, `place` or `lines`, `wall_id` or `from_xy`/`to_xy`, `person_said` | Asks the person, then binds the lines to the wall face with their token. Bridge 0.19.0+ | `POST /projects/{id}/list/bind` |
| `scanner_count` | write | `project_id` | Counts the openings on each bound face and judges the lines: `say`, `faces` (openings with sizes or why they don't fit, gaps not counted, seen share), `proposed` matches, `lines`, and each face's picture. Bridge 0.19.0+ | `POST /projects/{id}/count`, `GET …/count/{face}/image` |
| `scanner_confirm_matches` | write (asks the person) | `project_id`, `matches` [{`line_id`, `opening_id`}], `person_said` | Asks the person, then records which opening is which line; returns the lines judged. Bridge 0.19.0+ | `POST /projects/{id}/count/matches` |
| `scanner_coverage` | read | `scan_id` str | What the scan covered: point spacing at full resolution (p10/p50/p90 and the share fine ≤ 6 mm, good ≤ 12, coarse ≤ 25, sparse), and a plan in 10 cm cells with seen and shadow areas, as JSON, plus the plan image (green seen, red shadow, white cross the scanner). Bridge 0.6.0+ | `GET /scans/{id}/coverage` and `/coverage/image` |
| `scanner_propose` | write | `run_id` str; `step_id` str; `input` object; `note` str, optional | Proposes a waiting person step's `needs` (the agent's box, a measurement) for a person to confirm or change in the bridge app; the step keeps waiting. Bridge 0.3.0+ | `POST /runs/{id}/propose` |
| `scanner_cancel_run` | **destructive** | `run_id` str | Stops the run; a scan in flight is cancelled too | `POST /runs/{id}/cancel` |
| `scanner_log` | read | `project_id` str, optional; `since` str (RFC 3339), optional | The field log, oldest first | `GET /log` |
| `scanner_add_note` | write | `text` str; `project_id` str, optional | Adds an observation to the field log | `POST /log` |

Not present in bridge mode, by design: approving jobs, granting scans, and
deleting scans or projects. These belong to a person using the bridge app or
an operator token.

Ids are sent as one URL path segment each, with `/`, `?`, `#`, `%` and other
reserved characters escaped; an empty id, `.` or `..` is refused locally.

The default download names are `<scan_id>.tzf` and `<export_id>.<format>`,
made plain: each run of characters other than letters, digits, `_` and `-` in
the id becomes `_`, leading `_`/`-` are dropped, and the name is cut to 128
characters (`download.<ext>` if nothing is left). Pass `filename` to choose
the name yourself.

A typical bridge-mode scan: `scanner_status` → `scanner_projects` /
`scanner_create_project` → `scanner_start_scan` → `scanner_wait_for_job`
(ask the person to approve if it returns `waiting_for_approval`, then wait
again) → `scanner_export` → `scanner_wait_for_job` → `scanner_export_info` →
`scanner_download_export`.

An area scan: `scanner_preview(<station scan>)` → pick a box →
`scanner_aim` → `scanner_start_scan` with the four `window` angles.

### Prompt: `run_recipe`

| Argument | Meaning |
|---|---|
| `recipe_id` | The recipe to run (from `scanner_recipes`) |
| `project_id` | The project to run it in |

The prompt fetches the recipe from the bridge (`GET /recipes/{id}`) and
returns a text plan for the agent:

- the recipe's title, version and summary;
- the inputs to ask the person for, with their prompts and allowed choices;
- to call `scanner_start_run` with those inputs and follow it with
  `scanner_run`;
- each step, numbered, with how it is handled: `action` steps are run by the
  bridge itself; `person` steps are completed with `scanner_advance_run`
  (looking at `scanner_preview` of an earlier step's scan first when the step
  names one); `check` steps are verified by the bridge;
- to ask the person to approve in the bridge app whenever the run waits for
  approval;
- to report each measured value with its uncertainty, and to say plainly if a
  check failed or a step was declined.
