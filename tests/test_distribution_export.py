"""Distribution export: specs, captions, copy, variant selection, bundles, routes. No mp4 decode, no ffmpeg, no network."""
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from clipcut.distribution import CHANNELS, DistributionError, build_zip, export_run, generate_copy, load_export, to_srt, to_vtt
from clipcut.distribution import bundle as B
from clipcut.distribution import render as R
from clipcut.distribution.copygen import fit

LONG = "She really helped me with statistics, but more than that, she said, look you need to be a research assistant"


def _m(vid, channel, aspect, hook="m1", duration=30.0, hook_text="Did you know most people get this wrong?", captions="word_pop"):
    return {
        "variant_id": vid, "channel": channel, "aspect": aspect, "duration_s": duration, "hook_text": hook_text,
        "hook_end_s": 2.0, "cut_times_s": [2, 4, 6, 9, 12], "loop_end": True, "silence_removed_s": 1.0,
        "captions": [{"start": 0.0, "end": 1.4, "text": "DID YOU KNOW"}, {"start": 1.35, "end": 3.0, "text": "MOST PEOPLE GET"},
                     {"start": 3.0, "end": 4.2, "text": "THIS WRONG"}, {"start": 200.0, "end": 201.0, "text": "LATE CUE"}],
        "recipe": {"hook": hook, "captions": captions, "channel": channel, "aspect": aspect, "ending": "loop", "hook_title": True},
    }


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)


@pytest.fixture
def run(tmp_path):
    d = tmp_path / "r1"
    d.mkdir()
    ms = [_m("v01", "TikTok", "9:16", "m1", 20), _m("v02", "Reels", "9:16", "m2", 40), _m("v03", "Shorts", "9:16", "m1", 200),
          _m("v04", "X", "1:1", "m1", 25), _m("v05", "LinkedIn", "4:5", "m2", 50)]
    for m in ms:
        (d / f"{m['variant_id']}.json").write_text(json.dumps(m))
    (d / "results.json").write_text(json.dumps({"results": [{"variant_id": v} for v in ["v02", "v01", "v04", "v03", "v05"]]}))
    return tmp_path


class FakeLLM:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def complete(self, prompt):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_channel_ids_match_persona_api():
    assert list(CHANNELS) == ["tiktok", "shorts", "reels", "x", "linkedin"]
    assert CHANNELS["shorts"]["title_max"] == 100 and CHANNELS["x"]["caption_max"] == 280
    assert CHANNELS["linkedin"]["aspects"] == ["1:1", "4:5"]


def test_srt_and_vtt_from_manifest_captions():
    caps = _m("v01", "TikTok", "9:16")["captions"]
    srt = to_srt(caps, duration=30)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,350\nDID YOU KNOW\n")
    assert "LATE CUE" not in srt  # past the clip end
    assert srt.count("-->") == 3
    vtt = to_vtt(caps, duration=30)
    assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.350\nDID YOU KNOW")
    assert to_srt([], 10) == "" and to_vtt(None) == "WEBVTT\n\n"


def test_fit_respects_limit_on_word_boundary():
    out = fit("word " * 100, 50)
    assert len(out) <= 50 and out.endswith("…")
    assert fit("short", 50) == "short"


@pytest.mark.parametrize("cid", list(CHANNELS))
def test_template_copy_respects_limits(cid):
    m = _m("v01", "TikTok", "9:16", hook_text=LONG + " " + LONG)
    m["captions"] = [{"start": i, "end": i + 1, "text": "RESEARCH STATISTICS ASSISTANT " * 20} for i in range(30)]
    c = generate_copy(m, cid, mode="auto")
    s = CHANNELS[cid]
    assert c["source"] == "template" and c["model_version"] == "dist-copy-template-v1"
    assert len(c["caption"]) <= s["caption_max"]
    if s["title_max"]:
        assert c["title"] and len(c["title"]) <= s["title_max"]
    else:
        assert c["title"] is None
    assert len(c["hashtags"]) <= s["hashtags_max"]
    if cid == "shorts":
        assert "#Shorts" in c["hashtags"]


def test_template_copy_seeded_from_hook_text():
    c = generate_copy(_m("v01", "TikTok", "9:16"), "x", mode="template")
    assert "wrong" in c["caption"].lower()
    assert c["seed"] == "hook_rewrite" and c["title_card"]


def test_llm_copy_used_and_clamped():
    llm = FakeLLM(json.dumps({"title": "T" * 300, "caption": "Grok wrote this " * 40, "hashtags": ["#research", "stats", "x y"]}))
    c = generate_copy(_m("v01", "TikTok", "9:16"), "shorts", mode="auto", client=llm)
    assert c["source"] == "llm" and c["model_version"] == "dist-copy-llm-v1"
    assert len(c["title"]) <= 100 and c["caption"].startswith("Grok wrote this")
    assert c["hashtags"][0] == "#Shorts" and "#research" in c["hashtags"]
    x = generate_copy(_m("v01", "TikTok", "9:16"), "x", mode="auto", client=llm)
    assert len(x["caption"]) <= 280
    assert "Do not invent facts" in llm.prompts[0]


@pytest.mark.parametrize("reply", ["not json", json.dumps({"caption": ""}), RuntimeError("boom")])
def test_llm_failure_falls_back_to_template(reply):
    c = generate_copy(_m("v01", "TikTok", "9:16"), "tiktok", mode="auto", client=FakeLLM(reply))
    assert c["source"] == "template" and "note" in c


def test_llm_mode_without_key_is_503(run):
    with pytest.raises(DistributionError) as e:
        export_run("r1", mode="llm", root=run)
    assert e.value.status == 503


