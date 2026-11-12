"""Bridge-mode tools against a scripted in-process bridge stub: the requests
each tool sends and how it reads the replies, including the error paths.
Run: pytest -q"""
import asyncio
import http.server
import importlib
import json
import os
import sys
import tempfile
import threading
import types
from typing import ClassVar

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # Asking the person in the chat needs an SDK with elicitation (mcp 1.7 has none).
    from mcp.types import ClientCapabilities, ElicitationCapability
    ClientCapabilities(elicitation=ElicitationCapability())
    HAS_ELICITATION = True
except Exception:
    HAS_ELICITATION = False
needs_elicitation = pytest.mark.skipif(not HAS_ELICITATION, reason="this mcp SDK has no elicitation")

JPEG = b"\xff\xd8\xff\xe0\x00\x04\x00\x00\xff\xc0\x00\x0b\x08\x00\x0a\x00\x14\x01\x01\x11\x00\xff\xd9"  # 20 x 10
PNG = b"\x89PNG\r\n\x1a\n"


class Bridge(http.server.BaseHTTPRequestHandler):
    """Replies from `routes`: {(method, path): reply}; a reply is (code, body) or
    (code, body, content type), or a list of them served in turn. A dict or list
    body is sent as JSON. Every request is kept in `seen`."""
    routes: ClassVar[dict] = {}
    seen: ClassVar[list] = []

    def _handle(self):
        path, _, query = self.path.partition("?")
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("Content-Type") or ""
        body = (json.loads(raw) if "json" in ctype or not ctype else raw.decode()) if raw else None
        Bridge.seen.append({"method": self.command, "path": path, "query": query, "body": body,
                            "auth": self.headers.get("Authorization"), "ctype": ctype})
        reply = Bridge.routes.get((self.command, path), (404, {"code": "not_found", "title": "No such thing"}))
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        code, data, *ctype = reply
        if data == "DROP":  # promise more than is sent, then hang up
            self.send_response(code)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"part")
            self.close_connection = True
            return
        if not isinstance(data, bytes):
            data, ctype = json.dumps(data).encode(), ["application/json"]
        self.send_response(code)
        self.send_header("Content-Type", (ctype or ["application/octet-stream"])[0])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = _handle

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}/v1"


@pytest.fixture
def b(monkeypatch):
    # Imported late: test_offline sets the environment scanner_mcp reads at import.
    mod = importlib.import_module("scanner_bridge")
    monkeypatch.setattr(mod, "URL", BASE)
    monkeypatch.setattr(mod, "TOKEN", "agent-tok")
    monkeypatch.setattr(mod, "PERSON_TOKEN", "")
    monkeypatch.setattr(mod.direct, "DOWNLOAD_DIR", os.path.realpath(tempfile.mkdtemp()))
    Bridge.routes, Bridge.seen = {}, []
    return mod


def route(method, path, *reply):
    Bridge.routes[(method, "/v1" + path)] = list(reply) if len(reply) > 1 else reply[0]


def sent(i=-1):
    r = dict(Bridge.seen[i])
    r["path"] = r["path"].removeprefix("/v1")
    return r


def run(b, name, *args, **kw):
    f = getattr(b, name)
    return asyncio.run(getattr(f, "fn", f)(*args, **kw))


class Chat:
    """A chat client. `answers`: what the person says to each question, in
    order (a dict of fields to accept with, or an action such as "decline")."""

    def __init__(self, *answers, can_ask=True):
        self.answers, self.asked, self.progress = list(answers), [], []
        self.session = types.SimpleNamespace(check_client_capability=lambda caps: can_ask)

    async def elicit(self, message, schema):
        self.asked.append(message)
        a = self.answers.pop(0)
        if isinstance(a, str):
            return types.SimpleNamespace(action=a, data=None)
        fields = {k: f.default for k, f in schema.model_fields.items()} | a
        return types.SimpleNamespace(action="accept", data=schema(**fields))

    async def report_progress(self, done, total):
        self.progress.append(done)


# ------------------------------------------------------------ plain calls -----

