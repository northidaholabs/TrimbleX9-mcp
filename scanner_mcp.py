#!/usr/bin/env python3
"""scanner-mcp — an MCP server for a tripod LiDAR / terrestrial laser scanner
that exposes a local HTTP/REST API (`/api/v1`) over its USB or WiFi link.

Read / query / download tools are always available. Actuating tools (start a
scan, create/delete projects, delete scans) are only registered when the
environment variable SCANNER_ALLOW_ACTUATION is truthy ("1"/"true"/"yes") —
"receive-only by default", because starting a scan moves the instrument and
fires its laser, and the delete tools are destructive.

Config:
  SCANNER_BASE             HTTP/REST API base URL. Default http://192.168.42.1:34567/api/v1
  SCANNER_ALLOW_ACTUATION  Enable actuating/destructive tools when set truthy.
  SCANNER_DOWNLOAD_DIR     Directory downloads are written to (default
                           ~/scanner-downloads). Download tools never write
                           outside it.

Bridge mode: when SCANNER_BRIDGE_URL and SCANNER_BRIDGE_TOKEN are set, the
server instead talks to an x9 bridge service, which enforces the rules for
every client (see scanner_bridge.py).

Transport: stdio (run under an MCP client). Independent project, not affiliated
with any instrument manufacturer.
"""
import asyncio
import functools
import inspect
import json
import math
import os
import re
import struct
import time
import uuid as uuidlib

import httpx

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

__version__ = "0.15.0"

BASE = os.environ.get("SCANNER_BASE", "http://192.168.42.1:34567/api/v1").rstrip("/")
ALLOW_ACTUATION = os.environ.get("SCANNER_ALLOW_ACTUATION", "").lower() in ("1", "true", "yes")
DOWNLOAD_DIR = os.path.realpath(
    os.path.expanduser(os.environ.get("SCANNER_DOWNLOAD_DIR", "~/scanner-downloads"))
)
TIMEOUT = httpx.Timeout(15.0, connect=5.0)

mcp = FastMCP("trimblex9")


def _error_text(e: Exception, peer: str) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from {e.request.url}: {e.response.text[:200]}"
    if isinstance(e, httpx.HTTPError):
        return f"{peer} unreachable ({type(e).__name__}: {e})"
    if isinstance(e, json.JSONDecodeError):
        return f"{peer} sent a response that is not JSON ({e})"
    if isinstance(e, struct.error):
        return f"truncated or malformed data ({e})"
    return f"invalid value ({type(e).__name__}: {e})"


_CONVERTED = (httpx.HTTPError, ValueError, struct.error)  # ValueError covers JSON errors


def error_wrapper(peer: str):
    """Report HTTP, connection, JSON and data errors as tool errors, whose text
    reaches the client (mcp 2.x hides the text of other exceptions). `peer`
    names the other end in messages ("instrument", "bridge")."""
    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*a, **k):
                try:
                    return await fn(*a, **k)
                except _CONVERTED as e:
                    raise ToolError(_error_text(e, peer)) from e
        else:
            @functools.wraps(fn)
            def wrapper(*a, **k):
                try:
                    return fn(*a, **k)
                except _CONVERTED as e:
                    raise ToolError(_error_text(e, peer)) from e
        return wrapper
    return deco


_tool_errors = error_wrapper("instrument")


def tool(**kw):
    """mcp.tool, with HTTP and data failures reported as tool errors."""
    return lambda fn: mcp.tool(**kw)(_tool_errors(fn))


def _url(path: str) -> str:
    return f"{BASE}/{path.lstrip('/')}"


async def _get_json(path: str):
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(_url(path))
        r.raise_for_status()
        return r.json()


# ------------------------------------------------------- download safety -----

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _default_name(stem, ext) -> str:
    """A plain download name built from an id that may hold other characters:
    runs of anything but letters, digits, '_' and '-' become '_'."""
    ext = re.sub(r"[^A-Za-z0-9]", "", str(ext))[:8] or "bin"
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", str(stem)).lstrip("_-")[:127 - len(ext)]
    return f"{stem or 'download'}.{ext}"


