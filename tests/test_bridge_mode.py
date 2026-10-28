"""Bridge-mode tests: the MCP tools against a real x9-bridge and x9-mock.

Skipped unless X9_BRIDGE_BIN points at a directory with the `x9-bridge` and
`x9-mock` binaries (from trimble-x9-bridge: cargo build --release). With
X9_FIXTURES (dome/area .tzf + .jpg, as for the bridge's e2e test) the preview,
aim and export tools are exercised too.

Run: X9_BRIDGE_BIN=…/target/release pytest -q tests/test_bridge_mode.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import types

import pytest

BIN = os.environ.get("X9_BRIDGE_BIN")
FIX = os.environ.get("X9_FIXTURES")
MOCK, BRIDGE = "127.0.0.1:34611", "127.0.0.1:8811"
AGENT, OPERATOR = "agent-token-0123456789abcdef", "operator-token-0123456789abcdef"

pytestmark = pytest.mark.skipif(not BIN, reason="set X9_BRIDGE_BIN to run bridge-mode tests")


def _up(url):
    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


def _operator(method, path, body=None):
    req = urllib.request.Request(f"http://{BRIDGE}/v1{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {OPERATOR}", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read() or b"null")


class _Ctx:
    def __init__(self):
        self.progress = []

    async def report_progress(self, done, total):
        self.progress.append(done)


@pytest.fixture(scope="module")
def tools():
    tmp = tempfile.mkdtemp()
    tok = os.path.join(tmp, "tokens")
    with open(tok, "w") as f:
        f.write(f"claude agent {AGENT}\ncrew operator {OPERATOR}\n")
    recipes = os.path.join(tmp, "recipes")
    os.makedirs(recipes)
    env_m = {**os.environ, "X9_MOCK_ADDR": MOCK, "X9_MOCK_SPEED": "20"}
    if FIX:
        env_m["X9_MOCK_FIXTURES"] = FIX
    procs = [subprocess.Popen([f"{BIN}/x9-mock"], env=env_m, stderr=subprocess.DEVNULL)]
    _up(f"http://{MOCK}/api/v1/status")
    recipe = os.path.join(os.path.dirname(BIN.rstrip("/")), "..", "crates", "x9-bridge", "tests", "recipes", "window-measurement.toml")
    if os.path.exists(recipe):
        with open(recipe) as src, open(os.path.join(recipes, "window-measurement.toml"), "w") as dst:
            dst.write(src.read())
    procs.append(subprocess.Popen([f"{BIN}/x9-bridge"], stderr=subprocess.DEVNULL, env={
        **os.environ, "X9_BASE": f"http://{MOCK}/api/v1", "BRIDGE_ADDR": BRIDGE, "BRIDGE_TOKENS": tok,
        "BRIDGE_POLL_MS": "200", "BRIDGE_DATA": os.path.join(tmp, "data"), "BRIDGE_RECIPES": recipes}))
    _up(f"http://{BRIDGE}/v1/health")
    os.environ.update(SCANNER_BRIDGE_URL=f"http://{BRIDGE}/v1", SCANNER_BRIDGE_TOKEN=AGENT, SCANNER_PERSON_TOKEN=OPERATOR,
                      SCANNER_DOWNLOAD_DIR=os.path.join(tmp, "downloads"))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    for m in ("scanner_bridge", "scanner_mcp"):
        sys.modules.pop(m, None)
    import scanner_bridge
    yield scanner_bridge
    for p in procs:
        p.terminate()


def run(coro):
    return asyncio.run(coro)


class _Person(_Ctx):
    """A chat client that can ask the person: `answers` are what they say, in
    order (a dict of fields = accept, None = decline)."""
    def __init__(self, answers, can_ask=True):
        super().__init__()
        self.answers, self.asked = list(answers), []
        self.session = types.SimpleNamespace(check_client_capability=lambda caps: can_ask)

    async def elicit(self, message, schema):
        self.asked.append(message)
        a = self.answers.pop(0)
        if a is None:
            return types.SimpleNamespace(action=getattr(self, "elicit_action", "decline"), data=None)
        return types.SimpleNamespace(action="accept", data=schema(**a))


def fn(tool):
    return getattr(tool, "fn", tool)


def test_tools_are_listed_without_operator_tools(tools):
    names = {t.name for t in run(tools.mcp.list_tools())}
    assert {"scanner_status", "scanner_start_scan", "scanner_preview", "scanner_aim", "scanner_export",
            "scanner_start_run", "scanner_advance_run", "scanner_propose", "scanner_measure", "scanner_measurement", "scanner_coverage", "scanner_permissions", "scanner_propose_station", "scanner_plan_station", "scanner_set_mission", "scanner_update_targets", "scanner_ready_to_leave", "scanner_registration", "scanner_wall_face", "scanner_processing", "scanner_minimap"} <= names
    assert not any("approve" in n or "grant" in n or "delete" in n for n in names)


def test_scan_waits_for_approval_then_succeeds(tools, monkeypatch):
    # Without a person token (no way to ask anyone), an agents scan waits for an operators approval.
    monkeypatch.setattr(tools, "PERSON_TOKEN", "")
    st = run(fn(tools.scanner_status)())
    assert st["scanner"]["connected"]
    p = run(fn(tools.scanner_create_project)("mcp-bridge-test"))
    job = run(fn(tools.scanner_start_scan)(p["project_id"], "STD_L0", note="station 1"))
    assert job["state"] == "awaiting_approval"
    w = run(fn(tools.scanner_wait_for_job)(_Ctx(), job["job_id"]))
    assert w["waiting_for_approval"] and not w["done"]
    _operator("POST", f"/jobs/{job['job_id']}/approve")
    ctx = _Ctx()
    w = run(fn(tools.scanner_wait_for_job)(ctx, job["job_id"], timeout_s=120))
    assert w["done"] and w["job"]["state"] == "succeeded"
    assert ctx.progress, "progress was reported while scanning"
    scans = run(fn(tools.scanner_scans)(p["project_id"]))
    assert scans[0]["scan_id"] == w["job"]["result"]["scan_id"] and scans[0]["kind"] == "full_dome"
    tools._state = {"project_id": p["project_id"], "station": scans[0]["scan_id"]}


def test_errors_carry_the_bridge_code(tools):
    with pytest.raises(tools.BridgeError, match="invalid_request"):
        run(fn(tools.scanner_start_scan)(tools._state["project_id"], "STD_L9"))
    with pytest.raises(tools.ToolError):
        run(fn(tools.scanner_start_scan)(tools._state["project_id"], "STD_L2", start_ha=1.0))
    with pytest.raises(tools.BridgeError, match="not_found"):  # one escaped path segment
        run(fn(tools.scanner_scan)("../status"))


@pytest.mark.skipif(not FIX, reason="needs X9_FIXTURES")
def test_preview_aim_export(tools):
    station = tools._state["station"]
    out = run(fn(tools.scanner_preview)(station))
    assert "2082 x 815 px" in out[0] and out[1].data[:2] == b"\xff\xd8"
    a = run(fn(tools.scanner_aim)(station, 523, 418, 674, 564, margin_deg=0))
    assert abs(a["window"]["start_ha"] - 5.157144) < 1e-4
    ej = run(fn(tools.scanner_export)(station, "las", "none"))
    w = run(fn(tools.scanner_wait_for_job)(_Ctx(), ej["job_id"], timeout_s=180))
    info = run(fn(tools.scanner_export_info)(w["job"]["result"]["export_id"]))
    assert info["point_count"] == 6281859 and all(c["ok"] for c in info["checks"])
    d = run(fn(tools.scanner_download_export)(info["export_id"]))
    assert d["bytes"] == 227 + 20 * 6281859 and os.path.exists(d["path"])
    cov = run(fn(tools.scanner_coverage)(station))
    c = json.loads(cov[0])
    assert c["point_count"] == 6281859 and c["plan"]["shadow_m2"] > 0 and cov[1].data[1:4] == b"PNG"
    ej = run(fn(tools.scanner_export)(station, "copc", "none"))
    w = run(fn(tools.scanner_wait_for_job)(_Ctx(), ej["job_id"], timeout_s=240))
    d = run(fn(tools.scanner_download_export)(w["job"]["result"]["export_id"]))
    assert d["path"].endswith(".copc.laz") and open(d["path"], "rb").read(4) == b"LASF"


def test_recipe_prompt_and_run(tools):
    recipes = run(fn(tools.scanner_recipes)())
    if not recipes:
        pytest.skip("window recipe not found next to the binaries")
    text = run(fn(tools.run_recipe)("window-measurement", tools._state["project_id"]))
    assert "Mark the window" in text and "scanner_sign_off" in text and "Approve the run" in text
    assert "scanner_advance_run it yourself" in text and "asks the person once" in text
    assert isinstance(run(fn(tools.scanner_permissions)(tools._state["project_id"])), list)
    r = run(fn(tools.scanner_start_run)("window-measurement", tools._state["project_id"], {"purpose": "blinds"}, person_said="yes"))
    for _ in range(100):
        r = run(fn(tools.scanner_run)(r["run_id"]))
        if r["waiting_for"] in ("approval", "input"):
            break
        time.sleep(0.1)
    assert r["waiting_for"] in ("approval", "input")
    r = run(fn(tools.scanner_cancel_run)(r["run_id"]))
    for _ in range(50):
        r = run(fn(tools.scanner_run)(r["run_id"]))
        if r["state"] == "cancelled":
            break
        time.sleep(0.1)
    assert r["state"] == "cancelled"


def test_log_and_note(tools):
    run(fn(tools.scanner_add_note)("Tripod on the rug; slight sway", tools._state["project_id"]))
    log = run(fn(tools.scanner_log)(tools._state["project_id"]))
    assert any(e["text"].startswith("Tripod on the rug") and e["author"] == "claude" for e in log)


def _wait_run(tools, rid, pred, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = run(fn(tools.scanner_run)(rid))
        if pred(r) or r["state"] in ("succeeded", "failed", "cancelled"):
            return r
        time.sleep(0.3)
    return r


def test_permission_is_asked_once_in_the_chat(tools):
    p = run(fn(tools.scanner_create_project)(f"chat-{int(time.time() * 1000)}"))["project_id"]
    no = _Person([{"allow": False}])
    with pytest.raises(Exception, match="did not allow"):
        run(fn(tools.scanner_start_run)("window-measurement", p, {"purpose": "blinds"}, ctx=no))
    assert len(no.asked) == 1 and "next 8 hours" in no.asked[0]
    assert not [g for g in _operator("GET", "/grants") if g["project_id"] == p]
    yes = _Person([{"allow": True}])
    r = run(fn(tools.scanner_start_run)("window-measurement", p, {"purpose": "blinds"}, ctx=yes))
    assert r["permission"] == "given in the chat" and len(yes.asked) == 1
    gs = [g for g in _operator("GET", "/grants") if g["project_id"] == p]
    assert len(gs) == 1 and gs[0]["granted_by"] == "crew" and gs[0]["max_scans"] == 20
    r = _wait_run(tools, r["run_id"], lambda r: r["waiting_for"] in ("approval", "input"), timeout=60)
    assert r["waiting_for"] == "input" and r["interventions"]["approvals"] == 0
    run(fn(tools.scanner_cancel_run)(r["run_id"]))
    again = _Person([])
    r2 = run(fn(tools.scanner_start_run)("window-measurement", p, {"purpose": "blinds"}, ctx=again))
    assert again.asked == [] and r2["permission"] == "already given"
    run(fn(tools.scanner_cancel_run)(r2["run_id"]))
    # A chat that can't ask: the agent is told to ask in the chat, then relays the person's words.
    mute = _Person([], can_ask=False)
    p2 = run(fn(tools.scanner_create_project)(f"chat2-{int(time.time() * 1000)}"))["project_id"]
    with pytest.raises(Exception, match="person_said"):
        run(fn(tools.scanner_start_run)("window-measurement", p2, {"purpose": "blinds"}, ctx=mute))
    r3 = run(fn(tools.scanner_start_run)("window-measurement", p2, {"purpose": "blinds"}, person_said="yes, go ahead", ctx=mute))
    assert r3["permission"] == "relayed from the chat" and mute.asked == []
    g3 = [g for g in _operator("GET", "/grants") if g["project_id"] == p2]
    assert len(g3) == 1 and g3[0]["note"] == "relayed by the agent: \u201cyes, go ahead\u201d"
    run(fn(tools.scanner_cancel_run)(r3["run_id"]))
    # A chat that closes the question unshown: the agent is told to ask itself.
    shut = _Person([None])
    shut.elicit_action = "cancel"
    p4 = run(fn(tools.scanner_create_project)(f"chat4-{int(time.time() * 1000)}"))["project_id"]
    with pytest.raises(Exception, match="action: cancel.*person_said"):
        run(fn(tools.scanner_start_run)("window-measurement", p4, {"purpose": "blinds"}, ctx=shut))


@pytest.mark.skipif(not FIX, reason="needs X9_FIXTURES for the close-up and the measurement")
def test_one_sentence_run_signs_off_in_the_chat(tools):
    p = run(fn(tools.scanner_create_project)(f"one-{int(time.time() * 1000)}"))["project_id"]
    person = _Person([{"allow": True}, {"confirm": True}])
    r = run(fn(tools.scanner_start_run)("window-measurement", p, {"purpose": "blinds"}, ctx=person))
    rid = r["run_id"]
    r = _wait_run(tools, rid, lambda r: r["current_step_id"] == "box" and r["waiting_for"] == "input", timeout=60)
    with pytest.raises(Exception, match="yours"):
        run(fn(tools.scanner_sign_off)(rid, ctx=person))
    run(fn(tools.scanner_advance_run)(rid, "box", {"box": {"x0": 523, "y0": 418, "x1": 674, "y1": 564}}))
    r = _wait_run(tools, rid, lambda r: r["current_step_id"] == "confirm" and r["waiting_for"] == "input")
    assert r["current_step_id"] == "confirm", r
    out = run(fn(tools.scanner_sign_off)(rid, ctx=person, summary="Green fills the window up to the wall."))
    assert out["signed_off"] and out["corrected"] == {}
    assert "opening width mm" in person.asked[1] and "Green fills" in person.asked[1]
    r = _wait_run(tools, rid, lambda r: False, timeout=20)
    assert r["state"] == "succeeded"
    assert r["interventions"]["total"] == 1 and r["interventions"]["confirmed"] == 1
    assert [g["granted_by"] for g in r["authorized_by"]] == ["crew"]


@pytest.mark.skipif(not FIX, reason="needs X9_FIXTURES for the close-up and the measurement")
def test_relayed_sign_off_is_recorded(tools):
    p = run(fn(tools.scanner_create_project)(f"relay-{int(time.time() * 1000)}"))["project_id"]
    mute = _Person([], can_ask=False)
    r = run(fn(tools.scanner_start_run)("window-measurement", p, {"purpose": "blinds"}, person_said="sure", ctx=mute))
    rid = r["run_id"]
    _wait_run(tools, rid, lambda r: r["current_step_id"] == "box" and r["waiting_for"] == "input", timeout=60)
    run(fn(tools.scanner_advance_run)(rid, "box", {"box": {"x0": 523, "y0": 418, "x1": 674, "y1": 564}}))
    r = _wait_run(tools, rid, lambda r: r["current_step_id"] == "confirm" and r["waiting_for"] == "input")
    out = run(fn(tools.scanner_sign_off)(rid, ctx=mute))
    assert out["signed_off"] is False and "person_said" in out["message"]
    with pytest.raises(Exception, match="corrections can only change"):
        run(fn(tools.scanner_sign_off)(rid, person_said="ok", corrections={"box": 1}, ctx=mute))
    out = run(fn(tools.scanner_sign_off)(rid, person_said="looks right, but it's 1190 wide", corrections={"opening_width_mm": 1190}, ctx=mute))
    assert out["signed_off"] and out["relayed"] and out["corrected"] == {"opening_width_mm": 1190}
    r = _wait_run(tools, rid, lambda r: False, timeout=20)
    iv = r["interventions"]
    assert r["state"] == "succeeded" and iv["total"] == 1 and iv["corrected"] == 1 and iv["relayed"] == 1
    assert r["authorized_by"][0]["note"].startswith("relayed by the agent")


def test_a_plain_scan_asks_permission_once_too(tools):
    p = run(fn(tools.scanner_create_project)(f"scan-{int(time.time() * 1000)}"))["project_id"]
    person = _Person([{"allow": True}])
    job = run(fn(tools.scanner_start_scan)(p, "STD_L0", ctx=person))
    assert job["permission"] == "given in the chat" and job["state"] in ("queued", "running") and len(person.asked) == 1
    run(fn(tools.scanner_cancel_job)(job["job_id"]))
    again = _Person([])
    job2 = run(fn(tools.scanner_start_scan)(p, "STD_L0", ctx=again))
    assert job2["permission"] == "already given" and again.asked == []
    run(fn(tools.scanner_cancel_job)(job2["job_id"]))


@pytest.mark.skipif(not FIX, reason="needs X9_FIXTURES for a preview")
def test_propose_station_draws_the_spot(tools):
    station = tools._state["station"]
    out = run(fn(tools.scanner_propose_station)(
        station, "Carry the tripod past the window, by the door, on the rug.", 900, 600, "by the door",
        labels=[{"x": 600, "y": 450, "text": "Window", "kind": "landmark"}],
        boxes=[{"x0": 523, "y0": 418, "x1": 674, "y1": 564, "text": "Seen: the window", "kind": "seen"}]))
    v = json.loads(out[0])
    m = dict(v["marker"])
    # How far out the scan saw the ground there (the tripod is drawn that size).
    assert m.pop("distance_m", 1.0) > 0
    assert m == {"x": 900, "y": 600, "label": "by the door"} and v["proposed_by"] == "claude"
    assert out[1].data[1:4] == b"PNG"
    with pytest.raises(Exception, match="marker"):
        run(fn(tools.scanner_propose_station)(station, "x", 99999, 1))
    with pytest.raises(Exception, match="right"):
        run(fn(tools.scanner_propose_station)(
            station, "Turn right at the window.", 900, 600,
            labels=[{"x": 600, "y": 450, "text": "Window", "kind": "landmark"}]))


@pytest.mark.skipif(not FIX, reason="needs X9_FIXTURES for a full dome")
def test_plan_station_computes_spots_from_the_points(tools):
    station = tools._state["station"]
    p = run(fn(tools.scanner_plan_station)(station))
    assert p["scan_id"] == station and p["preview_size"] == [2082, 815] and p["min_step_m"] >= 2
    for c in p["candidates"]:
        assert c["distance_m"] >= p["min_step_m"] and len(c["preview_px"]) == 2
    assert p["candidates"] or p["reason"]
    sub = run(fn(tools.scanner_plan_station)(station, 523, 674))
    assert sub["subject_px"] == [523, 674]
    with pytest.raises(Exception, match="subject"):
        run(fn(tools.scanner_plan_station)(station, 523, 99999))


def test_mission_ledger_and_sign_off_in_the_chat(tools):
    p = run(fn(tools.scanner_create_project)(f"mission-{int(time.time() * 1000)}"))["project_id"]
    m = run(fn(tools.scanner_set_mission)(p, "Scan the exterior of the house", "exterior",
                                          "Every facade seen, no shadow on the house walls",
                                          [{"target_id": "north", "name": "North facade", "kind": "facade"}]))
    assert m["state"] == "active" and m["targets"][0]["status"] == "open"
    person = _Person([{"allow": True}, {"confirm": True}])
    job = run(fn(tools.scanner_start_scan)(p, "STD_L0", ctx=person))
    w = run(fn(tools.scanner_wait_for_job)(_Ctx(), job["job_id"], timeout_s=120))
    scan = w["job"]["result"]["scan_id"]
    with pytest.raises(Exception, match="still open: North facade"):
        run(fn(tools.scanner_ready_to_leave)(p, "All facades seen.", ctx=person))
    with pytest.raises(Exception, match="names the stations"):
        run(fn(tools.scanner_update_targets)(p, [{"target_id": "north", "status": "covered"}]))
    run(fn(tools.scanner_update_targets)(p, [{"target_id": "north", "status": "covered", "stations": [scan]},
                                             {"target_id": "south", "name": "South facade", "status": "declined", "note": "neighbour's yard, locked"}]))
    out = run(fn(tools.scanner_ready_to_leave)(p, "North seen from station 1; south declined (locked yard).", ctx=person))
    assert out["signed_off"] and out["mission"]["state"] == "signed_off" and out["mission"]["signed_off_by"] == "crew"
    assert out["mission"]["confirmed_via"] == "asked in the chat" and "Ready to leave" in person.asked[-1]
    assert "1 covered, 1 declined" in person.asked[-1]


def test_registration_before_and_after_a_station(tools):
    p = run(fn(tools.scanner_create_project)(f"reg-{int(time.time() * 1000)}"))["project_id"]
    out = run(fn(tools.scanner_registration)(p))
    v = json.loads(out[0])
    assert v["stations"] == [] and "Not registered yet" in v["summary"]
    with pytest.raises(Exception, match="not registered"):
        run(fn(tools.scanner_wall_face)(p, from_xy=[0, 0], to_xy=[5, 0]))


def test_processing_runs_on_the_pi_without_a_worker(tools):
    out = run(fn(tools.scanner_processing)())
    assert out["workers"] == [] and out["runs_on"] == "the Pi"
    assert "x9-bridge worker --bridge" in out["start_worker"]["command"]


def test_minimap_draws_before_any_station(tools):
    p = run(fn(tools.scanner_create_project)(f"map-{int(time.time() * 1000)}"))["project_id"]
    out = run(fn(tools.scanner_minimap)(p))
    assert "Not registered yet" in out[0] and len(out) == 2
