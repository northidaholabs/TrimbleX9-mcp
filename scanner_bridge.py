"""Bridge mode: the agent tools, served through an x9 bridge service.

The bridge is the only thing that talks to the scanner and it enforces the
rules (approvals, battery, busy scanner, request echo checks), so these tools
are thin: each one is a call to the bridge's HTTP API with this server's
token. With an `agent` token, scans wait for a person's approval unless a grant
covers them.

Config:
  SCANNER_BRIDGE_URL    Bridge API base, e.g. http://bridge.local:8795/v1
  SCANNER_BRIDGE_TOKEN  Bearer token issued by the bridge (usually role `agent`)
  SCANNER_DOWNLOAD_DIR  Where downloads are written (default ~/scanner-downloads)
  SCANNER_PERSON_TOKEN  Optional: an `operator` token for the person at the
                        scanner. Used only after they answer yes in the chat
                        (MCP elicitation, which the model cannot answer): to
                        give the visit's permission to scan and to sign off a
                        run. The model never sees it. Without it, or with a
                        client that can't ask, the person uses the bridge's
                        web UI instead.
  SCANNER_VISIT_HOURS   How long the visit's permission lasts (default 8)
"""
import asyncio
import json
import math
import os
import time
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, create_model

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import Context, Image
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP, Image
    from mcp.server.fastmcp.exceptions import ToolError

import scanner_mcp as direct  # download-path safety and error helpers

URL = os.environ.get("SCANNER_BRIDGE_URL", "").rstrip("/")
TOKEN = os.environ.get("SCANNER_BRIDGE_TOKEN", "")
PERSON_TOKEN = os.environ.get("SCANNER_PERSON_TOKEN", "")
VISIT_HOURS = float(os.environ.get("SCANNER_VISIT_HOURS", "8"))
VISIT_SCANS = 20
TIMEOUT = httpx.Timeout(30.0, connect=5.0)

mcp = FastMCP("trimblex9-bridge")


_tool_errors = direct.error_wrapper("bridge")


def tool(**kw):
    """mcp.tool, with HTTP and data failures reported as tool errors."""
    return lambda fn: mcp.tool(**kw)(_tool_errors(fn))


class BridgeError(ToolError):
    """A bridge refusal, carrying its machine-readable code and hint."""


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _seg(value) -> str:
    """An id as one URL path segment ('/', '?', '#' and the like escaped)."""
    s = str(value)
    if s in ("", ".", ".."):
        raise ToolError(f"invalid id {s!r}")
    return quote(s, safe="")


def _raise(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    try:
        p = r.json()
    except ValueError:
        p = None
    if isinstance(p, dict) and (p.get("code") or p.get("title")):
        msg = f"{p.get('code', r.status_code)}: {p.get('title', '')}"
        if p.get("detail"):
            msg += f" — {p['detail']}"
        if p.get("hint"):
            msg += f" (hint: {p['hint']})"
    else:
        msg = f"HTTP {r.status_code}: {r.text[:200]}"
    raise BridgeError(msg)


async def _call(method: str, path: str, body=None, params=None):
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.request(method, f"{URL}{path}", json=body, params=params, headers=_headers())
    _raise(r)
    return r.json() if r.content else None


async def _call_as_person(method: str, path: str, body=None):
    """A call with the person's token: only after they said yes in the chat."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.request(method, f"{URL}{path}", json=body, headers={"Authorization": f"Bearer {PERSON_TOKEN}"})
    _raise(r)
    return r.json() if r.content else None


def _can_ask(ctx) -> bool:
    """Whether the person can be asked in the chat: a person token is set and
    the client declared elicitation."""
    if not PERSON_TOKEN or ctx is None:
        return False
    try:
        from mcp.types import ClientCapabilities, ElicitationCapability
        return bool(ctx.session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability())))
    except Exception:
        return False


class _Allow(BaseModel):
    allow: bool = Field(True, description="Let the agent scan here")


def _sign_off_form(numeric: list, given: dict) -> type[BaseModel]:
    """The sign-off question: confirm, and each number prefilled with the
    proposal. MCP forms take a default only as a real value of the field's
    type, never null, so a number the person keeps is simply left as it is."""
    fields = {n: (float, Field(given[n], description=f"{n.replace('_', ' ')} (change it if the proposal is wrong)"))
              for n in numeric}
    return create_model("SignOff", confirm=(bool, Field(True, description="Confirm as proposed")), **fields)


async def _download(path: str, dest: str) -> int:
    tmp = dest + ".part"
    total = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0)) as c:
            async with c.stream("GET", f"{URL}{path}", headers=_headers()) as r:
                if r.status_code >= 400:
                    await r.aread()
                    _raise(r)
                with open(tmp, "wb") as fh:
                    async for chunk in r.aiter_bytes():
                        total += len(chunk)
                        fh.write(chunk)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return total


READ = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}

# ------------------------------------------------------------------ status ---


@tool(annotations=READ)
async def scanner_status() -> dict:
    """Bridge and scanner state: connection, busy, battery, storage, leveling,
    whether the scanner moved since its last scan, and the queue length."""
    return await _call("GET", "/status")


@tool(annotations=READ)
async def scanner_scan_modes() -> list:
    """The scan modes: name, range (m), angular step (rad), full-dome point
    count, fixed overhead (s) and full-dome time (s). Use the names in scans."""
    return await _call("GET", "/scan-modes")


@tool(annotations=READ)
async def scanner_projects() -> list:
    """Projects on the scanner (project_id, name, scan_count)."""
    return await _call("GET", "/projects")


@tool(annotations=WRITE)
async def scanner_create_project(name: str) -> dict:
    """Create a project (1-64 characters, no '/'). Returns its project_id."""
    return await _call("POST", "/projects", {"name": name})


# ------------------------------------------------------------------- scans ---


@tool(annotations=READ)
async def scanner_scans(project_id: str | None = None) -> list:
    """Scans, newest first: scan_id, kind (full_dome or area), station, mode,
    point count, parent station scan for area scans, and timings when the
    bridge ran them."""
    return await _call("GET", "/scans", params={"project_id": project_id} if project_id else None)


@tool(annotations=READ)
async def scanner_scan(scan_id: str) -> dict:
    """One scan's record."""
    return await _call("GET", f"/scans/{_seg(scan_id)}")


async def _ensure_permission(project_id: str, person_said: str, ctx, tool: str) -> str:
    """Make sure agents may scan in the project for this visit: a grant, or
    ask the person once (in the chat, else relayed). Returns how."""
    now = time.time()
    covered = any(g.get("project_id") == project_id and not g.get("run_id")
                  and (g.get("max_scans") is None or g.get("scans_used", 0) < g["max_scans"])
                  for g in await _call("GET", "/grants"))
    permission = "already given"
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + VISIT_HOURS * 3600))
    visit = {"project_id": project_id, "expires_at": expires, "max_scans": VISIT_SCANS}
    name = project_id
    if not covered:
        name = next((p["name"] for p in await _call("GET", "/projects") if p.get("project_id") == project_id), project_id)
    ask_yourself = (f"Ask the person in the chat: \"May I scan in {name} for the next {VISIT_HOURS:g} hours (up to "
                    f"{VISIT_SCANS} scans)?\" If they agree, call " f"{tool} again with person_said = their words.")
    if not covered and person_said.strip() and PERSON_TOKEN:
        await _call_as_person("POST", "/grants", {**visit, "note": f"relayed by the agent: \u201c{person_said.strip()[:300]}\u201d"})
        permission = "relayed from the chat"
    elif not covered:
        if _can_ask(ctx):
            res = await ctx.elicit(message=f"Let the agent scan in \"{name}\" for the next {VISIT_HOURS:g} hours "
                                           f"(up to {VISIT_SCANS} scans)? Nothing else is asked before the result.",
                                   schema=_Allow)
            if res.action == "accept" and res.data.allow:
                await _call_as_person("POST", "/grants", {**visit, "note": "asked in the chat"})
                permission = "given in the chat"
            elif res.action == "accept":
                raise ToolError("The person did not allow scanning here; nothing was started.")
            else:
                raise ToolError(f"No answer from the chat's question (action: {res.action}); some chat apps close it "
                                f"without showing it. Nothing was started. If the person didn't see a question: {ask_yourself}")
        elif PERSON_TOKEN:
            raise ToolError(f"This chat can't ask the person directly. Nothing was started. {ask_yourself}")
        else:
            permission = ("none: no person token is set, so each run's first scan waits for approval "
                          "in the bridge web UI (Approve the run), or they let agents scan from its Scans tab")
    return permission