def _dest(filename: str | None, default: str, overwrite: bool) -> str:
    """Resolve a download target strictly inside DOWNLOAD_DIR.

    Accepts a plain file name only (letters, digits, '.', '_', '-'; no
    directories, no leading dot). Refuses symlink escapes and, unless
    `overwrite` is set, existing files.
    """
    name = filename or default
    if not _SAFE_NAME.fullmatch(name) or ".." in name:
        raise ToolError(
            f"invalid filename {name!r}: use a plain name such as 'scan-14.tzf' "
            "(letters, digits, '.', '_', '-'; no paths)"
        )
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    path = os.path.realpath(os.path.join(DOWNLOAD_DIR, name))
    if os.path.dirname(path) != DOWNLOAD_DIR:
        raise ToolError(f"refusing to write outside the download directory {DOWNLOAD_DIR}")
    if os.path.exists(path) and not overwrite:
        raise ToolError(f"{path} already exists; pass overwrite=true to replace it")
    return path


async def _stream_to(url: str, path: str) -> int:
    """Stream a GET response to `path` atomically (temp file + rename)."""
    tmp = path + ".part"
    total = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0)) as c:
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    async for chunk in r.aiter_bytes():
                        total += len(chunk)
                        fh.write(chunk)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return total


def _existing_file(filename: str) -> str:
    """Resolve a plain file name to an existing file inside DOWNLOAD_DIR."""
    if not _SAFE_NAME.fullmatch(filename) or ".." in filename:
        raise ToolError(f"invalid filename {filename!r}: use a plain name from the download directory")
    path = os.path.realpath(os.path.join(DOWNLOAD_DIR, filename))
    if os.path.dirname(path) != DOWNLOAD_DIR or not os.path.isfile(path):
        raise ToolError(f"{filename!r} not found in {DOWNLOAD_DIR}; download it first")
    return path


# ------------------------------------------------------- scan-file geometry -----
# Observed .tzf layout (see the trimble-tzf project): u64 at 0x10 points to a
# metadata block holding f64 angle steps (+0x10 horizontal, +0x18 vertical),
# f64 start VA (+0x20, radians from zenith), f64 negated start HA (+0x28) and
# u32 grid rows/cols (+0x30/+0x34).

def _scan_geometry(path: str) -> dict:
    try:
        return _read_scan_geometry(path)
    except struct.error:
        raise ToolError(f"{os.path.basename(path)} is truncated or not a scan file") from None


def _read_scan_geometry(path: str) -> dict:
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        fh.seek(0x10)
        meta = struct.unpack("<Q", fh.read(8))[0]
        if not 0x5c <= meta <= size - 0x38:
            raise ToolError(f"{os.path.basename(path)} does not look like a scan file")
        fh.seek(meta + 0x10)
        step_h, step_v, start_va, neg_start_ha = struct.unpack("<4d", fh.read(32))
        rows, cols = struct.unpack("<2I", fh.read(8))
    if not (0 < step_h < 0.1 and 0 < step_v < 0.1 and rows and cols):
        raise ToolError(f"{os.path.basename(path)}: implausible scan geometry")
    return {"step_h": step_h, "step_v": step_v, "start_ha": (-neg_start_ha) % math.tau,
            "start_va": start_va, "rows": rows, "cols": cols}


def _jpeg_size(path: str) -> tuple[int, int]:
    """(width, height) from a JPEG's SOF marker."""
    try:
        return _read_jpeg_size(path)
    except struct.error:
        raise ToolError(f"{os.path.basename(path)} is a truncated or malformed JPEG") from None


