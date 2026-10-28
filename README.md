# TrimbleX9-mcp

An [MCP](https://modelcontextprotocol.io) server for the **Trimble X9**
terrestrial laser scanner. It gives an AI agent tools to read the scanner's
status, list projects and scans, read the scan-mode menu, download point-cloud
files and previews, and, behind an explicit opt-in, create projects and start
full-dome or area scans.

It runs in one of two modes:

- **Direct mode** talks to the scanner's own local HTTP/REST API (`/api/v1`)
  over its USB or Wi-Fi link. The safety checks run in this server.
- **Bridge mode** talks to a separate x9 bridge service that sits next to the
  scanner and enforces the rules (approvals, battery, busy scanner, echo
  checks) for every client. The tools are thin calls to the bridge's API.

<p align="center">
  <img src="https://raw.githubusercontent.com/northidaholabs/trimble-x9-bridge/main/docs/img/connections.svg" width="900"
       alt="In bridge mode on a field kit: the agent on the laptop runs this MCP server on a Raspberry Pi over ssh on a USB-C cable; the server calls x9-bridge, which drives the Trimble X9 over the scanner's Wi-Fi or USB.">
</p>

What the agent gets back is something it can look at. The minimap of a
19-station colour scan of an administration building, redrawn by
`scanner_minimap` after every station:

<p align="center">
  <img src="https://raw.githubusercontent.com/northidaholabs/trimble-x9-bridge/main/docs/img/admin-minimap.png" width="560"
       alt="Minimap of the administration building: building walls, low walls and vegetation as lines, and numbered station dots.">
</p>

And where to set up next: `scanner_plan_station` picks the spot from the
station's points, and `scanner_propose_station` shows the person the spot,
the landmark beside it and a tripod drawn its real size there, with its
shadow on the ground:

<p align="center">
  <img src="https://raw.githubusercontent.com/northidaholabs/trimble-x9-bridge/main/docs/img/admin-next-station.jpg" width="560"
       alt="Station 1's preview: the clock tower marked as seen, the wing's far side as hidden, a lamp post as the landmark, and a tripod drawn its real size on the pavement beyond it, casting a shadow.">
</p>

Tested with Claude Opus 5.5 and Grok 4.6 as the model in the chat: given the
same plan, both put the tripod on the same spot of pavement.

**Interoperability** project: written from scratch against the instrument's
network API, with behavior verified on a live instrument. No vendor code,
firmware or vendor documentation is included or redistributed. How the API
knowledge was obtained is recorded in [PROVENANCE.md](PROVENANCE.md).
Independent project, **not affiliated with or endorsed by Trimble Inc.**;
"Trimble" and "X9" are trademarks of their respective owners.

Contents: [Install](#install) · [Modes](#modes) ·
[Configuration](#configuration) · [Tools](#tools) · [Safety](#safety) ·
[Errors](#errors) · [Client configuration](#client-configuration) ·
[Compatibility](#compatibility) · [Tests](#tests) · full tool reference in
[docs/tools.md](docs/tools.md).

## Install

Python 3.10 or later. Install from the GitHub release tag; this provides the
console script `trimblex9-mcp`, which speaks MCP over stdio.

```bash
pip install "git+https://github.com/northidaholabs/TrimbleX9-mcp@v0.15.0"
# or, isolated:
pipx install "git+https://github.com/northidaholabs/TrimbleX9-mcp@v0.15.0"
# or run without installing:
uvx --from "git+https://github.com/northidaholabs/TrimbleX9-mcp@v0.15.0" trimblex9-mcp
# no git on the host (e.g. a minimal Raspberry Pi image):
pip install https://github.com/northidaholabs/TrimbleX9-mcp/archive/refs/tags/v0.15.0.tar.gz
```

Dependencies: `mcp` and `httpx` (see [Compatibility](#compatibility)).

### On a Raspberry Pi without internet

A field Pi is often on the scanner's Wi-Fi, with no internet. Build a
wheelhouse on a machine that has internet, for the Pi's Python (Raspberry Pi
OS trixie: CPython 3.13, aarch64), copy it over and install from it:

```bash
# on a machine with internet
tag=v0.15.0
pip wheel --no-deps -w wheelhouse "git+https://github.com/northidaholabs/TrimbleX9-mcp@$tag"
pip download -d wheelhouse --only-binary=:all: --platform manylinux2014_aarch64 \
  --python-version 3.13 --implementation cp "mcp>=1.7,<3" "httpx>=0.27"
curl -L -o wheelhouse/trimblex9-mcp-auto \
  https://raw.githubusercontent.com/northidaholabs/TrimbleX9-mcp/$tag/extras/trimblex9-mcp-auto
scp -r wheelhouse <pi>:

# on the Pi
python3 -m venv ~/x9-bridge/.venv
~/x9-bridge/.venv/bin/pip install --no-index --find-links ~/wheelhouse trimblex9-mcp
install -D -m 0755 ~/wheelhouse/trimblex9-mcp-auto ~/x9-bridge/bin/trimblex9-mcp-auto
```

The launcher (`extras/trimblex9-mcp-auto`, from v0.12.1) starts the server for
a field kit; see
[On a field Pi, through the bridge](#on-a-field-pi-through-the-bridge-recommended).

`trimblex9-mcp` is meant to be started by an MCP client, not run by hand; on a
terminal it just waits for MCP messages on stdin.

## Modes

The mode is chosen once, at start-up, from the environment:

| `SCANNER_BRIDGE_URL` | `SCANNER_BRIDGE_TOKEN` | Result |
|---|---|---|
| unset or empty | (ignored) | **Direct mode**: talks to the scanner at `SCANNER_BASE`. |
| set | set | **Bridge mode**: talks to the bridge at `SCANNER_BRIDGE_URL`. |
| set | unset or empty | Exits at start-up with `bridge mode needs SCANNER_BRIDGE_URL and SCANNER_BRIDGE_TOKEN`. It does **not** fall back to direct mode. |

The two modes expose different tool sets (see [Tools](#tools)). In bridge mode
`SCANNER_BASE` and `SCANNER_ALLOW_ACTUATION` are ignored: the bridge decides
what a token may do.

### Direct mode

Run the server on the host the scanner is connected to (by USB, or on the
scanner's Wi-Fi). Out of the box the server is **read-only**: tools that move
the instrument, fire its laser or delete data are not registered at all
unless `SCANNER_ALLOW_ACTUATION` is set. With it set, the server validates
every scan request locally, refuses to start a scan or delete data while the
scanner is busy,
and cancels any scan the instrument did not accept exactly as sent.

### Bridge mode

The bridge is the only thing that talks to the scanner. This server sends
each call to the bridge's HTTP API with a bearer token the bridge issued,
usually with role `agent`. With an agent token, a scan waits in
`awaiting_approval` until a person approves it in the bridge app, unless the
person granted a set of scans in advance. Approval, grant and delete tools
are deliberately absent from this server. Bridge mode adds exports to
PLY/LAS, previews returned as images, aiming, recipes and a field log.

The bridge itself is a separate service and is not part of this repository.

## Configuration

All configuration is through environment variables.

| Variable | Mode | Default | Effect |
|---|---|---|---|
| `SCANNER_BASE` | direct | `http://192.168.42.1:34567/api/v1` | Base URL of the scanner's HTTP API. The default is the scanner over its USB network link; use its Wi-Fi address (e.g. `http://192.168.43.1:34567/api/v1`) when connected over Wi-Fi. A trailing `/` is removed. |
| `SCANNER_ALLOW_ACTUATION` | direct | unset (off) | `1`, `true` or `yes` (any case) registers the actuating and destructive tools. Any other value, including `on`, leaves them unregistered. |
| `SCANNER_DOWNLOAD_DIR` | both | `~/scanner-downloads` | Directory that download tools write into and that `scanner_preview_to_angles` reads from. `~` is expanded and the path is resolved (symlinks followed) at start-up. Created on first download. Nothing is written outside it. |
| `SCANNER_BRIDGE_URL` | bridge | unset | Bridge API base, e.g. `http://bridge.local:8795/v1`. Setting it selects bridge mode. A trailing `/` is removed. |
| `SCANNER_BRIDGE_TOKEN` | bridge | unset | Bearer token issued by the bridge, sent as `Authorization: Bearer <token>`. Required in bridge mode. |
| `SCANNER_PERSON_TOKEN` | bridge | unset | An `operator` token for the person at the scanner. Used only after they answer yes in the chat (MCP elicitation, which the model can't answer): to allow scanning for the visit and to sign off a run. The model never sees it. Without it, or with a client that can't ask, the person uses the bridge's web UI. |
| `SCANNER_VISIT_HOURS` | bridge | 8 | How long the permission given in the chat lasts (up to 20 scans) |

Timeouts are fixed in the code, not configurable:

| Request | Direct mode | Bridge mode |
|---|---|---|
| Ordinary API calls | 15 s (5 s to connect) | 30 s (5 s to connect) |
| File downloads (streamed) | 600 s (5 s to connect) | 600 s (5 s to connect) |
| `scanner_preview` image | — | 120 s (5 s to connect) |

The long-running waits (`scanner_wait_for_scan`, `scanner_wait_for_job`) poll
and take their own `timeout_s` argument (1 to 7200 s, default 900).

## Tools

A summary; parameters, return values and rules for each tool are in
**[docs/tools.md](docs/tools.md)**.

"Read" tools only query. "Local write" tools write files into
`SCANNER_DOWNLOAD_DIR` but do not change the scanner. "Actuating" tools move
the instrument or fire its laser. "Destructive" tools stop work or delete
data. Each tool carries matching MCP annotations (`readOnlyHint`,
`destructiveHint`).

### Direct mode, always registered

| Tool | Kind | What it does |
|---|---|---|
| `scanner_status` | read | Battery, AC, levelling, storage, IMU, laser pointer, Wi-Fi config (secrets masked), errors |
| `scanner_projects` | read | List projects |
| `scanner_scans` | read | List scans as compact records |
| `scanner_scan_modes` | read | The scan-mode menu (range, points, size, time, spacing, rate) |
| `scanner_errors` | read | Current error list |
| `scanner_tasks` | read | Running or queued tasks with percent complete; empty means idle |
| `scanner_wait_for_scan` | read | Wait until no task is running, reporting progress |
| `scanner_scanfile_info` | read | Metadata of one point-cloud file |
| `scanner_download_scanfile` | local write | Download a scan's `.tzf` point-cloud file |
| `scanner_download_preview` | local write | Download a scan's preview JPEG |
| `scanner_preview_to_angles` | read (local only) | Turn a box on a downloaded preview into area-scan angles |

### Direct mode, only with `SCANNER_ALLOW_ACTUATION`

| Tool | Kind | What it does |
|---|---|---|
| `scanner_create_project` | write | Create a project |
| `scanner_launch_scan` | **actuating** | Start a full-dome scan |
| `scanner_launch_area_scan` | **actuating** | Start a scan of an angular window |
| `scanner_cancel_task` | **destructive** | Stop a running task |
| `scanner_delete_scan` | **destructive** | Delete a scan and its file, by scanUUID; refused while busy |
| `scanner_delete_project` | **destructive** | Delete a project and its scans, by projectUUID; refused while busy |

A typical direct-mode scan: `scanner_scan_modes` → `scanner_projects` (or
`scanner_create_project`) → `scanner_launch_scan` → `scanner_wait_for_scan` →
`scanner_scans` → `scanner_download_scanfile`.

### Bridge mode

| Tool | Kind | What it does |
|---|---|---|
| `scanner_status` | read | Bridge and scanner state: connection, busy, battery, storage, levelling, moved-since-last-scan, queue length |
| `scanner_scan_modes` | read | Scan modes with range, angular step, point count and timings |
| `scanner_projects` | read | Projects with scan counts |
| `scanner_create_project` | write | Create a project by name |
| `scanner_scans` | read | Scans, newest first, optionally for one project |
| `scanner_scan` | read | One scan's record |
| `scanner_start_scan` | **actuating** (queued; usually needs approval) | Queue a full-dome or area scan |
| `scanner_job` | read | One job's state and progress |
| `scanner_wait_for_job` | read | Wait for a job, returning early if it needs approval |
| `scanner_cancel_job` | **destructive** | Cancel a job; a running scan is stopped |
| `scanner_preview` | read | A scan's preview, returned as an image the agent can see |
| `scanner_aim` | read | Turn a box on a preview into an area-scan window |
| `scanner_export` | write (on the bridge) | Decode a scan to levelled PLY or LAS |
| `scanner_export_info` | read | An export's point count, bounds and checks |
| `scanner_download_export` | local write | Download an export file |
| `scanner_download_scanfile` | local write | Download a scan's raw `.tzf` |
| `scanner_recipes` | read | Installed recipes |
| `scanner_recipe` | read | One recipe's inputs and steps |
| `scanner_start_run` | write (may lead to scans) | Start running a recipe |
| `scanner_run` | read | A run's state and steps |
| `scanner_advance_run` | write | Complete a person step of a run |
| `scanner_measure` | write | Measure a window opening in a scan on the bridge (bridge 0.4.0+) |
| `scanner_measurement` | read | A measurement's values, checks and evidence image |
| `scanner_permissions` | read | Standing permissions to scan: project, expiry, scans left, who gave them, and the run they cover |
| `scanner_plan_station` | read | Computes where to set up next from a full dome's points: up to three spots on open ground a person can walk to, a real step out, seeing the most of what this station couldn't (behind and beside a named subject, if given), each with its preview pixel and a landmark beside it (bridge 0.14.0+) |
| `scanner_propose_station` | write | Marks where to set up next on the station's preview: the tripod, the one landmark the person walks past, and a sentence naming it (no left, right or compass words). The bridge refuses a proposal that breaks that, shows it in its web UI and returns the picture (bridge 0.8.0+) |
| `scanner_set_mission` | write | Sets what the job is for (the person's goal, kind, done-when criteria) and its first ledger targets (bridge 0.8.0+) |
| `scanner_mission` | read | The mission, its state and its ledger |
| `scanner_registration` | read | The project's stations in one frame (registered by the bridge after each full dome), the walls found, and the plan picture (bridge 0.9.0+) |
| `scanner_processing` | read | Where registration runs: the laptop workers connected, or the Pi; how to start a worker on the laptop. Ask the person once per laptop (bridge 0.10.0+) |
| `scanner_sign_off` | write | Asks the person, in the chat, to confirm a run's sign-off step (or to correct the numbers); relayed with `person_said` where the chat can't ask |
| `scanner_minimap` | read | The job so far as a simple line map: walls, numbered stations (amber when not trusted), wall targets; redrawn after every station (bridge 0.11.0+) |
| `scanner_wall_face` | read | How much of a wall face the registered stations saw: share of its length, spacing, stations, unseen stretches (bridge 0.9.0+) |
| `scanner_update_targets` | write | Adds or updates ledger targets: open, covered (by stations), measured (with a measurement), declined (with a reason). A wall target can carry a question (`question: "plumb"`, bridge 0.15.0+), covered only by its answer, and the job's tolerance in the person's words |
| `scanner_ask` | write | Asks a question target of the point cloud now (bridge 0.15.0+): `answer` with plumb (mm and inches, over what height) and flatness and the stations it rests on, and within or outside the target's tolerance; `not_yet` with what is missing and the planner `subject` for each unseen stretch, or `does_not_fit`. Numbers only with an answer. Up to 90 s |
| `scanner_ready_to_leave` | write (asks the person) | Says the job is done (refused while a target is open) and asks the person to sign it off in the chat, or relays their words |
| `scanner_coverage` | read | A scan's point spacing and coverage plan (seen, shadows), with the plan image (bridge 0.6.0+) |
| `scanner_propose` | write | Propose a person step's answer (e.g. the box around the window) for a person to confirm (bridge 0.3.0+) |
| `scanner_cancel_run` | **destructive** | Stop a run and any scan in flight |
| `scanner_log` | read | The field log |
| `scanner_add_note` | write | Add a note to the field log |

### Prompt (bridge mode)

| Prompt | Arguments | What it does |
|---|---|---|
| `survey_mission` | `project_id`, `goal` | Runs a site visit toward one goal: asks once about processing on the laptop, asks which one when the view shows more than one thing the job could mean, sets the mission, scans station by station with the minimap and registration, proposes each next station by the landmark the person walks past, and ends with the one sign-off |
| `run_recipe` | `recipe_id`, `project_id` | Fetches the recipe from the bridge and returns step-by-step instructions for walking it with the person: which inputs to ask for, then `scanner_start_run` and `scanner_run`, how each `action` / `person` / `check` step is handled, to ask for approval when the run waits for it, and to report measured values with their uncertainty. |

Direct mode has no prompts.

## The person, in the chat

AI-first: the person says what they want and signs off the result; the agent
does the rest. Two moments need the person, and both are asked in the chat
itself, through MCP elicitation, where the answer comes from the person and
not from the model:

1. **Permission for the visit.** `scanner_start_run` and `scanner_start_scan`
   check for a grant in the project. Without one it asks once: "Let the agent scan in "<project>" for
   the next 8 hours (up to 20 scans)?". Yes creates the grant with
   `SCANNER_PERSON_TOKEN`; no starts nothing. Later runs in the visit ask
   nothing.
2. **The sign-off.** `scanner_sign_off(run_id, summary)` asks about the
   bridge's proposal at the run's sign-off step: confirm it, or change the
   numbers, which the form shows prefilled with the proposal. The agent shows the evidence image in the chat first. This is the
   run's one person intervention.

Clients (October 2026): the Claude Code CLI shows elicitation from 2.1.76.
The Claude desktop app's Code tab declares it but closes the question without
showing it (seen 2026-10-21), and the desktop chat and claude.ai don't support
it. A Claude Code `Elicitation` hook can answer without showing a dialog;
don't configure one for this server.

**When the chat can't ask** (or closes the question unshown), the tool says so
and tells the agent to ask the person in plain chat, then call again with
`person_said` = the person's exact words (and, for the sign-off, `corrections`
for numbers they changed). That is weaker: the model carries the person's
answer. So the bridge records it: the grant's `note` reads "relayed by the
agent: "…"", the sign-off's input has `confirmed_via`, and the run counts it in
`interventions.relayed`. Questions answered in a dialog are noted "asked in
the chat".

## Missions: a defined "done"

The person's sentence ("scan the exterior of the house", "measure all the
windows") becomes the project's mission: their goal, a kind, and done-when
criteria the agent writes, concrete enough to check. The agent keeps a ledger
of targets (each facade, each window) as it works: open, covered by named
stations, measured, or declined with a reason. It chooses the next station for
what is still open, and `scanner_ready_to_leave` is refused while anything is.
Then the person signs the job off once, in the chat (or relayed), and the
mission is done. The `survey_mission` prompt runs the whole visit.

From bridge 0.9.0 the bridge registers every full dome into one project
frame by itself. Facade targets then name their wall (`wall_id` from
`scanner_registration`), and the bridge covers them from the geometry: a
wall seen over 80% of its length is covered, with the stations that saw it,
and `covered` is refused while less is seen.

## Safety

For agents, in short:

- **Permission:** scans run under a grant the person gives once per visit, in
  the chat. Never use the operator (person) token yourself; the server uses it
  only after the person said yes.
- **Secrets:** never print, paste or log tokens or the scanner's Wi-Fi
  passphrase. Keep tokens in files (`~/x9-bridge/*.token`), not in client
  settings: prefer the launcher over `--env SCANNER_BRIDGE_TOKEN=…`, which
  stores the token in the client's config.
- **The laser pointer** (the scanner's own `laser-pointer` API, not exposed by
  these tools): only with the person at the scanner and having said so, and
  never without explicit angles aimed at the floor.

### Read-only by default (direct mode)

Without `SCANNER_ALLOW_ACTUATION` the actuating and destructive tools are not
registered, so a client sees a read-only server and cannot call them. Starting
a scan moves the instrument and fires its laser; make sure the instrument is
safely set up before enabling them.

### Launch checks (direct mode)

The scanner answers HTTP 202 even to a scan request it cannot parse, and then
runs a default full-dome scan. It also ignores `projectUUID` unless
`projectName` matches too. So `scanner_launch_scan` and
`scanner_launch_area_scan`, in this order:

1. **Validate the angles** (area scan): `delta_ha` in (0, 2π],
   `0 ≤ start_va`, `0 < delta_va`, `start_va + delta_va ≤ π`. `start_ha` is
   wrapped into [0, 2π).
2. **Refuse while busy**: if any task is running or queued, they fail with
   `instrument is busy: <type> <taskUUID> (<state>)…` and send nothing. Another
   client, or another agent, may be driving the instrument.
3. **Check the project**: `project_uuid` must parse as a UUID, and a project
   with that name **and** that UUID must exist.
4. **Check the mode**: `scan_mode` must be one of the names in
   `scanner_scan_modes`.
5. **Send, then compare the echo**: the instrument echoes the request it
   accepted. Every field sent (`scanMode`, `projectName`, `projectUUID`,
   `scanType`, `fastMode`, `colorMode`, and the four angles for an area scan)
   must come back unchanged (floats to within 1e-9). On any mismatch the
   server **cancels the task at once** and returns
   `instrument did not accept the request as sent (sent vs echoed: …);
   cancelled its task (HTTP <status>)`.

`scanner_delete_scan` and `scanner_delete_project` refuse in the same way
while any task is running or queued: a scan being filed renumbers the scan and
project lists that the delete verification relies on, and deleting the project
a scan is writing into is never wanted. `scanner_create_project` does not
check, since a new project does not touch the running task. `scanner_cancel_task`
is meant for a running task, so it does not check either.

### Delete by UUID, verified (direct mode)

Scan and project `id`s on the scanner are list positions and are renumbered
after every delete. The delete tools therefore take the stable `scanUUID` or
`projectUUID`, look up its current `id` immediately before deleting, and read
the list again afterwards. The delete is reported as done only if that UUID is
gone and the list is exactly one shorter; otherwise the tool fails with
`delete of <uuid> not confirmed; re-check <kind> before retrying`.

### Downloads stay in one directory (both modes)

Download tools write only inside `SCANNER_DOWNLOAD_DIR`:

- `filename` must be a plain name: it starts with a letter or digit, then up
  to 127 more letters, digits, `.`, `_` or `-`. No directories, no `..`, no
  leading dot.
- The resolved path must sit directly in the download directory, so a symlink
  that points elsewhere is refused.
- An existing file is kept unless `overwrite=true`.
- Data streams into `<name>.part` and is renamed into place only when complete;
  the `.part` file is removed on failure.

`scanner_preview_to_angles` reads files only from the same directory, by plain
name.

### Secrets masked (direct mode)

The scanner returns its Wi-Fi passphrase in plain text in its status.
`scanner_status` replaces every non-empty string value whose key ends in
`key`, `psk`, `passphrase`, `password`, `secret` or `token` (any case) with
`***`, at any depth. Booleans such as `isFactoryKey` are kept. Only
`scanner_status` is masked. In bridge mode the bridge decides what its status
contains.

### Bridge mode

The bridge enforces approvals, battery limits, a busy scanner, a scanner that
moved since the reference scan, and request echo checks, for every client.
This server adds only input shape checks (for example, all four window angles
or none) and the download-directory rules above. Ids (`scan_id`, `job_id`,
`export_id`, `run_id`, `recipe_id`) are sent as single URL path segments with
every reserved character escaped, and an empty id, `.` or `..` is refused, so
an id cannot reach a different bridge endpoint. Whether projects can be
created while the scanner is busy is the bridge's decision; bridge mode has no
delete tools.

## Errors

Tool failures are raised as MCP tool errors (`ToolError`), so the message text
reaches the client on both mcp 1.x and mcp 2.x (mcp 2.x hides the text of
other exceptions). The messages are:

| Situation | Message |
|---|---|
| Scanner answered with an HTTP error (direct) | `HTTP <status> from <url>: <first 200 chars of body>` |
| Scanner unreachable (direct) | `instrument unreachable (<ErrorType>: <detail>)` |
| Bridge refused (bridge) | `<code>: <title> — <detail> (hint: <hint>)`, from the bridge's problem response, e.g. `battery_low`, `instrument_moved`, `invalid_request` |
| Bridge answered an error without a problem body (not JSON, or JSON without `code`/`title`) | `HTTP <status>: <first 200 chars of body>` |
| Scanner or bridge answered success with a body that is not JSON | `instrument sent a response that is not JSON (<detail>)` / `bridge sent a response that is not JSON (<detail>)` |
| Truncated or malformed `.tzf` or JPEG in `scanner_preview_to_angles` (direct) | `<name> is truncated or not a scan file` / `<name> is a truncated or malformed JPEG` |
| Any other bad value in a reply or file | `truncated or malformed data (<detail>)` or `invalid value (<ErrorType>: <detail>)` |
| Bridge unreachable (bridge) | `bridge unreachable (<ErrorType>: <detail>)` |
| Validation, busy, echo, delete and file-name checks | A plain sentence saying what was wrong and what to do, e.g. `unknown scan_mode 'X'; valid: …`, `<path> already exists; pass overwrite=true to replace it` |

## Client configuration

### On a field Pi, through the bridge (recommended)

The usual setup: the scanner talks to
[trimble-x9-bridge](https://github.com/northidaholabs/trimble-x9-bridge) on a
Raspberry Pi by the tripod (its `ops/README.md` builds the Pi), this server runs
on the same Pi, and the agent on the laptop starts it over ssh. The launcher
`extras/trimblex9-mcp-auto` (installed at `~/x9-bridge/bin/`) runs bridge mode
with the bridge's agent token from `~/x9-bridge/agent.token`, and passes the
person's token from `~/x9-bridge/operator.token` as `SCANNER_PERSON_TOKEN`, used
only after the person says yes in the chat. With an ssh alias `x9-bridge` for
the Pi (key login, no prompts):

```bash
claude mcp add -s user x9-bridge -- ssh -o BatchMode=yes x9-bridge 'X9_MCP_MODE=bridge ~/x9-bridge/bin/trimblex9-mcp-auto'
```

Quote the remote command, so `~` is the Pi's home, not the laptop's.

Check: the client lists the `scanner_*` tools, and `scanner_status` shows the
bridge and the scanner. `X9_MCP_MODE=direct` starts the same server in direct
mode for diagnostics; it stays read-only while the bridge runs.

### Claude Code

Direct mode, read-only:

```bash
claude mcp add trimblex9 -- trimblex9-mcp
```

Direct mode with actuation and custom paths:

```bash
claude mcp add trimblex9 \
  --env SCANNER_ALLOW_ACTUATION=1 \
  --env SCANNER_BASE=http://192.168.43.1:34567/api/v1 \
  --env SCANNER_DOWNLOAD_DIR=$HOME/scans \
  -- trimblex9-mcp
```

Without installing first:

```bash
claude mcp add trimblex9 -- uvx --from "git+https://github.com/northidaholabs/TrimbleX9-mcp@v0.15.0" trimblex9-mcp
```

Bridge mode:

```bash
claude mcp add trimblex9 \
  --env SCANNER_BRIDGE_URL=http://bridge.local:8795/v1 \
  --env SCANNER_BRIDGE_TOKEN=<agent token> \
  -- trimblex9-mcp
```

### Generic `mcpServers` JSON

For clients configured with an `mcpServers` object (Claude Desktop, many
others):

```json
{
  "mcpServers": {
    "trimblex9": {
      "command": "trimblex9-mcp",
      "env": {
        "SCANNER_BASE": "http://192.168.42.1:34567/api/v1",
        "SCANNER_DOWNLOAD_DIR": "/home/user/scanner-downloads"
      }
    }
  }
}
```

Add `"SCANNER_ALLOW_ACTUATION": "1"` to `env` to enable actuation. For bridge
mode, set `SCANNER_BRIDGE_URL` and `SCANNER_BRIDGE_TOKEN` instead. If
`trimblex9-mcp` is not on the client's `PATH`, give its full path (for a
virtualenv, `<venv>/bin/trimblex9-mcp`).

### On a remote host over SSH (e.g. a Raspberry Pi by the scanner)

The scanner is usually reached from a small computer on site, not from the
machine running the agent. Install the server on that host and let the client
start it over SSH; MCP's stdio transport passes through `ssh` unchanged. With
the bridge on that host, use the launcher as in
[On a field Pi, through the bridge](#on-a-field-pi-through-the-bridge-recommended).
Without a bridge (direct mode):

```bash
# on the agent's machine
claude mcp add trimblex9 -- ssh -T pi@scanner-pi.local \
  'SCANNER_DOWNLOAD_DIR=$HOME/scanner-downloads ~/x9-bridge/.venv/bin/trimblex9-mcp'
```

```json
{
  "mcpServers": {
    "trimblex9": {
      "command": "ssh",
      "args": ["-T", "pi@scanner-pi.local",
               "SCANNER_ALLOW_ACTUATION=1 ~/x9-bridge/.venv/bin/trimblex9-mcp"]
    }
  }
}
```

Notes:

- Set the environment variables **in the remote command**, as above. A
  client's `--env` / `"env"` applies to the local `ssh` process and is not
  forwarded to the remote host.
- Use key-based login with no password prompt (`ssh -T` and no TTY); the
  client cannot answer prompts.
- On current Raspberry Pi OS and Debian the system `pip` refuses to install
  into the system Python; use `pipx` or a virtualenv, and point the command
  at that `trimblex9-mcp`.
- Downloaded files land on the remote host, in its `SCANNER_DOWNLOAD_DIR`.
- Use the full path to `trimblex9-mcp` if the remote non-interactive shell's
  `PATH` does not include it.

## Compatibility

| Item | Supported |
|---|---|
| Python | 3.10 and later |
| `mcp` SDK | 1.x (`FastMCP`, 1.7 or later, see below) and 2.x (`MCPServer`); the import is chosen at run time |
| `httpx` | 0.27 and later |
| OS | Linux and macOS (anything that can reach the scanner's network link) |
| Transport | stdio |

CI runs the tests on Python 3.10 with mcp 1.7 (the floor; pinned with
`pydantic<2.14`, because mcp 1.7 imports a pydantic internal that 2.14
removed), on Python 3.10 with the newest mcp 1.x, and on Python 3.13 with
mcp 2.x. If you must stay on mcp 1.7, keep pydantic below 2.14. The tools use tool annotations, which `FastMCP` accepts from
mcp 1.7, so the requirement is `mcp>=1.7,<3`.

## Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/pytest -q
```

| Test file | Needs | Covers |
|---|---|---|
| `tests/test_offline.py` | nothing (starts in-process stubs of the scanner and bridge APIs) | Direct mode with actuation on: delete by UUID across renumbering, local launch validation, area-scan echo check, cancel on a mismatched echo, `scanner_preview_to_angles` including the seam wrap, launch and delete refused while busy, readable errors for truncated `.tzf`/JPEG files, non-JSON replies and an unreachable scanner, plain default download names, Wi-Fi key masking. Bridge helpers: ids escaped in URL paths, sanitised default download names, readable errors for non-JSON and non-problem replies |
| `tests/test_bridge_mode.py` | `X9_BRIDGE_BIN` | Bridge mode against a real bridge and scanner simulator: tool list has no approve/grant/delete tools, approval flow and progress, bridge error codes, an escaped id that would otherwise reach another endpoint, recipe prompt and run, field log |

Bridge-mode tests are skipped unless these are set:

| Variable | Effect |
|---|---|
| `X9_BRIDGE_BIN` | Directory holding the `x9-bridge` and `x9-mock` binaries (from the bridge project's `cargo build --release`). The tests start both on `127.0.0.1:34611` and `127.0.0.1:8811`. The recipe test also needs the bridge project's test recipe at `<bin>/../../crates/x9-bridge/tests/recipes/window-measurement.toml`, and is skipped without it. |
| `X9_FIXTURES` | Directory of dome/area `.tzf` and `.jpg` fixtures for the simulator. With it, the preview, aim and export test runs too. |

```bash
X9_BRIDGE_BIN=/path/to/bridge/target/release pytest -q tests/test_bridge_mode.py
```

## Development

```bash
.venv/bin/trimblex9-mcp        # run over stdio
python scanner_mcp.py          # same, from a checkout
```

`scanner_mcp.py` is direct mode and the entry point; `scanner_bridge.py` is
bridge mode and reuses the download-path helpers from `scanner_mcp.py`.

## License

MIT (see `LICENSE`).
