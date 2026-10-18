"""Demo fetch + simulated metrics. No network."""
import json
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from clipcut.demo_setup import (
    PRIMARY_RUN,
    channel_for,
    media_missing,
    run_is_visible,
    seed_run,
    setup,
)

ROOT = Path(__file__).resolve().parents[1]


def test_channel_ids_follow_aspect_and_recipe():
    tiktok, cursor = channel_for({"variant_id": "v01", "aspect": "9:16", "channel": "TikTok"}, 0)
    assert tiktok == "tiktok" and cursor == 0
    shorts, cursor = channel_for({"variant_id": "v02", "aspect": "9:16", "recipe": {"channel": "Shorts"}}, cursor)
    assert shorts == "shorts" and cursor == 0
    reels, _cursor = channel_for({"variant_id": "v04", "aspect": "9:16", "channel": "Reels"}, 0)
    assert reels == "reels"
    first, cursor = channel_for({"variant_id": "a", "aspect": "9:16"}, 0)
    second, cursor = channel_for({"variant_id": "b", "aspect": "9:16"}, cursor)
    third, _cursor = channel_for({"variant_id": "c", "aspect": "9:16"}, cursor)
    assert (first, second, third) == ("tiktok", "shorts", "reels")
    assert channel_for({"variant_id": "v13", "aspect": "1:1", "channel": "X"}, 0)[0] == "x"
    assert channel_for({"variant_id": "v15", "aspect": "4:5", "channel": "LinkedIn"}, 0)[0] == "linkedin"
    # Aspect wins when the recipe channel is a different platform.
    assert channel_for({"variant_id": "sq", "aspect": "1:1", "channel": "TikTok"}, 0)[0] == "x"


def test_niosh_variants_use_vertical_x_and_linkedin():
    directory = ROOT / "runs" / PRIMARY_RUN
    cursor = 0
    seen = {}
    for path in sorted(directory.glob("v[0-9][0-9].json")):
        manifest = json.loads(path.read_text())
        channel, cursor = channel_for(manifest, cursor)
        seen[manifest["variant_id"]] = channel
    assert seen["v01"] == "tiktok"
    assert seen["v02"] == "shorts"
    assert seen["v04"] == "reels"
    assert seen["v13"] == "x" and seen["v14"] == "x"
    assert seen["v15"] == "linkedin" and seen["v16"] == "linkedin"
    vertical = {seen[f"v{i:02d}"] for i in range(1, 13)}
    assert vertical == {"tiktok", "shorts", "reels"}


def test_seed_is_simulated_demo_and_idempotent(tmp_path):
    src = ROOT / "runs" / PRIMARY_RUN
    directory = tmp_path / "runs" / PRIMARY_RUN
    directory.mkdir(parents=True)
    for path in src.glob("v[0-9][0-9].json"):
        (directory / path.name).write_text(path.read_text())
    assert seed_run(PRIMARY_RUN, tmp_path) is True
    stored = json.loads((directory / "metrics.json").read_text())
    meta = stored["_meta"]
    assert meta["demo"] is True and meta["simulated"] is True
    assert "simul" in meta["generator"]
    demo = json.loads((directory / "demo.json").read_text())
    assert demo["demo"] is True and demo["simulated"] is True
    rows = {key: row for key, row in stored.items() if key != "_meta"}
    assert set(rows) == {f"v{i:02d}" for i in range(1, 17)}
    for vid, row in rows.items():
        assert row["demo"] is True and row["simulated"] is True
        assert row["channel"] in {"tiktok", "shorts", "reels", "x", "linkedin"}
        assert 1 <= row["shares"] <= row["views"]
        assert 0 <= row["avg_watch_pct"] <= 100
    assert rows["v13"]["channel"] == "x" and rows["v14"]["channel"] == "x"
    assert rows["v15"]["channel"] == "linkedin" and rows["v16"]["channel"] == "linkedin"
    assert {rows[f"v{i:02d}"]["channel"] for i in range(1, 13)} == {"tiktok", "shorts", "reels"}
    assert seed_run(PRIMARY_RUN, tmp_path) is False
    again = json.loads((directory / "metrics.json").read_text())
    assert again["v07"]["views"] == stored["v07"]["views"]