def test_thin_tools_send_the_documented_requests(b):
    for method, path in [("GET", "/scan-modes"), ("GET", "/projects"), ("POST", "/projects"), ("GET", "/scans"),
                         ("GET", "/scans/s1"), ("GET", "/jobs/j1"), ("POST", "/jobs/j1/cancel"), ("POST", "/aim"),
                         ("POST", "/scans/s1/exports"), ("GET", "/exports/e1"), ("POST", "/scans/s1/measurements"),
                         ("GET", "/scans/s1/next-station/plan"), ("GET", "/projects/p1/mission"),
                         ("PUT", "/projects/p1/mission"), ("POST", "/projects/p1/mission/targets"),
                         ("POST", "/projects/p1/registration/face"), ("GET", "/recipes"), ("GET", "/recipes/r1"),
                         ("GET", "/runs/r1"), ("POST", "/runs/r1/advance"), ("POST", "/runs/r1/propose"),
                         ("POST", "/runs/r1/cancel"), ("GET", "/log"), ("POST", "/log")]:
        route(method, path, (200, {"ok": path}))
    calls = [
        (("scanner_scan_modes",), ("GET", "/scan-modes", None)),
        (("scanner_projects",), ("GET", "/projects", None)),
        (("scanner_create_project", "Front"), ("POST", "/projects", {"name": "Front"})),
        (("scanner_scans",), ("GET", "/scans", None)),
        (("scanner_scans", "p 1"), ("GET", "/scans", None)),
        (("scanner_scan", "s1"), ("GET", "/scans/s1", None)),
        (("scanner_job", "j1"), ("GET", "/jobs/j1", None)),
        (("scanner_cancel_job", "j1"), ("POST", "/jobs/j1/cancel", None)),
        (("scanner_aim", "s1", 1, 2, 3, 4, -1.0), ("POST", "/aim", {"reference_scan_id": "s1", "margin_rad": 0.0,
                                                                     "box": {"x0": 1, "y0": 2, "x1": 3, "y1": 4}})),
        (("scanner_export", "s1"), ("POST", "/scans/s1/exports", {"format": "ply", "intensity": "auto"})),
        (("scanner_export_info", "e1"), ("GET", "/exports/e1", None)),
        (("scanner_measure", "s1"), ("POST", "/scans/s1/measurements", {"kind": "opening"})),
        (("scanner_plan_station", "s1"), ("GET", "/scans/s1/next-station/plan", None)),
        (("scanner_plan_station", "s1", 30, 10), ("GET", "/scans/s1/next-station/plan", None)),
        (("scanner_mission", "p1"), ("GET", "/projects/p1/mission", None)),
        (("scanner_set_mission", "p1", "goal", "room", "all walls"),
         ("PUT", "/projects/p1/mission", {"goal": "goal", "kind": "room", "done_when": "all walls", "targets": []})),
        (("scanner_update_targets", "p1", [{"target_id": "n"}]),
         ("POST", "/projects/p1/mission/targets", {"targets": [{"target_id": "n"}]})),
        (("scanner_wall_face", "p1", "w3"), ("POST", "/projects/p1/registration/face", {"wall_id": "w3"})),
        (("scanner_wall_face", "p1", "", [0, 0], [4, 0]),
         ("POST", "/projects/p1/registration/face", {"from": [0, 0], "to": [4, 0]})),
        (("scanner_recipes",), ("GET", "/recipes", None)),
        (("scanner_recipe", "r1"), ("GET", "/recipes/r1", None)),
        (("scanner_run", "r1"), ("GET", "/runs/r1", None)),
        (("scanner_advance_run", "r1", "box", {"box": 1}), ("POST", "/runs/r1/advance", {"step_id": "box", "input": {"box": 1}})),
        (("scanner_propose", "r1", "box", {"box": 1}), ("POST", "/runs/r1/propose", {"step_id": "box", "input": {"box": 1}})),
        (("scanner_propose", "r1", "box", {"box": 1}, "clear frame"),
         ("POST", "/runs/r1/propose", {"step_id": "box", "input": {"box": 1}, "note": "clear frame"})),
        (("scanner_cancel_run", "r1"), ("POST", "/runs/r1/cancel", None)),
        (("scanner_log",), ("GET", "/log", None)),
        (("scanner_log", "p1", "2026-01-01T00:00:00Z"), ("GET", "/log", None)),
        (("scanner_add_note", "sway"), ("POST", "/log", {"text": "sway"})),
        (("scanner_add_note", "sway", "p1"), ("POST", "/log", {"text": "sway", "project_id": "p1"})),
    ]
    for (name, *args), (method, path, body) in calls:
        assert run(b, name, *args) == {"ok": path}, name
        s = sent()
        assert (s["method"], s["path"], s["body"]) == (method, path, body), name
        assert s["auth"] == "Bearer agent-tok"
    queries = [s["query"] for s in Bridge.seen if s["query"]]
    assert queries == ["project_id=p+1", "subject=30%2C10", "project_id=p1&since=2026-01-01T00%3A00%3A00Z"]


def test_permissions_filter_by_project(b):
    route("GET", "/grants", (200, [{"project_id": "p1"}, {"project_id": "p2"}]))
    assert run(b, "scanner_permissions") == [{"project_id": "p1"}, {"project_id": "p2"}]
    assert run(b, "scanner_permissions", "p2") == [{"project_id": "p2"}]


def test_bridge_refusals_read_plainly(b):
    route("GET", "/scans/a", (409, {"code": "busy", "title": "Scanner busy", "detail": "a scan runs",
                                    "hint": "wait for job j1"}))
    route("GET", "/scans/b", (400, {"title": "Bad request"}))
    route("GET", "/scans/c", (502, b"<html>bad gateway</html>", "text/html"))
    route("GET", "/scans/d", (200, b""))
    with pytest.raises(b.BridgeError, match=r"^busy: Scanner busy — a scan runs \(hint: wait for job j1\)$"):
        run(b, "scanner_scan", "a")
    with pytest.raises(b.BridgeError, match=r"^400: Bad request$"):
        run(b, "scanner_scan", "b")
    with pytest.raises(b.BridgeError, match=r"^HTTP 502: <html>bad gateway</html>$"):
        run(b, "scanner_scan", "c")
    assert run(b, "scanner_scan", "d") is None  # an empty reply