def _read_jpeg_size(path: str) -> tuple[int, int]:
    with open(path, "rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            raise ToolError(f"{os.path.basename(path)} is not a JPEG")
        while True:
            marker = fh.read(2)
            if len(marker) < 2 or marker[0] != 0xFF:
                raise ToolError(f"{os.path.basename(path)}: no frame header found")
            seglen = struct.unpack(">H", fh.read(2))[0]
            if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">xHH", fh.read(5))
                return w, h
            fh.seek(seglen - 2, 1)


# ------------------------------------------------------------- launch safety -----
# The instrument answers 202 even when it cannot parse a scan request, and then
# runs a default full-dome scan. So every launch is validated locally first and
# the echoed request is checked afterwards; a mismatch is cancelled at once.

async def _check_project(project_name: str, project_uuid: str) -> None:
    try:
        uuidlib.UUID(project_uuid)
    except ValueError:
        raise ToolError(f"project_uuid {project_uuid!r} is not a UUID") from None
    projects = await _get_json("projects")
    if not any(p.get("projectName") == project_name and p.get("projectUUID") == project_uuid
               for p in projects):
        raise ToolError("no project with that project_name AND project_uuid; "
                         "see scanner_projects or create one with scanner_create_project")


async def _check_idle() -> None:
    """Refuse to launch or delete while any task is running or queued: another
    client (or another agent) may be driving the instrument, and a scan being
    filed renumbers the lists a delete relies on."""
    tasks = await _get_json("tasks")
    if tasks:
        busy = ", ".join(f"{t.get('type')} {t.get('taskUUID')} ({t.get('state')})" for t in tasks)
        raise ToolError(f"instrument is busy: {busy}. Wait with scanner_wait_for_scan, "
                           "or stop it with scanner_cancel_task if it is yours")


async def _check_mode(scan_mode: str) -> None:
    modes = [m.get("scanMode") for m in await _get_json("scans/modes")]
    if scan_mode not in modes:
        raise ToolError(f"unknown scan_mode {scan_mode!r}; valid: {', '.join(modes)}")


async def _cancel(task_id) -> int:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(_url(f"tasks/{task_id}"))
        return r.status_code


async def _launch(body: dict) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(_url("scans"), json=body)
        r.raise_for_status()
        task = r.json()
    echo = task.get("scanRequest") or {}

    def same(sent, got):
        if isinstance(sent, float):
            return isinstance(got, (int, float)) and abs(got - sent) < 1e-9
        return got == sent

    bad = {k: (v, echo.get(k)) for k, v in body.items() if not same(v, echo.get(k))}
    if bad:
        status = await _cancel(task.get("id"))
        raise ToolError(f"instrument did not accept the request as sent (sent vs echoed: {bad}); "
                           f"cancelled its task (HTTP {status})")
    return _task_summary(task)


async def _delete_verified(kind: str, uuid_key: str, uuid_value: str) -> dict:
    """Delete by UUID. Instrument ids are list positions that shift after every
    delete, so resolve the current id immediately before deleting and confirm
    afterwards that exactly that record is gone."""
    items = await _get_json(kind)
    match = [i for i in items if i.get(uuid_key) == uuid_value]
    if len(match) != 1:
        raise ToolError(f"no {kind[:-1]} with {uuid_key} {uuid_value!r}")
    item_id = match[0]["id"]
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(_url(f"{kind}/{item_id}"))
        r.raise_for_status()
    after = await _get_json(kind)
    if any(i.get(uuid_key) == uuid_value for i in after) or len(after) != len(items) - 1:
        raise ToolError(f"delete of {uuid_value} not confirmed; re-check {kind} before retrying")
    return {"deleted": kind[:-1], uuid_key: uuid_value, "id_at_delete": item_id}


_SECRET_NAME = re.compile(r"(key|psk|passphrase|password|secret|token)$", re.IGNORECASE)


