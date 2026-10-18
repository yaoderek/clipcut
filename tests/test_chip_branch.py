"""Edit-chip branches: POST /api/branch and GET /api/branch/{id}.

FFmpeg is mocked for the contract tests. One test renders a synthetic clip for real.
"""
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
from clipcut import chip_branch as cb

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def quiet_workers():
    cb.reset_for_tests()
    cb.wait_idle(8)
    yield
    cb.wait_idle(40)
    cb.reset_for_tests()


def _client(monkeypatch, runs):
    monkeypatch.setattr(app_module, "RUNS", runs)
    monkeypatch.setattr(app_module, "ROOT", runs.parent)
    return TestClient(app_module.app)


def _run_dir(runs, name="demo"):
    run = runs / name
    run.mkdir(parents=True, exist_ok=True)
    return run


def _fake_source(run, name="src.mp4"):
    path = run / name
    path.write_bytes(b"fake-mp4")
    (run / "manifest.json").write_text(json.dumps({"source": name}))
    return path


def _synth(path, seconds=4, size="640x360"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(path),
        ],
        check=True,
    )


def _poll(client, branch_id, timeout=30):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(f"/api/branch/{branch_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] in ("done", "error"):
            return last
        time.sleep(0.05)
    raise AssertionError(last)


def _finish(job):
    """Mark a queued chip done without ffmpeg. Used by the pool and API tests."""
    state = cb._read_id(job["id"], Path(job["runs_root"]))
    if state is None:
        return
    out = cb._branch_dir(state, Path(job["runs_root"]))
    out.mkdir(parents=True, exist_ok=True)
    if job["stage"] == "preview":
        (out / "thumb.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        (out / "preview.mp4").write_bytes(b"preview")
        state["thumb_ready"] = True
        state["status"] = "preview"
        state["timing"] = {"preview_s": 0.01}
    else:
        (out / "video.mp4").write_bytes(b"video-full")
        state["status"] = "done"
        timing = dict(state.get("timing") or {})
        timing["full_s"] = 0.02
        state["timing"] = timing
    cb._write_state(state, Path(job["runs_root"]))


def test_chip_params_are_normalized():
    assert cb._normalize_chip({"type": "captions", "params": {}})["params"] == {"style": "bold"}
    assert cb._normalize_chip({"type": "length", "params": {"seconds": "15"}})["params"] == {"seconds": 15}
    assert cb._normalize_chip({"type": "zoom", "params": {"punch_ins": False, "intensity": 2}})["params"]["punch_ins"] is False
    assert cb._normalize_chip({"type": "music", "params": {}})["params"] == {"track": "upbeat"}
    with pytest.raises(cb.ChipError):
        cb._normalize_chip({"type": "crop", "params": {"aspect": "4:5"}})
    with pytest.raises(cb.ChipError):
        cb._normalize_chip({"type": "length", "params": {"seconds": 20}})
    with pytest.raises(cb.ChipError):
        cb._normalize_chip({"type": "length", "params": {"seconds": True}})
    with pytest.raises(cb.ChipError):
        cb._normalize_chip({"type": "zoom", "params": {"intensity": 9}})
    with pytest.raises(cb.ChipError):
        cb._normalize_chip({"type": "nope"})


def test_hook_text_request_persona_and_template():
    text, source = cb.resolve_hook({"text": "  Ship it now  "}, {"variant_id": "v01"})
    assert (text, source) == ("Ship it now", "request")
    text, source = cb.resolve_hook({}, {"variant_id": "v01"})
    assert source == "template"
    assert "Wait" in text
    text, source = cb.resolve_hook({}, {"variant_id": "v01", "hook_text": "She really helped me with statistics"})
    assert source == "persona-hooks"
    assert text.strip()


def test_simulated_score_stacks_and_clamps():
    one = cb.simulated_score([{"type": "crop", "params": {"aspect": "9:16"}}])
    two = cb.simulated_score([
        {"type": "crop", "params": {"aspect": "9:16"}},
        {"type": "captions", "params": {"style": "karaoke"}, "text": "hello"},
    ])
    assert two > one
    huge = [{"type": "captions", "params": {"style": "karaoke"}}] * 20
    assert cb.simulated_score(huge) == 1.0
    assert 0 <= one <= 1


def test_score_falls_back_to_simulated(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("score down")
    monkeypatch.setattr("clipcut.persona_api.score_variants", boom)
    score = cb.make_score([{"type": "music", "params": {"track": "none"}}], "c_aabbccdd", 30, "")
    assert score["source"] == "simulated"
    assert score["value"] == cb.simulated_score([{"type": "music", "params": {"track": "none"}}])


def test_commands_cover_each_chip(tmp_path):
    info = {"duration": 20, "width": 1280, "height": 720, "has_audio": True}
    src = tmp_path / "in.mp4"

    def plan(chip, stage):
        state = {"chip": chip, "stack": [dict(chip, text="Wait this changes the cut")], "_src": str(src)}
        return cb.commands_for(state, info, stage, tmp_path / stage / chip["type"])

    captions = plan({"type": "captions", "params": {"style": "karaoke"}}, "preview")
    blob = " ".join(captions[-1]["args"])
    assert "ultrafast" in blob
    assert "ass=" in blob
    ass = (tmp_path / "preview" / "captions" / "captions.ass").read_text()
    assert "\\kf" in ass

    zoom = plan({"type": "zoom", "params": {"punch_ins": True, "intensity": 1}}, "preview")
    assert "lt(mod(t" in " ".join(zoom[-1]["args"])

    length = plan({"type": "length", "params": {"seconds": 15}}, "full")
    args = length[-1]["args"]
    assert args[args.index("-t") + 1] == "15.000"
    assert "copy" in args
    assert length[-1]["fallback"]

    crop = plan({"type": "crop", "params": {"aspect": "9:16"}}, "full")
    crop_blob = " ".join(crop[-1]["args"])
    assert "crop=720:1280" in crop_blob
    assert "veryfast" in crop_blob

    music = plan({"type": "music", "params": {"track": "upbeat"}}, "preview")
    assert "aevalsrc=" in music[0]["args"][music[0]["args"].index("-i") + 1]
    assert "amix" in " ".join(music[1]["args"])
    chill = plan({"type": "music", "params": {"track": "chill"}}, "full")
    assert "sine=frequency=174" in chill[0]["args"][chill[0]["args"].index("-i") + 1]

    none = plan({"type": "music", "params": {"track": "none"}}, "full")
    assert len(none) == 1
    assert "lavfi" not in none[0]["args"]
    assert "copy" in none[0]["args"]

    hook = plan({"type": "hook", "params": {"text": "Stop scrolling"}}, "thumb")
    assert hook[-1]["args"][hook[-1]["args"].index("-frames:v") + 1] == "1"
    assert "ass=" in " ".join(hook[-1]["args"])


def test_post_returns_immediately_and_get_reaches_done(tmp_path, monkeypatch, quiet_workers):
    runs = tmp_path / "runs"
    run = _run_dir(runs)
    _fake_source(run)
    monkeypatch.setattr(cb, "run_stage", _finish)
    client = _client(monkeypatch, runs)
    started = time.perf_counter()
    response = client.post("/api/branch", json={
        "run": "demo",
        "parent_variant_id": "source",
        "chip": {"type": "crop", "params": {"aspect": "1:1"}},
    })
    assert time.perf_counter() - started < 1.5
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"branch_id", "status_url", "preview_url"}
    assert body["status_url"] == f"/api/branch/{body['branch_id']}"
    assert body["preview_url"].endswith("/preview.mp4")
    queued = client.get(body["status_url"])
    assert queued.status_code == 200
    assert queued.json()["status"] in ("queued", "preview", "done")
    done = _poll(client, body["branch_id"])
    assert done["status"] == "done"
    assert done["video_url"].endswith("/video.mp4")
    assert done["thumb_url"].endswith("/thumb.jpg")
    assert done["score"]["source"] in ("heuristic", "simulated")
    assert 0 <= done["score"]["value"] <= 1
    assert done["chip"] == {"type": "crop", "params": {"aspect": "1:1"}}
    assert done["stack"][-1]["type"] == "crop"
    assert done["error"] is None
    branch_dir = runs / "demo" / "branches" / body["branch_id"]
    assert (branch_dir / "preview.mp4").is_file()
    assert (branch_dir / "video.mp4").is_file()
    assert (branch_dir / "thumb.jpg").is_file()


def test_errors_and_legacy_branch_routes_stay(tmp_path, monkeypatch, quiet_workers):
    runs = tmp_path / "runs"
    run = _run_dir(runs)
    _fake_source(run)
    (run / "v02.json").write_text("{}")
    monkeypatch.setattr(cb, "run_stage", _finish)
    client = _client(monkeypatch, runs)
    missing = client.post("/api/branch", json={"run": "nope", "parent_variant_id": "source", "chip": {"type": "crop", "params": {}}})
    assert missing.status_code == 404
    bad = client.post("/api/branch", json={"run": "demo", "parent_variant_id": "source", "chip": {"type": "crop", "params": {"aspect": "4:5"}}})
    assert bad.status_code == 400
    assert "detail" in bad.json()
    orphan = client.post("/api/branch", json={"run": "demo", "parent_variant_id": "v02", "chip": {"type": "hook", "params": {}}})
    assert orphan.status_code == 400
    unknown = client.get("/api/branch/c_deadbeef")
    assert unknown.status_code == 404
    legacy = client.get("/api/branch/status")
    assert legacy.status_code == 200
    assert "items" in legacy.json()
    queue = client.post("/api/branch/queue", json={})
    assert queue.status_code == 400
    assert "branches" in queue.json()["detail"]


def test_chip_pool_ignores_the_one_job_lock(tmp_path, monkeypatch, quiet_workers):
    runs = tmp_path / "runs"
    run = _run_dir(runs)
    _fake_source(run)

    def explode():
        raise AssertionError("chip render consulted the one-job lock")

    current = {"n": 0, "max": 0}
    lock = threading.Lock()

    def slow(job):
        with lock:
            current["n"] += 1
            current["max"] = max(current["max"], current["n"])
        time.sleep(0.35)
        with lock:
            current["n"] -= 1
        if job["stage"] == "preview":
            state = cb._read_id(job["id"], Path(job["runs_root"]))
            state["status"] = "done"
            cb._write_state(state, Path(job["runs_root"]))

    monkeypatch.setattr(app_module, "busy_job", explode)
    monkeypatch.setattr(cb, "run_stage", slow)
    client = _client(monkeypatch, runs)
    ids = []
    for _ in range(4):
        response = client.post("/api/branch", json={
            "run": "demo", "parent_variant_id": "source",
            "chip": {"type": "length", "params": {"seconds": 30}},
        })
        assert response.status_code == 200
        ids.append(response.json()["branch_id"])
    cb.wait_idle(8)
    assert current["max"] <= cb.WORKERS
    assert current["max"] >= 2
    assert cb.WORKERS in (2, 3)


def test_stacking_appends_the_parent_chip(tmp_path, monkeypatch, quiet_workers):
    runs = tmp_path / "runs"
    run = _run_dir(runs)
    _fake_source(run)
    monkeypatch.setattr(cb, "run_stage", _finish)
    client = _client(monkeypatch, runs)
    first = client.post("/api/branch", json={
        "run": "demo", "parent_variant_id": "source",
        "chip": {"type": "crop", "params": {"aspect": "9:16"}},
    }).json()
    second = client.post("/api/branch", json={
        "run": "demo", "parent_variant_id": first["branch_id"],
        "chip": {"type": "captions", "params": {"style": "bold"}},
    }).json()
    third = client.post("/api/branch", json={
        "run": "demo", "parent_variant_id": second["branch_id"],
        "chip": {"type": "length", "params": {"seconds": 15}},
    }).json()
    done = [_poll(client, item["branch_id"]) for item in (first, second, third)]
    assert [row["status"] for row in done] == ["done", "done", "done"]
    kinds = [chip["type"] for chip in done[2]["stack"]]
    assert kinds == ["crop", "captions", "length"]
    assert done[2]["parent_variant_id"] == second["branch_id"]
    assert done[1]["stack"][1]["text"]


def test_mocked_ffmpeg_writes_preview_then_full(tmp_path, monkeypatch, quiet_workers):
    runs = tmp_path / "runs"
    run = _run_dir(runs)
    _fake_source(run)
    calls = []

    def fake_ff(args, timeout=120):
        calls.append(list(args))
        Path(args[-1]).write_bytes(b"x" * 48)

    monkeypatch.setattr(cb, "ffmpeg_run", fake_ff)
    monkeypatch.setattr(cb, "probe_media", lambda path: {"duration": 12, "width": 1080, "height": 1920, "has_audio": False})
    client = _client(monkeypatch, runs)
    created = client.post("/api/branch", json={
        "run": "demo", "parent_variant_id": "source",
        "chip": {"type": "zoom", "params": {"punch_ins": True, "intensity": 1.5}},
    })
    assert created.status_code == 200
    done = _poll(client, created.json()["branch_id"])
    assert done["status"] == "done", done
    joined = [" ".join(map(str, args)) for args in calls]
    assert any("ultrafast" in blob for blob in joined)
    assert any("veryfast" in blob for blob in joined)
    assert any("-frames:v" in blob for blob in joined)
    assert done["timing"]["preview_s"] >= 0
    assert done["timing"]["full_s"] >= 0
    assert done["score"]["source"] == "heuristic"


def test_real_preview_and_full_render(tmp_path, monkeypatch, quiet_workers):
    runs = tmp_path / "runs"
    run = _run_dir(runs)
    _synth(run / "src.mp4", seconds=4, size="640x360")
    (run / "manifest.json").write_text(json.dumps({"source": "src.mp4", "hook_text": "Ship the demo"}))
    (run / "v01.json").write_text(json.dumps({"variant_id": "v01", "hook_text": "Ship the demo", "duration_s": 4}))
    client = _client(monkeypatch, runs)
    started = time.perf_counter()
    crop = client.post("/api/branch", json={
        "run": "demo", "parent_variant_id": "source",
        "chip": {"type": "crop", "params": {"aspect": "9:16"}},
    })
    assert crop.status_code == 200
    assert time.perf_counter() - started < 1.5
    crop_id = crop.json()["branch_id"]
    music = client.post("/api/branch", json={
        "run": "demo", "parent_variant_id": crop_id,
        "chip": {"type": "music", "params": {"track": "upbeat"}},
    })
    captions = client.post("/api/branch", json={
        "run": "demo", "parent_variant_id": "source",
        "chip": {"type": "captions", "params": {"style": "bold"}},
    })
    length = client.post("/api/branch", json={
        "run": "demo", "parent_variant_id": "source",
        "chip": {"type": "length", "params": {"seconds": 15}},
    })
    zoom = client.post("/api/branch", json={
        "run": "demo", "parent_variant_id": "source",
        "chip": {"type": "zoom", "params": {"punch_ins": True, "intensity": 1}},
    })
    assert music.status_code == captions.status_code == length.status_code == zoom.status_code == 200
    results = {
        "crop": _poll(client, crop_id, timeout=40),
        "music": _poll(client, music.json()["branch_id"], timeout=40),
        "captions": _poll(client, captions.json()["branch_id"], timeout=40),
        "length": _poll(client, length.json()["branch_id"], timeout=40),
        "zoom": _poll(client, zoom.json()["branch_id"], timeout=40),
    }
    for name, row in results.items():
        assert row["status"] == "done", (name, row.get("error"), row.get("timing"))
        assert row["timing"]["preview_s"] < 15, (name, row["timing"])
        assert row["timing"]["full_s"] < 20, (name, row["timing"])
        video = runs / "demo" / "branches" / row["branch_id"] / "video.mp4"
        preview = runs / "demo" / "branches" / row["branch_id"] / "preview.mp4"
        assert video.stat().st_size > 500
        assert preview.stat().st_size > 200
    assert results["music"]["music_placeholder"] is True
    assert "placeholder" in results["music"]["music_label"]
    assert [chip["type"] for chip in results["music"]["stack"]] == ["crop", "music"]
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0",
         str(runs / "demo" / "branches" / results["music"]["branch_id"] / "video.mp4")],
        capture_output=True, text=True, check=True,
    )
    width, height = [int(x) for x in probe.stdout.strip().split(",")]
    assert (width, height) == (720, 1280)
    audio = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
         str(runs / "demo" / "branches" / results["music"]["branch_id"] / "video.mp4")],
        capture_output=True, text=True, check=True,
    )
    assert "audio" in audio.stdout
    length_probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(runs / "demo" / "branches" / results["length"]["branch_id"] / "video.mp4")],
        capture_output=True, text=True, check=True,
    )
    assert float(length_probe.stdout) < 5
    print("CHIP_TIMES", json.dumps({name: row["timing"] for name, row in results.items()}))