@tool(annotations=WRITE)
async def scanner_start_scan(
    project_id: str, mode: str, color: bool = False,
    start_ha: float | None = None, delta_ha: float | None = None,
    start_va: float | None = None, delta_va: float | None = None,
    note: str | None = None, person_said: str = "", ctx: Context = None,
) -> dict:
    """Queue a scan. If the project has no permission to scan yet, this asks the
    person once, in the chat, to allow scanning for the visit (as
    scanner_start_run does; `person_said` as there). Without a window it is a full dome; give all four window
    angles (radians, VA from the zenith; get them from scanner_aim) for an area
    scan. Color adds ~150 s. The bridge checks the rules; with an agent token
    the job usually waits in `awaiting_approval` until a person approves it in
    the bridge app, unless a grant covers it. Returns the job: follow it with
    scanner_wait_for_job."""
    window = [start_ha, delta_ha, start_va, delta_va]
    if any(v is not None for v in window) and not all(v is not None for v in window):
        raise ToolError("give all four window angles for an area scan, or none for a full dome")
    body = {"project_id": project_id, "kind": "area" if window[0] is not None else "full_dome",
            "mode": mode, "color": color}
    if window[0] is not None:
        body["window"] = {"start_ha": start_ha % math.tau, "delta_ha": delta_ha,
                          "start_va": start_va, "delta_va": delta_va}
    if note:
        body["note"] = note
    # Ask only for a request the scanner can run; let the bridge reject the rest.
    known = mode in {m.get("mode") for m in await _call("GET", "/scan-modes")}
    permission = await _ensure_permission(project_id, person_said, ctx, "scanner_start_scan") if known else "not asked"
    job = await _call("POST", "/scans", body)
    job["permission"] = permission
    return job


# -------------------------------------------------------------------- jobs ---


@tool(annotations=READ)
async def scanner_job(job_id: str) -> dict:
    """One job: state (awaiting_approval, queued, running, succeeded, failed,
    cancelled), phase, progress 0-1, result and any problem."""
    return await _call("GET", f"/jobs/{_seg(job_id)}")


@tool(annotations=READ)
async def scanner_wait_for_job(ctx: Context, job_id: str, timeout_s: int = 900,
                               return_on_approval: bool = True) -> dict:
    """Wait for a job to finish, reporting progress when the client supports it.
    If the job is waiting for a person's approval and `return_on_approval` is
    true, returns at once so you can ask them; call again afterwards.
    Returns {job, done, timed_out, waiting_for_approval, elapsed_s}."""
    timeout_s = max(1, min(int(timeout_s), 7200))
    start = time.monotonic()
    while True:
        job = await _call("GET", f"/jobs/{_seg(job_id)}")
        elapsed = round(time.monotonic() - start, 1)
        state = job.get("state")
        if state in ("succeeded", "failed", "cancelled"):
            return {"job": job, "done": True, "timed_out": False, "waiting_for_approval": False, "elapsed_s": elapsed}
        if state == "awaiting_approval" and return_on_approval:
            return {"job": job, "done": False, "timed_out": False, "waiting_for_approval": True, "elapsed_s": elapsed,
                    "note": "Ask the operator to approve this job in the bridge app, then wait again."}
        if job.get("progress") is not None:
            try:
                await ctx.report_progress(round(job["progress"] * 100, 1), 100)
            except Exception:
                pass
        if elapsed >= timeout_s:
            return {"job": job, "done": False, "timed_out": True, "waiting_for_approval": state == "awaiting_approval",
                    "elapsed_s": elapsed}
        await asyncio.sleep(1.0)


@tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
async def scanner_cancel_job(job_id: str) -> dict:
    """Cancel a job of yours. A running scan is stopped; the scanner still files
    the partial sweep, which the cancelled job names as partial_scan_id (bridge
    0.1.1+). Keep it or delete it."""
    return await _call("POST", f"/jobs/{_seg(job_id)}/cancel")


# ------------------------------------------------------------------ aiming ---


@tool(annotations=READ)
async def scanner_preview(scan_id: str) -> list:
    """Look at a scan's preview: a grey laser-intensity panorama (full dome)
    or close-up (area scan). Returns the image and its size in pixels; boxes
    for scanner_aim are given in these pixel coordinates (y down)."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0)) as c:
        r = await c.get(f"{URL}/scans/{_seg(scan_id)}/preview", headers=_headers())
    _raise(r)
    w, h = _jpeg_dims(r.content)
    return [f"Preview of scan {scan_id}: {w} x {h} px. Give scanner_aim boxes in these pixels (y down; "
            f"x1 < x0 wraps across the panorama seam).", Image(data=r.content, format="jpeg")]


def _jpeg_dims(data: bytes) -> tuple[int, int]:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            break
        marker, seglen = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + seglen
    return 0, 0


@tool(annotations=READ)
async def scanner_aim(reference_scan_id: str, x0: int, y0: int, x1: int, y1: int, margin_deg: float = 0.5) -> dict:
    """Turn a box drawn on a scan's preview (see scanner_preview) into an
    area-scan window. The reference scan must be from where the scanner stands
    now (the bridge refuses if it moved). Pass the returned window's four
    angles to scanner_start_scan."""
    return await _call("POST", "/aim", {"reference_scan_id": reference_scan_id,
                                        "box": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
                                        "margin_rad": math.radians(max(0.0, margin_deg))})


# ----------------------------------------------------------------- exports ---


@tool(annotations=WRITE)
async def scanner_export(scan_id: str, format: str = "ply", intensity: str = "auto") -> dict:
    """Decode a scan to levelled points (format `ply`, `las`, or `copc` for a
    Cloud Optimized Point Cloud, bridge 0.6.0+; intensity `auto`, `preview` or
    `none`). Returns a job; when it succeeds its result has an export_id for
    scanner_export_info and scanner_download_export."""
    return await _call("POST", f"/scans/{_seg(scan_id)}/exports", {"format": format, "intensity": intensity})


@tool(annotations=READ)
async def scanner_export_info(export_id: str) -> dict:
    """An export's point count, bounds and automatic checks (point count,
    levelled, coordinates, intensity source). Mention any failed check."""
    return await _call("GET", f"/exports/{_seg(export_id)}")


@tool(annotations=WRITE)
async def scanner_measure(scan_id: str, kind: str = "opening") -> dict:
    """Measure a feature in a scan on the bridge (bridge 0.4.0+). `opening`:
    a window's finished opening (the hole in the wall face) from a levelled
    close-up with wall on every side. Returns a job; follow it with
    scanner_wait_for_job, then read the result with scanner_measurement."""
    return await _call("POST", f"/scans/{_seg(scan_id)}/measurements", {"kind": kind})


@tool(annotations=READ)
async def scanner_measurement(measurement_id: str) -> list:
    """A measurement's values (metres, with uncertainties), checks, and its
    evidence image: the wall face (grey), the opening (green) and the
    measuring row and column (red). Look at the image before reporting: the
    green area should fill the window up to the wall. Mention failed checks."""
    m = await _call("GET", f"/measurements/{_seg(measurement_id)}")
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{URL}/measurements/{_seg(measurement_id)}/evidence", headers=_headers())
    if r.status_code >= 400:
        return [json.dumps(m, indent=2)]
    return [json.dumps(m, indent=2), Image(data=r.content, format="png")]


@tool(annotations=READ)
async def scanner_plan_station(scan_id: str, subject_x0: int | None = None, subject_x1: int | None = None) -> dict:
    """Where to set up next, computed from this full dome's points (bridge
    0.14.0+): up to three spots, best first, each on open ground a person can
    walk to, a real step out (`min_step_m` scales with the place: metres in a
    room, tens on a campus), seeing the most ground this station couldn't
    while still sharing enough surface with it to be registered. Each has
    `preview_px` (the ground there, on this station's preview), `distance_m`,
    `walk_m`, `sees_hidden_m2`, and a `landmark` beside it with its own
    `preview_px`, `height_m`, `footprint_m2` and `shape`: "post" (under 1 m²:
    a pole, a lamp, a sign, a trunk; name the post itself, not the tree or
    wall behind it) or "mass" (a wall, a building, a hedge).

    Pass the subject as preview columns (`subject_x0`, `subject_x1`; x1 < x0
    when it crosses the panorama's seam) once you know what the job is about,
    so only ground behind and beside it counts. Without one, the plan follows
    the biggest hidden ground, which may be a lawn behind trees.

    Use the first spot unless the person or the mission says otherwise:
    scanner_propose_station with the marker at its `preview_px` and the
    landmark label at the landmark's `preview_px`, named from what you see
    there in the preview. No spots comes with a `reason` (nothing left hidden,
    not a full dome, no open ground). The first call on a scan decodes it
    (seconds)."""
    params = None
    if subject_x0 is not None and subject_x1 is not None:
        params = {"subject": f"{subject_x0},{subject_x1}"}
    return await _call("GET", f"/scans/{_seg(scan_id)}/next-station/plan", params=params)


@tool(annotations=WRITE)
async def scanner_propose_station(scan_id: str, note: str, marker_x: int, marker_y: int, marker_label: str = "",
                                  labels: list | None = None, boxes: list | None = None) -> list:
    """Show the person where to set up next (bridge 0.8.0+): the bridge draws a
    "SET UP HERE" tripod on this station's preview at (marker_x, marker_y),
    with your labels and boxes, and shows it in its web UI with your `note`.
    Returns the picture; show it in the chat too.

    The person has to match one glance to the ground in front of them, so the
    proposal is the tripod, the one thing they walk past to reach it, and a
    sentence naming that thing. The subject is whatever the mission is about:
    a building, a window, an object, a site. Before choosing, if this view
    shows more than one thing the job could mean (two buildings, two windows,
    two objects), ask the person which one; don't pick for them.

    Coordinates are preview pixels (scanner_preview gives the size). Start
    from scanner_plan_station: its first spot's `preview_px` is the marker and
    its landmark's `preview_px` the landmark label. Move off it only for a
    reason the plan can't see (the job needs a window square-on, the person
    said the lawn is out of bounds), and keep the tripod on ground where the
    open part of the subject comes into view and in sight of something this
    station saw so it can be registered.
    `marker_label` goes under "SET UP HERE": where on the ground ("on the
    pavement, ~5 m past the lamp post"), or empty; not a name for the spot.
    `labels`: [{x, y, text, kind}], at least one "landmark" (white: the thing
    to walk past, placed on it) and optionally "seen" (green) or "hidden" (red:
    what the next station is for). `boxes`: [{x0, y0, x1, y1, text, kind}],
    the same kinds. At most 3 labels and boxes in all, texts up to 80
    characters (a landmark's up to 30: "lamp post", not where it stands),
    each within a quarter of the preview's width of the marker.
    `note`: one or two sentences that name the landmark as labelled ("carry the
    tripod past the shed door, onto the path, about 3 m beyond it"). No left,
    right, clockwise or compass words: they change as the person turns, and
    the scan has no north. The bridge refuses a proposal that breaks these.

    Then look at the returned picture: the landmark's dot on the landmark, the
    tripod on walkable ground beside it, the note matching both. If not,
    propose again. A newer proposal for the scan replaces this one."""
    body = {"marker": {"x": marker_x, "y": marker_y, "label": marker_label}, "note": note,
            "labels": labels or [], "boxes": boxes or []}
    v = await _call("PUT", f"/scans/{_seg(scan_id)}/next-station", body)
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{URL}/scans/{_seg(scan_id)}/next-station/image", headers=_headers())
    if r.status_code >= 400:
        return [json.dumps(v, indent=2)]
    return [json.dumps(v, indent=2), Image(data=r.content, format="png")]


# ---------------------------------------------------------------- missions ---


@tool(annotations=WRITE)
async def scanner_set_mission(project_id: str, goal: str, kind: str, done_when: str, targets: list | None = None) -> dict:
    """Set what the job is for (bridge 0.8.0+), from the person's sentence:
    `goal` (their words), `kind` (exterior, windows, room or custom) and
    `done_when`: your criteria in prose, concrete enough to check ("every
    facade seen at <= 12 mm spacing with no shadow on the house's walls").
    Then keep a ledger of targets with scanner_update_targets: each facade,
    each window, … The mission frames your station advice (go after what is
    still open) and ends with scanner_ready_to_leave. Replaces any earlier
    mission on the project. Once stations are registered (scanner_registration),
    give facade targets their wall (`wall_id`) so the bridge judges them."""
    return await _call("PUT", f"/projects/{_seg(project_id)}/mission",
                       {"goal": goal, "kind": kind, "done_when": done_when, "targets": targets or []})


@tool(annotations=READ)
async def scanner_mission(project_id: str) -> dict:
    """The project's mission: goal, done_when, state (active, ready,
    signed_off) and the ledger of targets with their status and stations."""
    return await _call("GET", f"/projects/{_seg(project_id)}/mission")


@tool(annotations=WRITE)
async def scanner_update_targets(project_id: str, targets: list) -> dict:
    """Add or update ledger targets: [{target_id, name, kind, status, stations,
    measurement_id, note, wall_id, face}], by target_id (letters, digits, - or
    _; fields left out keep their value). status: open, covered (name the scan
    ids in `stations`), measured (also `measurement_id`) or declined (say why
    in `note`). Add targets as soon as you know they exist (the far facade you
    haven't seen yet is `open`), so the ledger shows what is left.

    A facade or wall target should name its wall (bridge 0.9.0+): `wall_id`
    from scanner_registration's walls, or `face`: {from: [x, y], to: [x, y]}
    in the project frame (metres) for a stretch no wall covers yet. The bridge
    then judges it from the registered stations and keeps its `geometry`
    (share of its length seen, spacing, stations, unseen stretches): it marks
    it covered itself once 80% is seen, re-judges it after every station, and
    refuses `covered` while less is seen. Don't mark wall targets covered by
    eye; leave them open and let the geometry decide."""
    return await _call("POST", f"/projects/{_seg(project_id)}/mission/targets", {"targets": targets})


# ------------------------------------------------------------ registration ---


@tool(annotations=READ)
async def scanner_registration(project_id: str) -> list:
    """The project's stations in one frame (bridge 0.9.0+). The bridge
    registers every full dome by itself a few seconds to a minute after it
    lands: `summary` says how many are placed and how well the pairs agree,
    each station has `placed`, `turn_deg` and `xyz_m`, and `walls` are the
    straight walls found in the merged plan ({wall_id, from, to, length_m,
    z_m, seen_by: station numbers}). Returns the plan picture too: walls with
    their ids, stations numbered with a tick for the way each faced, the
    mission's wall targets (green covered, red open, grey declined), on a 5 m
    grid. Read it to learn the building's outline and to choose the next
    station: an open wall target's unseen stretch is what it's for.

    The frame is the first station's, not north: describe places to the
    person by landmarks and station numbers ("past station 3, along the long
    wall"), never by coordinates or compass words. `registering_job_id` is set
    while a registration runs.

    Trust the verdicts, not the picture: each station is `confirmed` (two or
    more ties to trusted stations, and fresh fits, free space and the ground
    agree), `single` (one such tie), `frame`, `doubtful` (a check finds it
    off against most of its neighbours, or it is tied only through stations
    in doubt), `grouped` (in a separate group, or in a block joined to the
    rest by one tie that nothing else backs up: the next station should see
    walls of both), `ambiguous`
    (symmetric or repeated walls: it fits in more than one place) or
    `unplaced`; `reasons` say why. Walls carry a `kind`: building, low wall
    or vegetation (hedges and trees against walls); give facade targets
    building walls. Only confirmed, single
    and frame stations count towards wall coverage. Plan the next station to
    settle what isn't trusted: one that sees walls an earlier station saw
    (unplaced), something that breaks the symmetry such as a corner, door or
    tree (ambiguous), or the same area again from another spot (doubtful,
    single). `rejected_pairs` lists fits that were not used, and why."""
    v = await _call("GET", f"/projects/{_seg(project_id)}/registration")
    if not any(st.get("placed") for st in v.get("stations", [])):
        return [json.dumps(v, indent=2)]
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{URL}/projects/{_seg(project_id)}/registration/plan", headers=_headers())
    if r.status_code >= 400:
        return [json.dumps(v, indent=2)]
    return [json.dumps(v, indent=2), Image(data=r.content, format="png")]


@tool(annotations=READ)
async def scanner_minimap(project_id: str) -> list:
    """The job so far as a simple line map (bridge 0.11.0+): walls as lines,
    stations as numbered dots with a tick for the way each faced (amber with
    "?" while not trusted), and the mission's wall targets in their status
    colours. Stations scanned but not yet placed are listed underneath. The
    bridge redraws it after every station; show it to the person after each
    one, with a line on what changed. Its frame is the first station's, not
    north: talk about places by landmarks and station numbers."""
    reg = await _call("GET", f"/projects/{_seg(project_id)}/registration")
    lines = [reg.get("summary", "")]
    for st in reg.get("stations", []):
        if st.get("verdict") not in ("confirmed", "frame"):
            lines.append(f"Station {st['station']}: {st.get('verdict') or 'not placed'}"
                         + (f" ({st['reasons'][0]})" if st.get("reasons") else ""))
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{URL}/projects/{_seg(project_id)}/minimap", headers=_headers())
    if r.status_code >= 400:
        return ["\n".join(lines) + f"\n(no map: {r.status_code})"]
    return ["\n".join(lines), Image(data=r.content, format="png")]


@tool(annotations=READ)
async def scanner_processing() -> dict:
    """Where the heavy work (registering stations) runs (bridge 0.10.0+).
    `workers` lists computers doing it for the bridge; with none, the Pi does
    it: about 40 s to a minute and a half per new station. A laptop is
    several times faster.

    Ask the person once per laptop, the first time you set up on it, and
    remember the answer (don't ask again each job): "Process the scans on
    this laptop? It's faster, and uses some of its battery. Otherwise the
    Pi does it." If they say yes and no worker is listed, start one on the
    laptop yourself, in the background, as `start_worker` describes. If
    they say no, leave it to the Pi. Either way, registration still
    finishes: the bridge falls back to the Pi when a worker goes away."""
    workers = await _call("GET", "/workers")
    base = URL.rsplit("/v1", 1)[0] if URL else "http://10.55.0.1:8795"
    return {
        "workers": workers,
        "runs_on": ", ".join(w["worker"] for w in workers) if workers else "the Pi",
        "start_worker": {
            "binary": "x9-bridge from the trimble-x9-bridge release for this laptop "
                      "(trimble-x9-bridge-<version>-aarch64-apple-darwin.tar.gz for Apple silicon, x86_64-apple-darwin for Intel Macs)",
            "token": "~/.config/x9-worker/token holding an agent token for the bridge; on the field setup copy the Pi's without showing it: "
                     "ssh x9-bridge 'cat ~/x9-bridge/agent.token' > ~/.config/x9-worker/token",
            "command": f"x9-bridge worker --bridge {base}",
            "note": "Run it in the background for the visit; it reconnects by itself and keeps decoded stations in ~/.cache/x9-worker.",
        },
    }


@tool(annotations=READ)
async def scanner_wall_face(project_id: str, wall_id: str = "", from_xy: list | None = None, to_xy: list | None = None) -> dict:
    """How much of a wall face the registered stations saw, without changing
    anything (bridge 0.9.0+): a `wall_id` from scanner_registration, or a
    stretch `from_xy`/`to_xy` ([x, y], project frame, metres). Returns
    seen_share (of its length; 0.8 counts as covered), spacing_mm, height_m,
    the stations that saw it and the `unseen` stretches. Use it to check a
    facade before planning the next station."""
    body = {"wall_id": wall_id} if wall_id else {"from": from_xy, "to": to_xy}
    return await _call("POST", f"/projects/{_seg(project_id)}/registration/face", body)


class _SignOffJob(BaseModel):
    confirm: bool = Field(True, description="Sign the job off: nothing more will be added")


@tool(annotations=WRITE)
async def scanner_ready_to_leave(project_id: str, summary: str, person_said: str = "", ctx: Context = None) -> dict:
    """Say the job is done and ask the person to sign it off: the bridge
    refuses while any ledger target is open (and says which). `summary`: what
    was covered or measured and what was declined and why, in two or three
    lines. The person is asked in the chat; if the chat can't ask, ask them
    yourself ("Ready to leave: … Sign it off?") and call again with
    `person_said` (their exact words, only if they agreed). This sign-off is
    the job's last person intervention."""
    m = await _call("GET", f"/projects/{_seg(project_id)}/mission")
    if m.get("state") != "ready":
        m = await _call("POST", f"/projects/{_seg(project_id)}/mission/ready", {"summary": summary})
    tally = ", ".join(f"{n} {s}" for s in ("covered", "measured", "declined")
                      if (n := sum(1 for t in m["targets"] if t["status"] == s)))
    question = f"Ready to leave: {m['goal']} ({tally}). {summary.strip()} Sign the job off?"
    if person_said.strip() and PERSON_TOKEN:
        done = await _call_as_person("POST", f"/projects/{_seg(project_id)}/mission/sign-off",
                                     {"confirmed_via": f"relayed by the agent: \u201c{person_said.strip()[:300]}\u201d"})
        return {"signed_off": True, "relayed": True, "mission": done}
    ask_yourself = f"Ask the person in the chat: \"{question}\" If they agree, call scanner_ready_to_leave again with person_said = their words."
    if not _can_ask(ctx):
        if PERSON_TOKEN:
            return {"signed_off": False, "asked": False, "ready": True, "message": f"This chat can't ask the person directly. {ask_yourself}"}
        return {"signed_off": False, "asked": False, "ready": True,
                "message": "The job is ready to leave; the person signs it off in the bridge web UI (Now, Mission)."}
    res = await ctx.elicit(message=question, schema=_SignOffJob)
    if res.action != "accept":
        return {"signed_off": False, "asked": True, "action": res.action, "ready": True,
                "message": f"No answer from the chat's question; some chat apps close it without showing it. If the person didn't see it: {ask_yourself}"}
    if not res.data.confirm:
        return {"signed_off": False, "asked": True, "ready": True, "message": "The person didn't sign it off yet; ask what is missing."}
    done = await _call_as_person("POST", f"/projects/{_seg(project_id)}/mission/sign-off", {"confirmed_via": "asked in the chat"})
    return {"signed_off": True, "mission": done}


@tool(annotations=READ)
async def scanner_coverage(scan_id: str) -> list:
    """What a scan covered (bridge 0.6.0+): point spacing at full resolution
    (median, 90th percentile, and the share that is fine ≤ 6 mm, good ≤ 12,
    coarse ≤ 25, sparse beyond) and a plan from above in 10 cm cells: green
    seen, red shadow (floor nearer than the farthest return in that direction
    with no points at any height: something blocked it; full domes only, as
    `shadows_assessed` says), dark nothing; a white cross marks the scanner.
    Use it to decide whether a station saw enough or where the next station
    should go (then scanner_propose_station marks the spot on the preview);
    a shadow behind furniture or a door is what another station fills. The plans up and right are the scanners own axes (wherever its
    zero angle pointed when set up, different at every station), not north or
    east: never give the person compass words. Direct them by landmarks they
    can see on site, from the scan and its preview ("about 4 m past the shed
    door, level with the deck corner"). The first call on a scan decodes it
    (seconds)."""
    cov = await _call("GET", f"/scans/{_seg(scan_id)}/coverage")
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{URL}/scans/{_seg(scan_id)}/coverage/image", headers=_headers())
    if r.status_code >= 400:
        return [json.dumps(cov, indent=2)]
    return [json.dumps(cov, indent=2), Image(data=r.content, format="png")]


@tool(annotations=WRITE)
async def scanner_download_export(export_id: str, filename: str | None = None, overwrite: bool = False) -> dict:
    """Download an export into SCANNER_DOWNLOAD_DIR (plain file name; no paths)."""
    info = await _call("GET", f"/exports/{_seg(export_id)}")
    fmt = info.get("format") or "ply"
    default = direct._default_name(export_id, "laz" if fmt == "copc" else fmt)
    if fmt == "copc":
        default = default[: -len(".laz")] + ".copc.laz"
    dest = direct._dest(filename, default, overwrite)
    n = await _download(f"/exports/{_seg(export_id)}/file", dest)
    return {"export_id": export_id, "bytes": n, "path": dest}


@tool(annotations=WRITE)
async def scanner_download_scanfile(scan_id: str, filename: str | None = None, overwrite: bool = False) -> dict:
    """Download a scan's raw file (.tzf) into SCANNER_DOWNLOAD_DIR."""
    dest = direct._dest(filename, direct._default_name(scan_id, "tzf"), overwrite)
    n = await _download(f"/scans/{_seg(scan_id)}/tzf", dest)
    return {"scan_id": scan_id, "bytes": n, "path": dest}


# ----------------------------------------------------------------- recipes ---


@tool(annotations=READ)
async def scanner_recipes() -> list:
    """Installed recipes (recipe_id, title, version, summary)."""
    return await _call("GET", "/recipes")


@tool(annotations=READ)
async def scanner_recipe(recipe_id: str) -> dict:
    """A recipe's inputs and steps (action, person and check steps)."""
    return await _call("GET", f"/recipes/{_seg(recipe_id)}")


@tool(annotations=WRITE)
async def scanner_start_run(recipe_id: str, project_id: str, inputs: dict | None = None,
                            person_said: str = "", ctx: Context = None) -> dict:
    """Start running a recipe in a project. If the project has no permission
    to scan yet, this asks the person once, in the chat, to allow scanning for
    the visit; a refusal starts nothing. If the chat can't ask them, it says
    so: ask the person yourself ("May I scan in <project> for the next N hours?")
    and call again with `person_said`. `person_said`: only when the chat couldn't ask the person and they agreed
    in this conversation: their own words, quoted exactly. The bridge records
    that their answer was relayed by you. Never fill it otherwise. The bridge then executes the steps; it
    pauses with `waiting_for: input` at a person step (answer `review = final`
    steps yourself with scanner_advance_run; for the sign-off use
    scanner_sign_off) or `waiting_for: approval` when no permission covers a
    scan. Follow it with scanner_run."""
    permission = await _ensure_permission(project_id, person_said, ctx, "scanner_start_run")
    run = await _call("POST", "/runs", {"recipe_id": recipe_id, "project_id": project_id, "inputs": inputs or {}})
    run["permission"] = permission
    return run


@tool(annotations=WRITE)
async def scanner_sign_off(run_id: str, summary: str = "", person_said: str = "",
                           corrections: dict | None = None, ctx: Context = None) -> dict:
    """Ask the person, in the chat, to sign off the step the run waits at (the
    measurement): they confirm it or type corrected numbers. Show them the
    evidence first (scanner_measurement's image, and the box you chose) and put
    one line of what you checked in `summary`. This is the run's one person
    intervention; never answer it yourself. If the chat can't ask, the result
    says so: then ask them yourself ("Is the opening W × H mm right?") and call
    again with `person_said`, and `corrections` ({field: number}) if they gave
    different numbers. `person_said`: only when the chat couldn't ask the person and they agreed
    in this conversation: their own words, quoted exactly. The bridge records
    that their answer was relayed by you. Never fill it otherwise."""
    r = await _call("GET", f"/runs/{_seg(run_id)}")
    if r.get("waiting_for") != "input":
        raise ToolError(f"run {run_id} is not waiting for a person (state {r.get('state')}, waiting for {r.get('waiting_for')})")
    step = next(s for s in r["steps"] if s["step_id"] == r["current_step_id"])
    rec = await _call("GET", f"/recipes/{_seg(r['recipe_id'])}")
    d = next((s for s in rec["steps"] if s["step_id"] == step["step_id"]), {})
    if d.get("review") == "final":
        raise ToolError(f"step {step['step_id']!r} is yours: answer it with scanner_advance_run")
    prop = step.get("proposal")
    if not prop:
        raise ToolError("nothing to sign off yet: propose an answer first with scanner_propose")
    given = dict(prop["input"])
    numeric = [n for n in d.get("needs", []) if isinstance(given.get(n), (int, float)) and not isinstance(given.get(n), bool)]
    shown = ", ".join(f"{n.replace('_', ' ')} {given[n]}" for n in numeric) or json.dumps(given)
    ask_yourself = (f"Show the person the result and ask in the chat whether it is right ({shown}); then call "
                    "scanner_sign_off again with person_said = their words, and corrections if they gave other numbers.")
    if person_said.strip() and PERSON_TOKEN:
        bad = [k for k in (corrections or {}) if k not in numeric]
        if bad:
            raise ToolError(f"corrections can only change {', '.join(numeric)}; not {', '.join(bad)}")
        corrected = {k: v for k, v in (corrections or {}).items() if v != given[k]}
        given.update(corrected)
        given["confirmed_via"] = f"relayed by the agent: \u201c{person_said.strip()[:300]}\u201d"
        run = await _call_as_person("POST", f"/runs/{_seg(run_id)}/advance", {"step_id": step["step_id"], "input": given})
        return {"signed_off": True, "relayed": True, "corrected": corrected, "run": run}
    if not _can_ask(ctx):
        if PERSON_TOKEN:
            return {"signed_off": False, "asked": False, "message": f"This chat can't ask the person directly. {ask_yourself}"}
        return {"signed_off": False, "asked": False,
                "message": f"This chat can't ask the person directly. Ask them to confirm in the bridge web UI, run {run_id}."}
    schema = _sign_off_form(numeric, given)
    msg = f"{d.get('title') or step['step_id']}: {shown}." + (f" {prop['note']}." if prop.get("note") else "") + (f" {summary}" if summary else "")
    res = await ctx.elicit(message=msg, schema=schema)
    if res.action != "accept":
        return {"signed_off": False, "asked": True, "action": res.action,
                "message": f"No answer from the chat's question; some chat apps close it without showing it. If the person didn't see it: {ask_yourself}"}
    corrected = {}
    for n in numeric:
        v = getattr(res.data, n)
        if v != given[n]:
            corrected[n] = int(v) if float(v).is_integer() else v
    if not res.data.confirm and not corrected:
        return {"signed_off": False, "asked": True, "message": "The person did not confirm and gave no correction."}
    given.update(corrected)
    given["confirmed_via"] = "asked in the chat"
    run = await _call_as_person("POST", f"/runs/{_seg(run_id)}/advance", {"step_id": step["step_id"], "input": given})
    return {"signed_off": True, "corrected": corrected, "run": run}


@tool(annotations=READ)
async def scanner_permissions(project_id: str | None = None) -> list:
    """Standing permissions to scan (bridge 0.7.0+ records them on runs): each
    grant's project, expiry, scans used of max_scans, who gave it, and its
    run_id when it covers one run only. With a grant for the project, a run's
    scans need no approval; check before starting a recipe run."""
    gs = await _call("GET", "/grants")
    return [g for g in gs if project_id is None or g.get("project_id") == project_id]


@tool(annotations=READ)
async def scanner_run(run_id: str) -> dict:
    """A run: state, what it is waiting for, the current step, and each step's
    state, job and output."""
    return await _call("GET", f"/runs/{_seg(run_id)}")


@tool(annotations=WRITE)
async def scanner_advance_run(run_id: str, step_id: str, input: dict) -> dict:
    """Complete a person step with its `needs` (e.g. {"box": {"x0":…, "y0":…,
    "x1":…, "y1":…}} drawn on the `preview_of` step's preview)."""
    return await _call("POST", f"/runs/{_seg(run_id)}/advance", {"step_id": step_id, "input": input})


@tool(annotations=WRITE)
async def scanner_propose(run_id: str, step_id: str, input: dict, note: str | None = None) -> dict:
    """Propose a waiting person step's answer for a person to confirm, e.g. the
    box around the window you found on the `preview_of` step's preview:
    {"box": {"x0":…, "y0":…, "x1":…, "y1":…}} (x1 < x0 crosses the panorama
    seam). Say in `note` what you found and how sure you are. The step keeps
    waiting until a person confirms or changes it in the bridge app (bridge
    0.3.0+); a newer proposal replaces an older one."""
    body = {"step_id": step_id, "input": input}
    if note:
        body["note"] = note
    return await _call("POST", f"/runs/{_seg(run_id)}/propose", body)


@tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
async def scanner_cancel_run(run_id: str) -> dict:
    """Stop a run; a scan in flight is cancelled too."""
    return await _call("POST", f"/runs/{_seg(run_id)}/cancel")


# --------------------------------------------------------------------- log ---


@tool(annotations=READ)
async def scanner_log(project_id: str | None = None, since: str | None = None) -> list:
    """The field log, oldest first (filter by project or RFC 3339 time)."""
    params = {k: v for k, v in (("project_id", project_id), ("since", since)) if v}
    return await _call("GET", "/log", params=params or None)


@tool(annotations=WRITE)
async def scanner_add_note(text: str, project_id: str | None = None) -> dict:
    """Add an observation to the field log."""
    body = {"text": text}
    if project_id:
        body["project_id"] = project_id
    return await _call("POST", "/log", body)


# ------------------------------------------------------------------ prompt ---


@mcp.prompt()
async def run_recipe(recipe_id: str, project_id: str) -> str:
    """Walk a scanning recipe with the person, using the bridge tools."""
    r = await _call("GET", f"/recipes/{_seg(recipe_id)}")
    lines = [f"Run the recipe \"{r['title']}\" (v{r['version']}) in project {project_id}.", r.get("summary", ""), ""]
    if r.get("inputs"):
        lines.append("First ask the person for these inputs:")
        for i in r["inputs"]:
            choices = f" (one of: {', '.join(i['choices'])})" if i.get("choices") else ""
            lines.append(f"- {i['name']}: {i.get('prompt', '')}{choices}")
        lines.append("")
    lines.append(f"Then call scanner_start_run(recipe_id=\"{recipe_id}\", project_id=\"{project_id}\", inputs=…) "
                 "and follow it with scanner_run. The steps:")
    for n, s in enumerate(r["steps"], 1):
        work = ("work out " + ", ".join(s.get("needs", [])) + " yourself"
                + (f" from scanner_preview of step {s['preview_of']}'s scan_id" if s.get("preview_of") else ""))
        how = {
            "action": f"the bridge calls {s.get('operation_id')} itself",
            "person": work + (", then scanner_advance_run it yourself: this step is yours (review = final), "
                              "the person reviews it with the result at the sign-off, and the bridge's checks catch a bad answer"
                              if s.get("review") == "final" else
                              f"the bridge proposes it from step {s['propose_from']}; check it, show the person the evidence, "
                              "then call scanner_sign_off (the one question to the person)"
                              if s.get("propose_from") else
                              ", then scanner_propose it with a note and call scanner_sign_off for the person to confirm"),
            "check": f"the bridge checks {', '.join(s.get('expect', []))} of step {s.get('of')}",
        }[s["kind"]]
        title = s.get("title") or s["step_id"]
        extra = f" — {s['instructions']}" if s.get("instructions") else ""
        lines.append(f"{n}. {title} [{s['kind']}: {how}]{extra}")
    lines += ["", "AI-first: you do the work and the person signs off once. Aim for one person intervention per run.",
              "scanner_start_run asks the person once, in the chat, to allow scanning for the visit when the project "
              "has no permission yet; don't ask them anything else before the sign-off. If a tool says the chat can't ask "
              "(or the person never saw a question), ask them yourself in one line and pass their exact words as person_said.",
              "At the sign-off, show the measurement's evidence image (scanner_measurement) and say what you checked, "
              "then call scanner_sign_off: the person confirms or corrects there. If it reports the chat can't ask, "
              "they use the bridge web UI (Approve the run, then Looks right).",
              "Answer steps marked review = final yourself; for the other person steps, propose and let the person confirm. "
              "Advance those for the person only if they tell you to go ahead without confirming.",
              "Report each measured value with its uncertainty, the person interventions the run counted, "
              "and say plainly if a check failed or a step was declined."]
    return "\n".join(lines)


@mcp.prompt()
async def survey_mission(project_id: str, goal: str) -> str:
    """Run a site visit toward one goal, from the person's sentence to a signed-off ledger."""
    return "\n".join([
        f"The person wants: \"{goal}\" (project {project_id}). You run the visit; they carry the tripod and sign off once at the end.",
        "",
        "0. scanner_processing: if no worker is listed and you haven't asked on this laptop before, ask once whether to "
        "process scans on the laptop (faster) and start the worker if they agree; remember the answer.",
        "1. scanner_set_mission: their words as goal, the kind (exterior, windows, room or custom), and done_when: "
        "criteria concrete enough to check (spacing, which walls, no shadows on what). "
        "If the first station's view shows more than one thing their words could mean (two buildings, two windows, "
        "two objects), ask them which one before the mission names it.",
        "2. At each station: a full dome (scanner_start_scan; it asks the person once for the visit's permission), "
        "then scanner_coverage and scanner_preview. Update the ledger with scanner_update_targets: add every target "
        "you can see or infer (each facade, each window), mark what this station covered with its scan id, keep the rest open.",
        "   After each station, show the person scanner_minimap (the map so far) with a line on what changed. "
        "From the second station on, the bridge registers each dome into one frame by itself: read scanner_registration "
        "(wait while registering_job_id is set). Give each facade target its wall_id from the plan; the bridge then marks "
        "facades covered from the geometry, so don't mark them by eye.",
        ("3. While targets are open: choose the next station for what is still open, not the biggest shadow "
        "(the plan shows open walls in red; scanner_wall_face gives their unseen stretches). "
        "Keep each new station in sight of walls an earlier station saw, or it can't be registered; "
        "a station that isn't confirmed (doubtful, ambiguous, unplaced) doesn't count, so the next one should settle it. "
        "scanner_plan_station computes the spot from the points: pass the subject's preview columns, and take its "
        "first spot unless the ledger needs another (an open wall it doesn't face). "
        "scanner_propose_station marks it on the preview: the tripod, the one landmark they walk past to reach it, "
        "and a sentence naming that landmark (never left, right or compass words). Look at the picture it returns "
        "and propose again if the dot isn't on the landmark or the tripod isn't on ground beside it. "
         "Tell the person that sentence and wait for them to say they've moved."),
        "4. A target you can't cover (a locked yard, a neighbour's wall) is declined, with the reason.",
        "5. When nothing is open: scanner_ready_to_leave with a two-or-three-line summary; the person signs off.",
        "",
        "Ask the person nothing else, except which one when the view shows more than one thing the job could mean. "
        "Report at the end: stations used, the ledger, and anything declined.",
    ])


def main() -> None:
    if not URL or not TOKEN:
        raise SystemExit("bridge mode needs SCANNER_BRIDGE_URL and SCANNER_BRIDGE_TOKEN")
    mcp.run()