def test_unreachable_bridge_is_a_readable_error(b, monkeypatch):
    monkeypatch.setattr(b, "URL", "http://127.0.0.1:9/v1")
    with pytest.raises(b.ToolError, match="bridge unreachable"):
        run(b, "scanner_status")


# ------------------------------------------------------- jobs and waiting -----

def test_wait_for_job_reports_progress_until_done(b, monkeypatch):
    route("GET", "/jobs/j1", (200, {"state": "running", "progress": 0.25}), (200, {"state": "queued"}),
          (200, {"state": "succeeded", "result": {"scan_id": "s9"}}))
    slept = []

    async def sleep(s):
        slept.append(s)

    monkeypatch.setattr(b, "asyncio", types.SimpleNamespace(sleep=sleep))
    chat = Chat()
    w = run(b, "scanner_wait_for_job", chat, "j1")
    assert w["done"] and w["job"]["result"] == {"scan_id": "s9"} and not w["waiting_for_approval"]
    assert chat.progress == [25.0] and slept == [1.0, 1.0]


def test_wait_for_job_times_out(b, monkeypatch):
    route("GET", "/jobs/j1", (200, {"state": "awaiting_approval", "progress": 0}))
    clock = iter(range(0, 100_000, 1000))
    monkeypatch.setattr(b, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))

    class Mute:  # a client that never asked for progress
        async def report_progress(self, done, total):
            raise RuntimeError("no progress token")

    w = run(b, "scanner_wait_for_job", Mute(), "j1", timeout_s=5, return_on_approval=False)
    assert w == {"job": {"state": "awaiting_approval", "progress": 0}, "done": False, "timed_out": True,
                 "waiting_for_approval": True, "elapsed_s": 1000}


# --------------------------------------------------- tools with a picture -----

def test_preview_gives_size_and_image(b):
    route("GET", "/scans/s1/preview", (200, JPEG, "image/jpeg"))
    text, img = run(b, "scanner_preview", "s1")
    assert "20 x 10 px" in text and img.data == JPEG
    route("GET", "/scans/s2/preview", (404, {"code": "not_found", "title": "no preview"}))
    with pytest.raises(b.BridgeError, match="not_found"):
        run(b, "scanner_preview", "s2")
    assert b._jpeg_dims(b"\xff\xd8\x00\x00" + b"\0" * 20) == (0, 0)  # not a marker
    assert b._jpeg_dims(b"\xff\xd8\xff\xd9") == (0, 0)  # too short to hold a frame header


@pytest.mark.parametrize(("tool", "args", "first", "image"), [
    ("scanner_measurement", ("m1",), ("GET", "/measurements/m1"), "/measurements/m1/evidence"),
    ("scanner_coverage", ("s1",), ("GET", "/scans/s1/coverage"), "/scans/s1/coverage/image"),
    ("scanner_propose_station", ("s1", "Past the shed.", 5, 6), ("PUT", "/scans/s1/next-station"),
     "/scans/s1/next-station/image"),
])
def test_picture_tools_fall_back_to_text(b, tool, args, first, image):
    route(*first, (200, {"value": 1}))
    route("GET", image, (200, PNG, "image/png"), (500, b"render failed"))
    out = run(b, tool, *args)
    assert json.loads(out[0]) == {"value": 1} and out[1].data == PNG
    out = run(b, tool, *args)
    assert len(out) == 1 and json.loads(out[0]) == {"value": 1}


def test_propose_station_body(b):
    route("PUT", "/scans/s1/next-station", (200, {}))
    run(b, "scanner_propose_station", "s1", "Past the shed.", 5, 6, "on the path",
        labels=[{"x": 1, "y": 2, "text": "shed", "kind": "landmark"}])
    put = next(s for s in Bridge.seen if s["method"] == "PUT")
    assert put["body"] == {"marker": {"x": 5, "y": 6, "label": "on the path"}, "note": "Past the shed.",
                           "labels": [{"x": 1, "y": 2, "text": "shed", "kind": "landmark"}], "boxes": []}


def test_ask_answers_with_evidence_when_there_is_some(b, monkeypatch):
    path = "/projects/p1/mission/targets/north/ask"
    route("POST", path, (200, {"verdict": "not_yet"}), (200, {"verdict": "answer", "evidence_url": "x"}))
    route("GET", path + "/image", (200, PNG, "image/png"), (404, b""))
    assert json.loads(run(b, "scanner_ask", "p1", "north")[0]) == {"verdict": "not_yet"}
    out = run(b, "scanner_ask", "p1", "north")
    assert len(out) == 2 and out[1].data == PNG
    assert len(run(b, "scanner_ask", "p1", "north")) == 1  # the image is gone