def _redact(obj):
    """Mask secret-looking string fields (e.g. the WiFi passphrase that the
    instrument returns in plain text in its status). Booleans such as
    isFactoryKey are kept."""
    if isinstance(obj, dict):
        return {k: ("***" if isinstance(v, str) and v and _SECRET_NAME.search(k) else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _task_summary(t: dict) -> dict:
    p = t.get("percentage")
    return {
        "id": t.get("id"),
        "taskUUID": t.get("taskUUID"),
        "type": t.get("type"),
        "state": t.get("state"),
        "percent": None if p is None else round(float(p) * 100, 1),
        "scanID": t.get("scanID"),
        "exitResult": t.get("exitResult"),
    }


# ---------------------------------------------------------------- read-only ---

@tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_status() -> dict:
    """Get the instrument's current status: battery, AC state, leveling, on-board
    storage, IMU, laser-pointer, WiFi config (passphrase masked), and the active
    error list."""
    return _redact(await _get_json("status"))


@tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_projects() -> list:
    """List all projects on the instrument (id, projectName, projectUUID,
    nextProjectScanID)."""
    return await _get_json("projects")


@tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_scans() -> list:
    """List all scans as compact records (id, scanUUID, scanMode, points, file
    path, and the project folder the scan belongs to). Ids are list positions
    and change when scans are deleted; scanUUID is stable."""
    scans = await _get_json("scans")
    out = []
    for s in scans:
        t = s.get("tzf") or {}
        f = t.get("file", "") or ""
        out.append({
            "id": s.get("id"),
            "scanUUID": s.get("scanUUID"),
            "scanMode": s.get("scanMode"),
            "nbPoints": t.get("nbPoints"),
            "fileSizeMB": round((t.get("fileSize") or 0) / 1e6, 1),
            "scanFileId": s.get("tzfId"),
            "file": f,
            "project": f.split("/")[0] if "/" in f else None,
            "downloaded": t.get("downloaded"),
        })
    return out


@tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_scan_modes() -> list:
    """List the available scan modes (the density/range/time menu): mode name,
    range (m), point count, file size, scan time (s), spacing, and rate.
    Use these names when starting a scan."""
    return await _get_json("scans/modes")


@tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_errors() -> list:
    """Get the instrument's current error list (type, errorLevel, description)."""
    return await _get_json("errors")


@tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_tasks() -> list:
    """List running or queued tasks (e.g. a scan in progress) with their state
    and percent complete (0-100). An empty list means the instrument is idle."""
    return [_task_summary(t) for t in await _get_json("tasks")]


@tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_wait_for_scan(ctx: Context, timeout_s: int = 900, poll_s: float = 2.0) -> dict:
    """Wait until no task is running (use after scanner_launch_scan), polling the
    task list every `poll_s` seconds (0.5-30) for up to `timeout_s` (1-7200).
    Reports progress to the client when supported. Returns
    {done, timed_out, elapsed_s, last} where `last` is the final task snapshot."""
    timeout_s = max(1, min(int(timeout_s), 7200))
    poll_s = max(0.5, min(float(poll_s), 30.0))
    start = time.monotonic()
    last = None
    while True:
        tasks = await _get_json("tasks")
        elapsed = round(time.monotonic() - start, 1)
        if not tasks:
            return {"done": True, "timed_out": False, "elapsed_s": elapsed, "last": last}
        last = _task_summary(tasks[0])
        if last["percent"] is not None:
            try:
                await ctx.report_progress(last["percent"], 100)
            except Exception:
                pass  # client did not request progress notifications
        if elapsed >= timeout_s:
            return {"done": False, "timed_out": True, "elapsed_s": elapsed, "last": last}
        await asyncio.sleep(poll_s)


@tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def scanner_scanfile_info(scan_file_id: int) -> dict:
    """Get metadata for one point-cloud file: file path, size, point count, and
    whether it has been downloaded."""
    return await _get_json(f"tzfs/{scan_file_id}")


@tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def scanner_download_scanfile(
    scan_file_id: int, filename: str | None = None, overwrite: bool = False
) -> dict:
    """Download a scan's point-cloud file into the server's download directory
    (SCANNER_DOWNLOAD_DIR). `filename` is a plain name (default
    'scan-<id>.tzf'); paths are rejected. Existing files are kept unless
    `overwrite` is true. Streams the full file; returns bytes and path."""
    path = _dest(filename, f"scan-{scan_file_id}.tzf", overwrite)
    n = await _stream_to(_url(f"tzfs/{scan_file_id}/file"), path)
    return {"scanFileId": scan_file_id, "bytes": n, "path": path}


@tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def scanner_download_preview(
    scan_file_id: int, filename: str | None = None, overwrite: bool = False
) -> dict:
    """Download a scan's panoramic preview JPEG into the server's download
    directory (default name 'scan-<id>-preview.jpg'); same rules as
    scanner_download_scanfile."""
    path = _dest(filename, f"scan-{scan_file_id}-preview.jpg", overwrite)
    n = await _stream_to(_url(f"tzfs/{scan_file_id}/preview"), path)
    return {"scanFileId": scan_file_id, "bytes": n, "path": path}


@tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def scanner_preview_to_angles(
    scan_filename: str, preview_filename: str,
    x0: int, y0: int, x1: int, y1: int, margin_deg: float = 0.5,
) -> dict:
    """Convert a box drawn on a scan's preview JPEG into instrument angles for
    scanner_launch_area_scan. Both files must already be in the download
    directory (scanner_download_scanfile + scanner_download_preview of the same
    scan, taken from the station the instrument is still standing on).
    (x0, y0)-(x1, y1) are preview pixel corners, y down; if x1 < x0 the box
    wraps across the panorama seam. `margin_deg` pads every side.
    Returns start_ha/delta_ha/start_va/delta_va in radians (VA from zenith)."""
    geo = _scan_geometry(_existing_file(scan_filename))
    pw, ph = _jpeg_size(_existing_file(preview_filename))
    sx, sy = geo["cols"] / pw, geo["rows"] / ph
    if not (0 <= x0 <= pw and 0 <= x1 <= pw and 0 <= y0 < y1 <= ph):
        raise ToolError(f"box must lie within the {pw}x{ph} preview with y0 < y1")
    margin = math.radians(max(0.0, float(margin_deg)))
    if x1 == x0:
        raise ToolError("box has zero width")
    width_px = x1 - x0 if x1 > x0 else x1 + pw - x0
    start_ha = (geo["start_ha"] + x0 * sx * geo["step_h"] - margin) % math.tau
    delta_ha = min(math.tau, width_px * sx * geo["step_h"] + 2 * margin)
    start_va = max(0.0, geo["start_va"] + y0 * sy * geo["step_v"] - margin)
    end_va = min(math.pi, geo["start_va"] + y1 * sy * geo["step_v"] + margin)
    return {
        "start_ha": round(start_ha, 6), "delta_ha": round(delta_ha, 6),
        "start_va": round(start_va, 6), "delta_va": round(end_va - start_va, 6),
        "degrees": {"ha": [round(math.degrees(start_ha), 2), round(math.degrees(delta_ha), 2)],
                    "va": [round(math.degrees(start_va), 2), round(math.degrees(end_va - start_va), 2)]},
        "preview_cells_per_px": [round(sx, 3), round(sy, 3)],
    }


# --------------------------------------------- actuating / destructive -------
# Registered only when SCANNER_ALLOW_ACTUATION is truthy.

if ALLOW_ACTUATION:

    @tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
    async def scanner_create_project(project_name: str, project_uuid: str) -> dict:
        """Create a project on the instrument. Supply a projectName and a unique
        projectUUID (UUIDv4). Returns the created project record."""
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(_url("projects"),
                             json={"projectName": project_name, "projectUUID": project_uuid})
            r.raise_for_status()
            return r.json()

    @tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
    async def scanner_launch_scan(project_name: str, project_uuid: str, scan_mode: str,
                                  color: bool = False) -> dict:
        """ACTUATING: start a full-dome scan — moves the instrument and fires its
        laser for the mode's duration. Pass BOTH project_name and project_uuid of
        an existing project; `scan_mode` is a name from scanner_scan_modes;
        `color` also captures images (slower). Refuses while any task is running
        (another client may be using the instrument). Inputs are checked first, and if
        the instrument does not echo the request back as sent the task is
        cancelled and an error raised. Returns the task; follow it with
        scanner_wait_for_scan. Ensure the instrument is safely positioned."""
        await _check_idle()
        await _check_project(project_name, project_uuid)
        await _check_mode(scan_mode)
        return await _launch({"scanMode": scan_mode, "projectName": project_name,
                              "projectUUID": project_uuid, "scanType": "FullDome",
                              "fastMode": False, "colorMode": "ON" if color else "OFF"})

    @tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
    async def scanner_launch_area_scan(project_name: str, project_uuid: str, scan_mode: str,
                                       start_ha: float, delta_ha: float,
                                       start_va: float, delta_va: float,
                                       color: bool = False) -> dict:
        """ACTUATING: scan only a window of the scene. Angles are radians: HA
        0..2π (horizontal), VA from the zenith 0..π; get them from
        scanner_preview_to_angles. Same project/mode/safety rules as
        scanner_launch_scan. Small windows take about a minute without color."""
        start_ha, delta_ha = float(start_ha) % math.tau, float(delta_ha)
        start_va, delta_va = float(start_va), float(delta_va)
        if not 0 < delta_ha <= math.tau:
            raise ToolError("delta_ha must be in (0, 2π]")
        if not (0 <= start_va <= math.pi and 0 < delta_va <= math.pi - start_va):
            raise ToolError("need 0 <= start_va and 0 < delta_va with start_va + delta_va <= π")
        await _check_idle()
        await _check_project(project_name, project_uuid)
        await _check_mode(scan_mode)
        return await _launch({"scanMode": scan_mode, "projectName": project_name,
                              "projectUUID": project_uuid, "scanType": "AreaScan",
                              "fastMode": False, "colorMode": "ON" if color else "OFF",
                              "startHA": start_ha, "deltaHA": delta_ha,
                              "startVA": start_va, "deltaVA": delta_va})

    @tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
    async def scanner_cancel_task(task_uuid: str) -> dict:
        """Cancel a running task (e.g. stop a scan in progress) by its taskUUID
        from scanner_tasks. The instrument still files the partial sweep as a scan;
        check scanner_scans and delete it if it is not wanted."""
        match = [t for t in await _get_json("tasks") if t.get("taskUUID") == task_uuid]
        if not match:
            raise ToolError(f"no running task {task_uuid!r}; see scanner_tasks")
        status = await _cancel(match[0]["id"])
        if status >= 400:
            raise ToolError(f"cancel refused (HTTP {status})")
        return {"cancelled": task_uuid, "status": status}

    @tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
    async def scanner_delete_scan(scan_uuid: str) -> dict:
        """DESTRUCTIVE: permanently delete a scan (and its file), identified by its
        scanUUID from scanner_scans. Refuses while any task is running.
        Deletion is confirmed afterwards."""
        await _check_idle()
        return await _delete_verified("scans", "scanUUID", scan_uuid)

    @tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
    async def scanner_delete_project(project_uuid: str) -> dict:
        """DESTRUCTIVE: permanently delete a project and its scans, identified by
        its projectUUID from scanner_projects. Refuses while any task is
        running. Deletion is confirmed afterwards."""
        await _check_idle()
        return await _delete_verified("projects", "projectUUID", project_uuid)


def main() -> None:
    if os.environ.get("SCANNER_BRIDGE_URL"):
        import scanner_bridge

        scanner_bridge.main()
        return
    mcp.run()


if __name__ == "__main__":
    main()