def test_two_run_names_get_different_metrics():
    from clipcut.demo_setup import _metrics_payload

    manifests = [json.loads(path.read_text()) for path in sorted((ROOT / "runs" / PRIMARY_RUN).glob("v[0-9][0-9].json"))]

    def series(run):
        payload = _metrics_payload(manifests, run)
        return [(vid, row["views"], row["avg_watch_pct"], row["shares"]) for vid, row in sorted(payload["metrics"].items())]

    kendra = series("demo_pd_kendra")
    calvin = series("demo_pd_calvin")
    niosh = series(PRIMARY_RUN)
    assert kendra != calvin
    assert kendra != niosh
    assert calvin != niosh
    assert series("demo_pd_calvin") == calvin
    channels = {row["channel"] for row in _metrics_payload(manifests, "demo_pd_kendra")["metrics"].values()}
    assert channels == {row["channel"] for row in _metrics_payload(manifests, PRIMARY_RUN)["metrics"].values()}


def test_seed_rewrites_simulated_metrics_from_another_run(tmp_path):
    src = ROOT / "runs" / PRIMARY_RUN
    directory = tmp_path / "runs" / "demo_pd_calvin"
    directory.mkdir(parents=True)
    for path in src.glob("v[0-9][0-9].json"):
        (directory / path.name).write_text(path.read_text())
    borrowed = json.loads((src.parent / PRIMARY_RUN / "metrics.json").read_text())
    (directory / "metrics.json").write_text(json.dumps(borrowed))
    assert seed_run("demo_pd_calvin", tmp_path) is True
    stored = json.loads((directory / "metrics.json").read_text())
    assert stored["v01"]["views"] != borrowed["v01"]["views"]
    assert stored["_meta"]["demo"] is True and stored["v01"]["demo"] is True
    assert seed_run("demo_pd_calvin", tmp_path) is False


def test_seed_does_not_replace_real_metrics(tmp_path):
    directory = tmp_path / "runs" / "kept"
    directory.mkdir(parents=True)
    (directory / "v01.json").write_text(json.dumps({"variant_id": "v01", "aspect": "9:16", "channel": "TikTok"}))
    (directory / "metrics.json").write_text(json.dumps({
        "_meta": {"simulated": False},
        "v01": {"views": 10, "avg_watch_pct": 40, "shares": 1, "channel": "tiktok"},
    }))
    assert seed_run("kept", tmp_path) is False
    stored = json.loads((directory / "metrics.json").read_text())
    assert stored["v01"]["views"] == 10
    assert "demo" not in stored["v01"]