def test_registration_draws_the_plan_once_a_station_is_placed(b):
    route("GET", "/projects/p1/registration", (200, {"stations": [{"placed": False}]}),
          (200, {"stations": [{"placed": True}]}))
    route("GET", "/projects/p1/registration/plan", (200, PNG, "image/png"), (500, b""))
    assert len(run(b, "scanner_registration", "p1")) == 1
    assert run(b, "scanner_registration", "p1")[1].data == PNG
    assert len(run(b, "scanner_registration", "p1")) == 1


def test_minimap_lists_untrusted_stations(b):
    route("GET", "/projects/p1/registration", (200, {"summary": "3 stations", "stations": [
        {"station": 1, "verdict": "frame"}, {"station": 2, "verdict": "doubtful", "reasons": ["off by 0.4 m"]},
        {"station": 3, "verdict": None}]}))
    route("GET", "/projects/p1/minimap", (200, PNG, "image/png"), (503, b""))
    text, img = run(b, "scanner_minimap", "p1")
    assert text == "3 stations\nStation 2: doubtful (off by 0.4 m)\nStation 3: not placed" and img.data == PNG
    assert run(b, "scanner_minimap", "p1") == [text + "\n(no map: 503)"]


def test_processing_names_the_workers(b, monkeypatch):
    route("GET", "/workers", (200, [{"worker": "laptop-1"}, {"worker": "laptop-2"}]))
    out = run(b, "scanner_processing")
    assert out["runs_on"] == "laptop-1, laptop-2"
    assert out["start_worker"]["command"] == f"x9-bridge worker --bridge {BASE.removesuffix('/v1')}"


def test_processing_without_a_url_names_the_field_setup(b, monkeypatch):
    async def no_workers(method, path, *a, **k):
        return []

    monkeypatch.setattr(b, "_call", no_workers)
    monkeypatch.setattr(b, "URL", "")
    out = run(b, "scanner_processing")
    assert out["runs_on"] == "the Pi" and out["start_worker"]["command"].endswith("--bridge http://10.55.0.1:8795")


# ------------------------------------------------------------------- saves -----

LAST = {"worker": "laptop", "folder": "/Users/crew/X9 Projects/Yard-p1", "saved_at": "2026-11-11T21:00:00Z",
        "files": 5, "bytes": 2_400_000_000, "stations": [{"station": 1, "copc": "stations/station-01.copc.laz"}]}


def test_save_starts_a_save_and_says_where_it_stands(b):
    waiting = {"job_id": "j9", "kind": "save", "state": "running", "phase": "waiting for the laptop"}
    route("POST", "/projects/p1/save", (202, waiting))
    route("GET", "/projects/p1/save", (200, {"project_id": "p1", "last": LAST, "job": waiting}))
    out = run(b, "scanner_save", "p1")
    assert out["job"] == waiting and out["last"] == LAST
    assert out["message"].startswith("Waiting for the laptop: no worker has taken the save yet.")
    assert sent(-2)["method"] == "POST" and sent(-2)["path"] == "/projects/p1/save" and sent(-2)["body"] is None
    route("POST", "/projects/p1/save", (202, dict(waiting, phase="saving on laptop")))
    assert run(b, "scanner_save", "p1")["message"] == "Saving now (saving on laptop); job j9."


def test_saved_reads_the_last_save(b):
    route("GET", "/projects/p1/save", (200, {"project_id": "p1", "last": LAST, "job": None}))
    out = run(b, "scanner_saved", "p1")
    assert out["message"] == "Saved on laptop at 2026-11-11T21:00:00Z: /Users/crew/X9 Projects/Yard-p1 (5 files, 2.4 GB)."
    route("GET", "/projects/p1/save", (200, {"project_id": "p1", "last": None, "job": None}))
    assert run(b, "scanner_saved", "p1")["message"] == "Not saved to a laptop yet."


def test_saving_on_an_older_bridge_or_with_nothing_registered_reads_plainly(b):
    for name in ("scanner_save", "scanner_saved"):
        with pytest.raises(b.ToolError, match="needs bridge 0.18.0 or later"):
            run(b, name, "p1")
    route("POST", "/projects/p1/save", (409, {"code": "conflict", "title": "Conflict",
                                              "detail": "no station of this project is registered yet, so there is nothing to save"}))
    with pytest.raises(b.BridgeError, match="nothing to save"):
        run(b, "scanner_save", "p1")


def test_ready_to_leave_reports_the_save_the_sign_off_starts(b, monkeypatch):
    mission_setup(state="ready")
    monkeypatch.setattr(b, "PERSON_TOKEN", "person-tok")
    waiting = {"job_id": "j9", "phase": "waiting for the laptop"}
    route("GET", "/projects/p1/save", (200, {"project_id": "p1", "last": None, "job": waiting}))
    out = run(b, "scanner_ready_to_leave", "p1", "Done.", person_said="sign it")
    assert out["signed_off"] and out["save"]["job"] == waiting
    assert out["save"]["message"].startswith("Waiting for the laptop")
    # A save that can't be read leaves the sign-off as it is.
    route("GET", "/projects/p1/save", (500, {"code": "internal", "title": "Oops"}))
    assert "save" not in run(b, "scanner_ready_to_leave", "p1", "Done.", person_said="sign it")


# --------------------------------------------------------------- downloads -----