def test_canvas_drop_payload_chains_and_aliases(tmp_path, monkeypatch, quiet_workers):
    runs = tmp_path / "runs"
    run = _run_dir(runs)
    _fake_source(run)
    (run / "v01.mp4").write_bytes(b"variant-bytes")
    (run / "v01.json").write_text(json.dumps({"variant_id": "v01", "hook_text": "Ship the demo"}))
    monkeypatch.setattr(cb, "run_stage", _finish)
    client = _client(monkeypatch, runs)
    response = client.post("/api/branch", json={
        "run": "demo",
        "variant_id": "v01",
        "parent": "src",
        "chips": ["len15", "zoom", "captions"],
        "video": "/runs/demo/v01.mp4",
        "settings": {"length": 15, "captions": "word_pop", "aspect": "9:16", "pacing": "tight_punch"},
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["video"] == "/runs/demo/v01.mp4"
    assert body["status"] == "queued"
    assert body["id"] == body["branch_id"]
    done = _poll(client, body["branch_id"], timeout=12)
    assert done["status"] == "done", done
    assert [chip["type"] for chip in done["stack"]] == ["length", "zoom", "captions"]
    assert done["video"] == done["video_url"]
    assert done["poster"] == done["thumb_url"]
    assert done["source"] == done["score"]["source"]
    again = client.post("/api/branch", json={
        "run": "demo",
        "variant_id": "v01",
        "parent": "x_2",
        "chips": ["len15", "zoom", "captions", "music"],
        "video": done["video_url"],
        "settings": {"track": "chill"},
    })
    assert again.status_code == 200, again.text
    stacked = _poll(client, again.json()["branch_id"], timeout=12)
    assert stacked["status"] == "done", stacked
    assert [chip["type"] for chip in stacked["stack"]] == ["length", "zoom", "captions", "music"]
    assert stacked["music_placeholder"] is True
    assert stacked["parent_variant_id"] == body["branch_id"]
