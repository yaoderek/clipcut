"""Demo fetch + simulated metrics. No network."""
import json
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
    path = ROOT / "runs" / PRIMARY_RUN / "metrics.json"
    assert path.is_file(), "demo-setup should write runs/v2_niosh_multi/metrics.json before commit"
    stored = json.loads(path.read_text())
    assert stored["_meta"]["demo"] is True
    assert stored["_meta"]["simulated"] is True
    assert json.loads((ROOT / "runs" / PRIMARY_RUN / "demo.json").read_text())["simulated"] is True


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
    summary = setup(root=tmp_path, runs=(), origin="http://127.0.0.1:9", timeout=1)
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