def test_download_copc_export_and_named_scanfile(b):
    route("GET", "/exports/e1", (200, {"format": "copc"}))
    route("GET", "/exports/e1/file", (200, b"LASF0000"))
    d = run(b, "scanner_download_export", "e1")
    assert os.path.basename(d["path"]) == "e1.copc.laz" and d["bytes"] == 8
    with pytest.raises(b.ToolError, match="already exists"):
        run(b, "scanner_download_export", "e1")
    route("GET", "/exports/e2", (200, {}))  # no format: ply
    route("GET", "/exports/e2/file", (200, b"ply"))
    assert run(b, "scanner_download_export", "e2", "cloud.ply")["path"].endswith("/cloud.ply")


def test_failed_downloads_leave_nothing_behind(b):
    route("GET", "/scans/s1/tzf", (410, {"code": "gone", "title": "file deleted on the scanner"}))
    with pytest.raises(b.BridgeError, match="gone: file deleted"):
        run(b, "scanner_download_scanfile", "s1")
    route("GET", "/scans/s2/tzf", (200, "DROP"))
    with pytest.raises(b.ToolError, match="bridge unreachable"):
        run(b, "scanner_download_scanfile", "s2")
    assert os.listdir(b.direct.DOWNLOAD_DIR) == []


# ---------------------------------------------------- permission to scan -----

def scan_setup(grants=()):
    route("GET", "/scan-modes", (200, [{"mode": "STD_L0"}]))
    route("GET", "/grants", (200, list(grants)))
    route("GET", "/projects", (200, [{"project_id": "p1", "name": "Front window"}]))
    route("POST", "/scans", (200, {"job_id": "j1", "state": "queued"}))
    route("POST", "/grants", (200, {"grant_id": "g1"}))


def test_start_scan_window_and_unknown_mode(b):
    scan_setup()
    with pytest.raises(b.ToolError, match="all four window angles"):
        run(b, "scanner_start_scan", "p1", "STD_L0", start_ha=1.0)
    job = run(b, "scanner_start_scan", "p1", "STD_L9", start_ha=7.0, delta_ha=0.2, start_va=1.0, delta_va=0.3,
              note="station 2")
    assert job["permission"] == "not asked"
    assert not any(s["path"].endswith("/grants") for s in Bridge.seen)  # nothing asked for an unknown mode
    body = sent()["body"]
    assert body["kind"] == "area" and body["note"] == "station 2"
    assert abs(body["window"]["start_ha"] - (7.0 - 2 * 3.141592653589793)) < 1e-9


def test_start_scan_permission_without_asking(b, monkeypatch):
    scan_setup(grants=[{"project_id": "p1", "max_scans": 20, "scans_used": 20},  # used up
                       {"project_id": "p1", "run_id": "r9"},  # one run only
                       {"project_id": "p1", "max_scans": None}])
    assert run(b, "scanner_start_scan", "p1", "STD_L0")["permission"] == "already given"
    scan_setup()
    assert run(b, "scanner_start_scan", "p1", "STD_L0")["permission"].startswith("none: no person token")
    monkeypatch.setattr(b, "PERSON_TOKEN", "person-tok")
    with pytest.raises(b.ToolError, match="can't ask the person directly.*Front window"):
        run(b, "scanner_start_scan", "p1", "STD_L0", ctx=Chat(can_ask=False))
    with pytest.raises(b.ToolError, match="can't ask"):
        run(b, "scanner_start_scan", "p1", "STD_L0", ctx=types.SimpleNamespace())  # no session at all
    job = run(b, "scanner_start_scan", "p1", "STD_L0", person_said="  yes, go ahead ")
    assert job["permission"] == "relayed from the chat"
    grant = next(s for s in Bridge.seen if s["method"] == "POST" and s["path"] == "/v1/grants")
    assert grant["auth"] == "Bearer person-tok"
    assert grant["body"]["note"] == "relayed by the agent: “yes, go ahead”" and grant["body"]["max_scans"] == 20


@needs_elicitation
def test_start_scan_asks_in_the_chat(b, monkeypatch):
    monkeypatch.setattr(b, "PERSON_TOKEN", "person-tok")
    scan_setup()
    with pytest.raises(b.ToolError, match="did not allow"):
        run(b, "scanner_start_scan", "p1", "STD_L0", ctx=Chat({"allow": False}))
    with pytest.raises(b.ToolError, match="action: decline"):
        run(b, "scanner_start_scan", "p1", "STD_L0", ctx=Chat("decline"))
    assert not any(s["method"] == "POST" for s in Bridge.seen)
    chat = Chat({})
    assert run(b, "scanner_start_scan", "p1", "STD_L0", ctx=chat)["permission"] == "given in the chat"
    assert "Front window" in chat.asked[0]


# ------------------------------------------------------------- sign-offs -----

MISSION = {"goal": "Scan the house", "state": "active",
           "targets": [{"status": "covered"}, {"status": "covered"}, {"status": "declined"}, {"status": "open"}]}