def test_setup_offline_does_not_raise(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("clipcut.demo_setup._read_url", boom)
    summary = setup(root=tmp_path, origin="http://127.0.0.1:9", timeout=1)
    assert summary["downloaded"] == []
    assert summary["failed"]
    assert summary["newest"] is None


def test_checked_in_preload_metrics_are_demo():
    from clipcut.demo_setup import _metrics_payload

    path = ROOT / "runs" / PRIMARY_RUN / "metrics.json"
    assert path.is_file(), "demo-setup should write runs/v2_niosh_multi/metrics.json before commit"
    stored = json.loads(path.read_text())
    assert stored["_meta"]["demo"] is True
    assert stored["_meta"]["simulated"] is True
    assert json.loads((ROOT / "runs" / PRIMARY_RUN / "demo.json").read_text())["simulated"] is True
    manifests = [json.loads(item.read_text()) for item in sorted((ROOT / "runs" / PRIMARY_RUN).glob("v[0-9][0-9].json"))]
    expected = _metrics_payload(manifests, PRIMARY_RUN)["metrics"]
    assert stored["v01"]["views"] == expected["v01"]["views"]
    assert stored["v01"]["avg_watch_pct"] == expected["v01"]["avg_watch_pct"]
    assert stored["v01"]["channel"] == expected["v01"]["channel"] == "tiktok"


def test_auto_fetch_stays_off_under_pytest(monkeypatch):
    monkeypatch.delenv("CLIPCUT_AUTO_FETCH_DEMO", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    import app
    assert app._demo_auto_fetch_enabled() is False


def test_media_missing_without_files(tmp_path):
    assert media_missing(tmp_path) is True


def test_sample_fallback_covers_extra_clips(tmp_path, monkeypatch):
    calls = []

    def boom(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    def trimmed(samples, spec, _timeout):
        calls.append(spec["file"])
        (samples / spec["file"]).write_bytes(b"x" * 100_000)
        return True

    monkeypatch.setattr("clipcut.demo_setup._read_url", boom)
    monkeypatch.setattr("clipcut.demo_setup._fetch_trimmed", trimmed)
    summary = setup(root=tmp_path, runs=(), origin="http://127.0.0.1:9", timeout=1, render=False)
    assert calls == [
        "talking_head_niosh.mp4",
        "talking_head_kendra.mp4",
        "talking_head_nasa_calvin.mp4",
    ]
    assert summary["downloaded"] == calls
    assert summary["failed"] == []


def test_placeholder_runs_stay_off_the_run_list(tmp_path, monkeypatch):
    hidden = tmp_path / "demo_pd_calvin"
    hidden.mkdir()
    (hidden / "results.json").write_text("{}\n")
    (hidden / "manifest.json").write_text(json.dumps({"reused_from": PRIMARY_RUN, "source": "samples/talking_head_nasa_calvin.mp4"}))
    assert run_is_visible(hidden) is False
    (hidden / ".rendered").write_text("ok\n")
    assert run_is_visible(hidden) is False
    (hidden / "manifest.json").write_text(json.dumps({"source": "samples/talking_head_nasa_calvin.mp4"}))
    assert run_is_visible(hidden) is True
    assert run_is_visible(tmp_path / "v2_niosh_multi") is True

    import app
    runs = tmp_path / "runs"
    keep = runs / "v2_niosh_multi"
    keep.mkdir(parents=True)
    (keep / "results.json").write_text("{}\n")
    clone = runs / "demo_pd_kendra"
    clone.mkdir()
    (clone / "results.json").write_text("{}\n")
    (clone / "manifest.json").write_text(json.dumps({"reused_from": PRIMARY_RUN}))
    monkeypatch.setattr(app, "RUNS", runs)
    assert app.runs() == ["v2_niosh_multi"]


def test_setup_does_not_clone_or_hardlink_niosh(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("clipcut.demo_setup._read_url", boom)
    primary = tmp_path / "runs" / PRIMARY_RUN
    primary.mkdir(parents=True)
    (primary / "v01.json").write_text(json.dumps({"variant_id": "v01", "aspect": "9:16", "channel": "TikTok"}))
    (primary / "v01.mp4").write_bytes(b"v" * 100_000)
    summary = setup(root=tmp_path, origin="http://127.0.0.1:9", timeout=1)
    assert summary["newest"] == PRIMARY_RUN
    assert not (tmp_path / "runs" / "demo_pd_kendra").exists()
    assert not (tmp_path / "runs" / "demo_pd_calvin").exists()
    assert list(primary.glob("*.mp4")) == [primary / "v01.mp4"]


def _fake_proc():
    class Proc:
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.returncode = -9

    return Proc()


def _write_rendered_run(dest, source):
    dest.mkdir(parents=True, exist_ok=True)
    variants = [
        ("v01", "9:16", "TikTok"),
        ("v02", "1:1", "X"),
        ("v03", "4:5", "LinkedIn"),
    ]
    for vid, aspect, channel in variants:
        (dest / f"{vid}.json").write_text(json.dumps({
            "variant_id": vid, "aspect": aspect, "channel": channel,
            "recipe": {"channel": channel, "aspect": aspect},
        }))
        (dest / f"{vid}.mp4").write_bytes(b"v" * 100_000)
    (dest / "manifest.json").write_text(json.dumps({"source": source, "variants": []}))
    (dest / "results.json").write_text(json.dumps({"winner": "v02", "results": []}))


def test_render_skips_a_finished_run_and_publishes_it(tmp_path):
    dest = tmp_path / "runs" / "demo_pd_calvin"
    _write_rendered_run(dest, "samples/talking_head_nasa_calvin.mp4")
    summary = {"present": [], "rendered": [], "failed": []}

    def slot(*_args):
        raise AssertionError("cutter should not start")

    from clipcut.demo_setup import render_extra_runs
    render_extra_runs(tmp_path, summary, start_cut=slot)
    assert "demo_pd_calvin render" in summary["present"]
    assert summary["rendered"] == []
    assert (dest / ".rendered").is_file()
    assert run_is_visible(dest) is True
    stored = json.loads((dest / "metrics.json").read_text())
    assert stored["_meta"]["demo"] is True and stored["v01"]["channel"] == "tiktok"
    assert stored["v02"]["channel"] == "x" and stored["v03"]["channel"] == "linkedin"
    # Kendra's sample is absent, so that one is recorded and not invented.
    assert any(item.startswith("demo_pd_kendra") for item in summary["failed"])


def test_render_cuts_then_seeds_real_manifests(tmp_path):
    sample = tmp_path / "samples" / "talking_head_nasa_calvin.mp4"
    sample.parent.mkdir()
    sample.write_bytes(b"x" * 100_000)
    (tmp_path / "samples" / "talking_head_kendra.mp4").write_bytes(b"x" * 100_000)
    calls = []

    def slot(run, cmd, n):
        calls.append((run, n, cmd))
        out = Path(cmd[cmd.index("--out") + 1])
        _write_rendered_run(out, cmd[cmd.index("cut2") + 1])
        return _fake_proc()

    summary = {"present": [], "rendered": [], "failed": []}
    from clipcut.demo_setup import render_extra_runs
    render_extra_runs(tmp_path, summary, start_cut=slot, variants=8)
    assert [item[0] for item in calls] == ["demo_pd_kendra", "demo_pd_calvin"]
    assert all(item[1] == 8 and "-n" in item[2] and "cut2" in item[2] for item in calls)
    assert summary["rendered"] == ["demo_pd_kendra", "demo_pd_calvin"]
    assert summary["failed"] == []
    for run in ("demo_pd_kendra", "demo_pd_calvin"):
        dest = tmp_path / "runs" / run
        assert run_is_visible(dest) is True
        assert "reused_from" not in json.loads((dest / "manifest.json").read_text())
        assert json.loads((dest / "metrics.json").read_text())["_meta"]["simulated"] is True
    render_extra_runs(tmp_path, {"present": [], "rendered": [], "failed": []}, start_cut=slot)
    assert len(calls) == 2


def test_render_drops_a_niosh_clone_and_honors_a_busy_lock(tmp_path):
    dest = tmp_path / "runs" / "demo_pd_calvin"
    dest.mkdir(parents=True)
    (dest / "manifest.json").write_text(json.dumps({"reused_from": "v2_niosh_multi", "source": "samples/x.mp4"}))
    (dest / "v01.json").write_text(json.dumps({"variant_id": "v01"}))
    (dest / "results.json").write_text("{}\n")
    sample = tmp_path / "samples" / "talking_head_nasa_calvin.mp4"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"x" * 100_000)
    (tmp_path / "samples" / "talking_head_kendra.mp4").write_bytes(b"x" * 100_000)
    seen = {}

    def slot(run, cmd, n):
        out = Path(cmd[cmd.index("--out") + 1])
        seen[run] = (out / "manifest.json").exists()
        if run == "demo_pd_calvin":
            return None
        _write_rendered_run(out, "samples/talking_head_kendra.mp4")
        return _fake_proc()

    summary = {"present": [], "rendered": [], "failed": []}
    from clipcut.demo_setup import render_extra_runs
    render_extra_runs(tmp_path, summary, start_cut=slot)
    assert seen["demo_pd_calvin"] is False
    assert "demo_pd_calvin: cutter busy" in summary["failed"]
    assert not (dest / ".rendered").exists()
    assert run_is_visible(dest) is False
    assert "demo_pd_kendra" in summary["rendered"]


def test_demo_slot_waits_for_the_user_job(monkeypatch):
    import app

    class Alive:
        def poll(self):
            return None

    class Finished:
        def poll(self):
            return 0

    app.JOBS["ui_user"] = {"proc": Alive(), "n": 4}
    started = []

    def fake_popen(cmd, cwd, stdout, stderr):
        started.append(cmd)
        stdout.close()
        return _fake_proc()

    monkeypatch.setattr(app.subprocess, "Popen", fake_popen)

    def release():
        time.sleep(0.3)
        app.JOBS["ui_user"]["proc"] = Finished()

    threading.Thread(target=release).start()
    try:
        proc = app._demo_job_slot("demo_pd_calvin", ["cut"], 8)
        assert proc is not None
        assert started and started[0] == ["cut"]
        assert app.busy_job() in (None, "demo_pd_calvin")
    finally:
        app.JOBS.pop("ui_user", None)
        app.JOBS.pop("demo_pd_calvin", None)
