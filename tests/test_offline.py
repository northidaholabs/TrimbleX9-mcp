"""Offline tests against a stub instrument. Run: pytest -q"""
import asyncio
import http.server
import json
import math
import os
import struct
import sys
import tempfile
import threading
from typing import ClassVar

import pytest

P_UUID = "1a89f533-ea7f-429c-9ba5-54a1e11c6159"
MODES = [{"scanMode": "HS_L1"}, {"scanMode": "STD_L0"}]
DEFAULTS = {"scanMode": "STD_L0", "scanType": "FullDome", "colorMode": "ON", "fastMode": False,
            "projectName": "probe", "projectUUID": P_UUID,
            "startHA": -1, "deltaHA": 0, "startVA": -1, "deltaVA": 0}
STATE = {}


def reset():
    STATE.update(scans=[{"scanUUID": f"s-{i}", "tzf": {"file": f"probe/{i}.tzf"}} for i in range(4)],
                 tasks=[], cancelled=[], fallback=False)


class Stub(http.server.BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.removeprefix("/api/v1/")
        if p == "projects":
            return self._send([{"id": 0, "projectName": "probe", "projectUUID": P_UUID}])
        if p == "scans/modes":
            return self._send(MODES)
        if p == "scans":  # ids are list positions, like the instrument
            return self._send([dict(s, id=i) for i, s in enumerate(STATE["scans"])])
        if p == "tasks":
            return self._send(STATE["tasks"])
        if p == "errors":  # a broken reply: HTTP 200 with a body that is not JSON
            body = b"<html>gateway</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if p == "status":
            return self._send({"batteryLevel": 80, "wifiConfiguration": {
                "SSID": "SCANNER-1", "key": "hunter2-plaintext", "isFactoryKey": True, "wifiBand": "both"},
                "errorList": [{"type": "NoError"}]})
        self._send({}, 404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        echo = dict(DEFAULTS) if STATE["fallback"] else dict(DEFAULTS, **req)
        task = {"id": 7, "taskUUID": "t-7", "state": "idle", "percentage": 0, "scanRequest": echo}
        STATE["tasks"] = [task]
        self._send(task, 202)

    def do_DELETE(self):
        kind, _, ident = self.path.removeprefix("/api/v1/").partition("/")
        if kind == "scans" and int(ident) < len(STATE["scans"]):
            STATE["scans"].pop(int(ident))
            return self._send({})
        if kind == "tasks":
            STATE["cancelled"].append(ident)
            STATE["tasks"] = []
            return self._send({})
        self._send({}, 404)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
DL = tempfile.mkdtemp()
os.environ.update(SCANNER_BASE=f"http://127.0.0.1:{srv.server_address[1]}/api/v1",
                  SCANNER_ALLOW_ACTUATION="1", SCANNER_DOWNLOAD_DIR=DL)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scanner_mcp as m


def call(name, **args):
    """Tool result as plain data (content blocks, a (content, structured) tuple in newer
    1.x releases, or a CallToolResult in 2.x)."""
    r = asyncio.run(m.mcp.call_tool(name, args))
    if hasattr(r, "content"):  # mcp 2.x: a CallToolResult
        r = r.content
    if isinstance(r, tuple):
        return r[1].get("result", r[1]) if isinstance(r[1], dict) else r[1]
    return json.loads(r[0].text) if r else None


def raises(name, **args):
    try:
        call(name, **args)
    except Exception as e:
        return str(e)
    raise AssertionError(f"{name} did not raise")


def test_delete_scan_by_uuid_survives_renumbering():
    reset()
    call("scanner_delete_scan", scan_uuid="s-1")
    call("scanner_delete_scan", scan_uuid="s-2")  # was id 2, now id 1
    assert [s["scanUUID"] for s in STATE["scans"]] == ["s-0", "s-3"]
    assert "no scan" in raises("scanner_delete_scan", scan_uuid="s-1")


def test_launch_validates_locally():
    reset()
    ok = {"project_name": "probe", "project_uuid": P_UUID}
    assert "unknown scan_mode" in raises("scanner_launch_scan", scan_mode="BOGUS", **ok)
    assert "no project" in raises("scanner_launch_scan", project_name="other",
                                  project_uuid=P_UUID, scan_mode="HS_L1")
    assert "delta_va" in raises("scanner_launch_area_scan", scan_mode="HS_L1", start_ha=0,
                                delta_ha=0.2, start_va=3.0, delta_va=0.5, **ok)
    assert STATE["tasks"] == []  # nothing was sent


def test_area_scan_echo_ok():
    reset()
    call("scanner_launch_area_scan", project_name="probe", project_uuid=P_UUID,
         scan_mode="HS_L1", start_ha=-0.1, delta_ha=0.2, start_va=1.47, delta_va=0.2)
    assert STATE["cancelled"] == []


def test_fallback_is_cancelled():
    reset()
    STATE["fallback"] = True  # instrument ignores the body and runs its defaults
    err = raises("scanner_launch_area_scan", project_name="probe", project_uuid=P_UUID,
                 scan_mode="HS_L1", start_ha=0, delta_ha=0.2, start_va=1.47, delta_va=0.2)
    assert "did not accept" in err and STATE["cancelled"] == ["7"]


def _fake_scan(name, rows, cols, step, start_va, start_ha):
    meta = 0x100
    data = bytearray(meta + 0x40)
    struct.pack_into("<Q", data, 0x10, meta)
    struct.pack_into("<4d2I", data, meta + 0x10, step, step, start_va, -start_ha, rows, cols)
    with open(os.path.join(DL, name), "wb") as fh:
        fh.write(data)


def _fake_jpeg(name, w, h):
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    with open(os.path.join(DL, name), "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0\x00\x04\x00\x00" + sof + b"\xff\xd9")


def test_preview_to_angles():
    step = 1.508612e-3
    _fake_scan("dome.tzf", 1631, 4165, step, 0.0, 3.57876)
    _fake_jpeg("dome.jpg", 2082, 815)
    r = call("scanner_preview_to_angles", scan_filename="dome.tzf", preview_filename="dome.jpg",
             x0=590, y0=390, x1=640, y1=440, margin_deg=0)
    sx = 4165 / 2082
    assert math.isclose(r["start_ha"], (3.57876 + 590 * sx * step) % math.tau, abs_tol=1e-5)
    assert math.isclose(r["start_va"], 390 * (1631 / 815) * step, abs_tol=1e-5)
    assert math.isclose(r["delta_ha"], 50 * sx * step, abs_tol=1e-5)
    wrap = call("scanner_preview_to_angles", scan_filename="dome.tzf", preview_filename="dome.jpg",
                x0=2070, y0=0, x1=10, y1=10, margin_deg=0)
    assert math.isclose(wrap["delta_ha"], 22 * sx * step, abs_tol=1e-5)
    assert "not found" in raises("scanner_preview_to_angles", scan_filename="nope.tzf",
                                 preview_filename="dome.jpg", x0=0, y0=0, x1=1, y1=1)


def test_launch_refused_while_busy():
    reset()
    STATE["tasks"] = [{"id": 3, "taskUUID": "other-client", "type": "scan", "state": "running"}]
    err = raises("scanner_launch_area_scan", project_name="probe", project_uuid=P_UUID,
                 scan_mode="HS_L1", start_ha=0, delta_ha=0.2, start_va=1.47, delta_va=0.2)
    assert "busy" in err and "other-client" in err
    assert "busy" in raises("scanner_launch_scan", project_name="probe", project_uuid=P_UUID,
                            scan_mode="HS_L1")
    assert STATE["tasks"][0]["taskUUID"] == "other-client"  # nothing was posted
    assert STATE["cancelled"] == []


def test_delete_refused_while_busy():
    reset()
    STATE["tasks"] = [{"id": 3, "taskUUID": "other-client", "type": "scan", "state": "running"}]
    assert "busy" in raises("scanner_delete_scan", scan_uuid="s-1")
    assert "busy" in raises("scanner_delete_project", project_uuid=P_UUID)
    assert len(STATE["scans"]) == 4  # nothing was deleted


def test_truncated_files_are_readable_errors():
    with open(os.path.join(DL, "short.tzf"), "wb") as fh:
        fh.write(b"\0" * 0x12)  # cut off inside the metadata pointer
    with open(os.path.join(DL, "short.jpg"), "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0\x00")  # cut off inside a segment length
    _fake_scan("ok.tzf", 10, 20, 1e-3, 0.0, 0.0)
    _fake_jpeg("ok.jpg", 20, 10)
    box = {"x0": 0, "y0": 0, "x1": 1, "y1": 1}
    assert "short.tzf is truncated" in raises("scanner_preview_to_angles", scan_filename="short.tzf",
                                              preview_filename="ok.jpg", **box)
    assert "short.jpg is a truncated" in raises("scanner_preview_to_angles", scan_filename="ok.tzf",
                                                preview_filename="short.jpg", **box)


def test_non_json_reply_is_a_readable_error():
    assert "not JSON" in raises("scanner_errors")


def test_default_names_are_plain():
    assert m._default_name("3f2a-77", "tzf") == "3f2a-77.tzf"
    assert m._default_name("../a/b?c d", "tzf") == "a_b_c_d.tzf"
    assert m._default_name("...", "la/s") == "download.las"
    long = m._default_name("x" * 300, "ply")
    assert len(long) == 128 and m._SAFE_NAME.fullmatch(long)


def test_status_masks_wifi_key():
    s = call("scanner_status")
    w = s["wifiConfiguration"]
    assert w["key"] == "***" and w["SSID"] == "SCANNER-1" and w["isFactoryKey"] is True
    assert "hunter2" not in json.dumps(s)


def test_unreachable_instrument_is_a_readable_error(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://127.0.0.1:9/api/v1")
    assert "instrument unreachable" in raises("scanner_status")


# ------------------------------------------- bridge-mode helpers, offline -----

class BridgeStub(http.server.BaseHTTPRequestHandler):
    paths: ClassVar[list] = []

    def _reply(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        BridgeStub.paths.append(self.path)
        if self.path == "/v1/status":
            return self._reply(200, b"not json", "text/plain")
        if self.path.startswith("/v1/jobs/"):
            return self._reply(500, b'["boom"]')
        if self.path.startswith("/v1/exports/") and not self.path.endswith("/file"):
            return self._reply(200, b'{"format": "las"}')
        if self.path.startswith("/v1/scans/") and not self.path.endswith("/tzf"):
            return self._reply(200, b'{"scan_id": "x"}')
        self._reply(200, b"DATA", "application/octet-stream")

    def log_message(self, *a):
        pass


bsrv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BridgeStub)
threading.Thread(target=bsrv.serve_forever, daemon=True).start()
import scanner_bridge as b


def bcall(name, *args):
    return asyncio.run(getattr(getattr(b, name), "fn", getattr(b, name))(*args))


def bridge(monkeypatch):
    monkeypatch.setattr(b, "URL", f"http://127.0.0.1:{bsrv.server_address[1]}/v1")
    monkeypatch.setattr(b, "TOKEN", "t")
    BridgeStub.paths.clear()


def test_bridge_ids_are_escaped(monkeypatch):
    bridge(monkeypatch)
    bcall("scanner_scan", "../status?x#y")
    bcall("scanner_scan", "a/b")
    assert BridgeStub.paths == ["/v1/scans/..%2Fstatus%3Fx%23y", "/v1/scans/a%2Fb"]
    for bad in ("", ".", ".."):
        with pytest.raises(b.ToolError, match="invalid id"):
            bcall("scanner_scan", bad)


def test_bridge_default_download_names_are_sanitised(monkeypatch):
    bridge(monkeypatch)
    monkeypatch.setattr(b.direct, "DOWNLOAD_DIR", os.path.realpath(DL))  # as the module resolves it; macOS temp dirs sit behind a symlink
    d = bcall("scanner_download_export", "exp/1:2")
    assert os.path.basename(d["path"]) == "exp_1_2.las" and d["bytes"] == 4
    assert BridgeStub.paths[-1] == "/v1/exports/exp%2F1%3A2/file"
    d = bcall("scanner_download_scanfile", "../scan 9")
    assert os.path.basename(d["path"]) == "scan_9.tzf"


def test_bridge_odd_replies_are_readable_errors(monkeypatch):
    bridge(monkeypatch)
    with pytest.raises(b.ToolError, match="bridge sent a response that is not JSON"):
        bcall("scanner_status")
    with pytest.raises(b.BridgeError, match=r'HTTP 500: \["boom"\]'):
        bcall("scanner_job", "j1")