def mission_setup(state="active"):
    route("GET", "/projects/p1/mission", (200, dict(MISSION, state=state)))
    route("POST", "/projects/p1/mission/ready", (200, dict(MISSION, state="ready")))
    route("POST", "/projects/p1/mission/sign-off", (200, {"state": "signed_off"}))


def test_ready_to_leave_without_asking(b, monkeypatch):
    mission_setup()
    out = run(b, "scanner_ready_to_leave", "p1", "North covered.")
    assert out["signed_off"] is False and "bridge web UI" in out["message"]
    assert sent()["path"] == "/projects/p1/mission/ready" and sent()["body"] == {"summary": "North covered."}
    mission_setup(state="ready")
    Bridge.seen.clear()
    monkeypatch.setattr(b, "PERSON_TOKEN", "person-tok")
    out = run(b, "scanner_ready_to_leave", "p1", "North covered.", ctx=Chat(can_ask=False))
    assert "Ready to leave: Scan the house (2 covered, 1 declined). North covered." in out["message"]
    assert not any(s["path"].endswith("/ready") for s in Bridge.seen)
    out = run(b, "scanner_ready_to_leave", "p1", "North covered.", person_said="sign it")
    assert out == {"signed_off": True, "relayed": True, "mission": {"state": "signed_off"}}
    # The sign-off, then a look at the save it started (none on this bridge).
    assert sent(-2)["auth"] == "Bearer person-tok"
    assert sent(-2)["body"] == {"confirmed_via": "relayed by the agent: “sign it”"}
    assert sent()["path"] == "/projects/p1/save"


@needs_elicitation
def test_ready_to_leave_asks_in_the_chat(b, monkeypatch):
    monkeypatch.setattr(b, "PERSON_TOKEN", "person-tok")
    mission_setup(state="ready")
    out = run(b, "scanner_ready_to_leave", "p1", "Done.", ctx=Chat("cancel"))
    assert out["asked"] and out["action"] == "cancel" and not out["signed_off"]
    out = run(b, "scanner_ready_to_leave", "p1", "Done.", ctx=Chat({"confirm": False}))
    assert not out["signed_off"] and "didn't sign it off" in out["message"]
    assert not any(s["path"].endswith("/sign-off") for s in Bridge.seen)
    out = run(b, "scanner_ready_to_leave", "p1", "Done.", ctx=Chat({}))
    assert out == {"signed_off": True, "mission": {"state": "signed_off"}}
    assert sent(-2)["body"] == {"confirmed_via": "asked in the chat"}


def run_setup(run_doc, recipe_steps):
    route("GET", "/runs/r1", (200, run_doc))
    route("GET", "/recipes/window", (200, {"steps": recipe_steps}))
    route("POST", "/runs/r1/advance", (200, {"state": "running"}))


WAITING = {"recipe_id": "window", "waiting_for": "input", "current_step_id": "sign", "steps": [
    {"step_id": "box"}, {"step_id": "sign", "proposal": {"input": {"width_mm": 1180, "height_mm": 900.5,
                                                                   "ok": True, "box": [1, 2]}}}]}
NEEDS = [{"step_id": "sign", "needs": ["width_mm", "height_mm", "ok", "box"]}]


def test_sign_off_refuses_what_it_cannot_sign(b):
    run_setup({"state": "running", "waiting_for": None}, [])
    with pytest.raises(b.ToolError, match="not waiting for a person"):
        run(b, "scanner_sign_off", "r1")
    run_setup(WAITING, [{"step_id": "sign", "review": "final"}])
    with pytest.raises(b.ToolError, match="is yours"):
        run(b, "scanner_sign_off", "r1")
    run_setup(dict(WAITING, steps=[{"step_id": "sign"}]), NEEDS)
    with pytest.raises(b.ToolError, match="nothing to sign off yet"):
        run(b, "scanner_sign_off", "r1")


def test_sign_off_without_asking(b, monkeypatch):
    run_setup(WAITING, NEEDS)
    out = run(b, "scanner_sign_off", "r1")
    assert not out["signed_off"] and "bridge web UI, run r1" in out["message"]
    monkeypatch.setattr(b, "PERSON_TOKEN", "person-tok")
    out = run(b, "scanner_sign_off", "r1", ctx=Chat(can_ask=False))
    assert "width mm 1180, height mm 900.5" in out["message"]  # only the numbers; booleans are not fields
    with pytest.raises(b.ToolError, match="corrections can only change width_mm, height_mm; not box"):
        run(b, "scanner_sign_off", "r1", person_said="ok", corrections={"box": [0, 0]})
    out = run(b, "scanner_sign_off", "r1", person_said="ok but 1190", corrections={"width_mm": 1190, "height_mm": 900.5})
    assert out["signed_off"] and out["relayed"] and out["corrected"] == {"width_mm": 1190}
    advance = sent()
    assert advance["auth"] == "Bearer person-tok" and advance["body"]["step_id"] == "sign"
    assert advance["body"]["input"]["width_mm"] == 1190
    assert advance["body"]["input"]["confirmed_via"] == "relayed by the agent: “ok but 1190”"