def test_selection_without_variant(run):
    doc = export_run("r1", root=run, mode="template")
    sel = {c["channel"]: (c["variant_id"], c["selection"]) for c in doc["channels"]}
    assert sel == {"tiktok": ("v01", "channel_target"), "shorts": ("v03", "channel_target"), "reels": ("v02", "channel_target"),
                   "x": ("v04", "channel_target"), "linkedin": ("v05", "channel_target")}
    shorts = next(c for c in doc["channels"] if c["channel"] == "shorts")
    assert shorts["trimmed"] and shorts["duration_s"] == 180


def test_selection_with_variant_uses_sibling_or_reframe(run):
    doc = export_run("r1", variant_id="v01", root=run, mode="template")
    sel = {c["channel"]: (c["variant_id"], c["selection"], c["needs_reframe"]) for c in doc["channels"]}
    assert sel["tiktok"] == ("v01", "requested", False)
    assert sel["x"] == ("v04", "sibling_aspect", False)  # same hook m1, 1:1
    assert sel["linkedin"] == ("v04", "sibling_aspect", False)  # LinkedIn takes 1:1 too
    doc = export_run("r1", variant_id="v02", root=run, mode="template")
    sel = {c["channel"]: (c["variant_id"], c["selection"], c["needs_reframe"]) for c in doc["channels"]}
    assert sel["linkedin"] == ("v05", "sibling_aspect", False)  # same hook m2, 4:5
    assert sel["x"] == ("v02", "requested_reframe", True)  # no m2 cut in 1:1 / 16:9
    x = next(c for c in doc["channels"] if c["channel"] == "x")
    assert x["target_aspect"] == "1:1" and x["aspect"] == "9:16"  # closest accepted aspect, least crop


def test_missing_mp4_degrades_cleanly(run):
    doc = export_run("r1", variant_id="v01", root=run, mode="template")
    for c in doc["channels"]:
        assert c["video"]["status"] == "missing" and c["video"]["url"] is None and not c["ready"]
        assert c["captions"]["srt_url"].endswith("/captions.srt") and c["copy"]["caption"]
    out = run / "r1" / "distribution" / doc["export_id"]
    assert (out / "x" / "captions.srt").read_text().startswith("1\n")
    assert (out / "x" / "post.txt").read_text().startswith("CAPTION:")


def test_mp4_copied_when_fits_and_reframe_unavailable_without_ffmpeg(run, monkeypatch):
    (run / "r1" / "v02.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    monkeypatch.setattr(R, "ffmpeg_available", lambda: False)
    doc = export_run("r1", variant_id="v02", channels=["tiktok", "x"], root=run, mode="template")
    tk, li = doc["channels"]
    assert tk["video"]["status"] == "copied" and tk["ready"] and tk["video"]["url"].endswith("/tiktok/tiktok_v02.mp4")
    assert li["video"]["status"] == "unavailable" and "ffmpeg" in li["video"]["note"] and not li["ready"]
    nr = export_run("r1", variant_id="v02", channels=["x"], root=run, mode="template", render=False)
    assert nr["channels"][0]["video"]["status"] == "skipped"


def test_reframe_filter_shapes():
    assert R.reframe_filter("9:16", "1:1", 1080, 1080).startswith("crop=iw:")
    assert R.reframe_filter("9:16", "4:5", 1080, 1350).startswith("crop=iw:")
    assert "boxblur" in R.reframe_filter("9:16", "16:9", 1280, 720)
    assert "boxblur" in R.reframe_filter("16:9", "9:16", 1080, 1920)


def test_bad_inputs(run):
    for kwargs, status in [(dict(run="../x"), 400), (dict(run="nope"), 404), (dict(run="r1", channels=["myspace"]), 400),
                           (dict(run="r1", channels=[]), 400), (dict(run="r1", variant_id="v99"), 404), (dict(run="r1", mode="x"), 400)]:
        with pytest.raises(DistributionError) as e:
            export_run(root=run, **kwargs)
        assert e.value.status == status, kwargs


def test_load_and_zip(run):
    doc = export_run("r1", root=run, mode="template")
    again = load_export("r1", root=run)
    assert again["export_id"] == doc["export_id"] and again["exports"] == [doc["export_id"]]
    z = zipfile.ZipFile(build_zip("r1", doc["export_id"], root=run))
    names = z.namelist()
    prefix = f"r1_{doc['export_id']}"
    assert f"{prefix}/export.json" in names and f"{prefix}/tiktok/captions.srt" in names and f"{prefix}/x/post.txt" in names
    one = zipfile.ZipFile(build_zip("r1", doc["export_id"], "x", root=run)).namelist()
    assert not any("/tiktok/" in n for n in one)
    with pytest.raises(DistributionError):
        build_zip("r1", "ex_../../etc", root=run)
    with pytest.raises(DistributionError) as e:
        load_export("r1", root=run, export_id="ex_20200101_000000_abcd")
    assert e.value.status == 404


def test_http_routes(run):
    import app as appmod
    appmod.app.state.runs_root = run
    try:
        c = TestClient(appmod.app)
        assert c.get("/api/distribution/export/r1").status_code == 404
        r = c.post("/api/distribution/export", json={"run": "r1", "variant_id": "v01", "channels": ["tiktok", "x"], "mode": "template"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert [x["channel"] for x in body["channels"]] == ["tiktok", "x"] and body["copy_source"] == "template"
        assert c.get("/api/distribution/export/r1").json()["export_id"] == body["export_id"]
        z = c.get(body["bundle_url"])
        assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
        assert c.post("/api/distribution/export", json={"run": "r1", "channels": ["bad"]}).json()["error"].startswith("unknown channels")
        assert c.post("/api/distribution/export", content=b"[1]", headers={"content-type": "application/json"}).status_code == 400
        assert len(c.get("/api/distribution/channels").json()["channels"]) == 5
    finally:
        del appmod.app.state.runs_root