@needs_elicitation
def test_sign_off_keeps_fractional_corrections(b, monkeypatch):
    monkeypatch.setattr(b, "PERSON_TOKEN", "person-tok")
    run_setup(WAITING, NEEDS)
    out = run(b, "scanner_sign_off", "r1", ctx=Chat({"confirm": False, "height_mm": 901.25}))
    assert out["signed_off"] and out["corrected"] == {"height_mm": 901.25}


def test_shown_values_fall_back_to_the_whole_proposal(b, monkeypatch):
    monkeypatch.setattr(b, "PERSON_TOKEN", "person-tok")
    run_setup(WAITING, [{"step_id": "sign", "needs": ["box"]}])
    out = run(b, "scanner_sign_off", "r1", ctx=Chat(can_ask=False))
    assert '"box": [1, 2]' in out["message"]


# ------------------------------------------------------------------ prompts -----

RECIPE = {
    "title": "Window", "version": "2", "summary": "Measure a window.",
    "inputs": [{"name": "purpose", "prompt": "What for?", "choices": ["blinds", "glass"]}, {"name": "note"}],
    "steps": [{"step_id": "dome", "kind": "action", "operation_id": "scan_full_dome"},
              {"step_id": "box", "kind": "person", "needs": ["box"], "preview_of": "dome", "review": "final",
               "title": "Mark the window", "instructions": "Box the glass and frame"},
              {"step_id": "sign", "kind": "person", "needs": ["width_mm"], "propose_from": "measure"},
              {"step_id": "free", "kind": "person", "needs": ["count"]},
              {"step_id": "chk", "kind": "check", "expect": ["width_mm"], "of": "measure"}]}


def test_run_recipe_prompt_explains_every_step(b):
    route("GET", "/recipes/window", (200, RECIPE))
    text = run(b, "run_recipe", "window", "p1")
    assert text.startswith('Run the recipe "Window" (v2) in project p1.\nMeasure a window.')
    assert "- purpose: What for? (one of: blinds, glass)" in text and "- note: \n" in text
    assert "1. dome [action: the bridge calls scan_full_dome itself]" in text
    assert "2. Mark the window [person: work out box yourself from scanner_preview of step dome's scan_id" in text
    assert "— Box the glass and frame" in text
    assert "3. sign [person: work out width_mm yourself" in text and "proposes it from step measure;" in text
    assert "4. free [person: work out count yourself, then scanner_propose it" in text
    assert "5. chk [check: the bridge checks width_mm of step measure]" in text


def test_run_recipe_prompt_separates_proposed_steps(b):
    route("GET", "/recipes/window", (200, RECIPE))
    assert "work out width_mm yourself, the bridge proposes it" in run(b, "run_recipe", "window", "p1")


def test_survey_mission_prompt(b):
    text = run(b, "survey_mission", "p1", "Scan the barn")
    assert text.startswith('The person wants: "Scan the barn" (project p1).') and "scanner_ready_to_leave" in text


# --------------------------------------------------------------------- main -----

def test_main_needs_url_and_token(b, monkeypatch):
    ran = []
    monkeypatch.setattr(b.mcp, "run", lambda: ran.append(True))
    monkeypatch.setattr(b, "TOKEN", "")
    with pytest.raises(SystemExit, match="SCANNER_BRIDGE_URL and SCANNER_BRIDGE_TOKEN"):
        b.main()
    monkeypatch.setattr(b, "TOKEN", "agent-tok")
    b.main()
    assert ran == [True]


# ------------------------------------------------------- lists and counting ---

SCHEDULE = "mark,type,width,height,tolerance,wall\nW1,window,3'-0\",4'-0\",1/4 in,North\n"


def test_load_list_sends_the_schedule_or_the_list(b):
    route("PUT", "/projects/p1/list", (200, {"lines": [{"line_id": "W1", "status": "open"}]}))
    out = run(b, "scanner_load_list", "p1", schedule_csv=SCHEDULE, revision="B", title="Level 1")
    assert out["lines"][0]["line_id"] == "W1"
    assert sent()["body"] == SCHEDULE and sent()["ctype"] == "text/csv" and sent()["query"] == "revision=B&title=Level+1"
    run(b, "scanner_load_list", "p1", list_json={"format": "nil-list"})
    assert sent()["body"] == {"format": "nil-list"} and sent()["auth"] == "Bearer agent-tok"
    for kw in ({}, {"schedule_csv": SCHEDULE, "list_json": {}}):
        with pytest.raises(b.ToolError, match="one of them"):
            run(b, "scanner_load_list", "p1", **kw)
    route("PUT", "/projects/p1/list", (400, {"code": "invalid_request", "title": "Invalid", "detail": "row 2 (W1): no width"}))
    with pytest.raises(b.BridgeError, match="row 2 \\(W1\\): no width"):
        run(b, "scanner_load_list", "p1", schedule_csv=SCHEDULE)


def test_count_returns_the_face_pictures_and_the_record_reads(b):
    route("POST", "/projects/p1/count", (200, {"say": "3 openings found", "faces": [{"face_id": "f1"}, {"face_id": "f2"}]}))
    route("GET", "/projects/p1/count/f1/image", (200, PNG, "image/png"))
    out = run(b, "scanner_count", "p1")
    assert json.loads(out[0])["say"] == "3 openings found"
    assert len(out) == 2 and out[1].data == PNG, "f2 has no picture: left out"
    route("GET", "/projects/p1/record", (200, {"format": "nil-list-record", "lines": []}))
    assert run(b, "scanner_record", "p1")["format"] == "nil-list-record"


def test_lists_on_an_older_bridge_say_so(b):
    for method, path in [("PUT", "/projects/p1/list"), ("GET", "/projects/p1/record"), ("POST", "/projects/p1/count")]:
        route(method, path, (404, b""))
    for name, kw in [("scanner_load_list", {"schedule_csv": SCHEDULE}), ("scanner_record", {}), ("scanner_count", {})]:
        with pytest.raises(b.ToolError, match="need bridge 0.19.0 or later"):
            run(b, name, "p1", **kw)
    # A 404 that names what is missing is the bridge's answer, passed on.
    route("GET", "/projects/p1/record", (404, {"code": "not_found", "title": "Not found", "detail": "record (the mission wasn't made from a list)"}))
    with pytest.raises(b.BridgeError, match="wasn't made from a list"):
        run(b, "scanner_record", "p1")


def test_binding_and_matching_are_the_persons(b, monkeypatch):
    route("POST", "/projects/p1/list/bind", (200, {"bound": ["Window W1"]}))
    route("POST", "/projects/p1/count/matches", (200, {"lines": []}))
    with pytest.raises(b.ToolError, match="name the lines"):
        run(b, "scanner_bind_lines", "p1", wall_id="w3")
    with pytest.raises(b.ToolError, match="give the face as wall_id"):
        run(b, "scanner_bind_lines", "p1", wall_id="w3", place="North", from_xy=[0, 0], to_xy=[1, 0])
    with pytest.raises(b.ToolError, match="at least one"):
        run(b, "scanner_confirm_matches", "p1", [])
    # Without the person's token, the person does it in the web UI.
    out = run(b, "scanner_bind_lines", "p1", wall_id="w3", place="North")
    assert out["done"] is False and "bridge web UI" in out["message"] and not Bridge.seen
    monkeypatch.setattr(b, "PERSON_TOKEN", "person-tok")
    out = run(b, "scanner_bind_lines", "p1", wall_id="w3", place="North", ctx=Chat(can_ask=False))
    assert 'Ask the person in the chat: "Bind the lines at North to wall w3?"' in out["message"] and not Bridge.seen
    out = run(b, "scanner_bind_lines", "p1", place="North", from_xy=[0, 0], to_xy=[6, 0], person_said="yes, that wall")
    assert out == {"done": True, "bound": ["Window W1"]}
    assert sent()["auth"] == "Bearer person-tok"
    assert sent()["body"] == {"lines": [], "place": "North", "from": [0, 0], "to": [6, 0], "confirmed_via": "relayed by the agent: \u201cyes, that wall\u201d"}
    out = run(b, "scanner_confirm_matches", "p1", [{"line_id": "W1", "opening_id": "f1-2"}, {"line_id": "W2"}], ctx=Chat(can_ask=False))
    assert '"Confirm the openings: W1 is f1-2; W2: no match?"' in out["message"]
    run(b, "scanner_confirm_matches", "p1", [{"line_id": "W1", "opening_id": "f1-2"}], person_said="yes")
    assert sent()["path"] == "/projects/p1/count/matches" and sent()["auth"] == "Bearer person-tok"
    assert sent()["body"]["matches"] == [{"line_id": "W1", "opening_id": "f1-2"}]
    route("POST", "/projects/p1/list/bind", (404, b""))
    with pytest.raises(b.ToolError, match="need bridge 0.19.0 or later"):
        run(b, "scanner_bind_lines", "p1", lines=["W1"], wall_id="w3", person_said="yes")


@needs_elicitation
def test_binding_and_matching_ask_in_the_chat(b, monkeypatch):
    monkeypatch.setattr(b, "PERSON_TOKEN", "person-tok")
    route("POST", "/projects/p1/list/bind", (200, {"bound": ["Window W1"]}))
    route("POST", "/projects/p1/count/matches", (200, {"lines": []}))
    chat = Chat("cancel", {"confirm": False}, {})
    assert run(b, "scanner_bind_lines", "p1", lines=["W1", "W2"], wall_id="w3", ctx=chat)["action"] == "cancel"
    assert "said no" in run(b, "scanner_bind_lines", "p1", lines=["W1", "W2"], wall_id="w3", ctx=chat)["message"]
    assert not Bridge.seen
    assert run(b, "scanner_bind_lines", "p1", lines=["W1", "W2"], wall_id="w3", ctx=chat)["done"] is True
    assert chat.asked[0] == "Bind lines W1, W2 to wall w3?"
    assert sent()["body"]["confirmed_via"] == "asked in the chat"
    out = run(b, "scanner_confirm_matches", "p1", [{"line_id": "W1", "opening_id": "f1-2"}], ctx=Chat({}))
    assert out["done"] is True and sent()["body"]["confirmed_via"] == "asked in the chat"
