"""Publish: demo states, publish.json, and the YouTube resumable upload with mocked HTTP. No network, no ffmpeg."""
import json

import pytest
from fastapi.testclient import TestClient

from clipcut.distribution import DistributionError, export_run
from clipcut.distribution import youtube as YT
from clipcut.distribution.publish import publish_run, publish_status


def _m(vid, channel, aspect, hook, duration):
    return {"variant_id": vid, "channel": channel, "aspect": aspect, "duration_s": duration,
            "hook_text": "Did you know most people get this wrong?", "hook_end_s": 2.0, "loop_end": True,
            "captions": [{"start": 0.0, "end": 1.4, "text": "DID YOU KNOW"}, {"start": 1.4, "end": 3.0, "text": "MOST PEOPLE GET THIS WRONG"}],
            "recipe": {"hook": hook, "captions": "word_pop", "channel": channel, "aspect": aspect}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("XAI_API_KEY", "GROK_API_KEY", *YT.ENV_KEYS):
        monkeypatch.delenv(k, raising=False)

    def _no_net(*a, **k):
        raise AssertionError("network call in a test")
    monkeypatch.setattr(YT, "_http", _no_net)


@pytest.fixture
def run(tmp_path):
    d = tmp_path / "r1"
    d.mkdir()
    for m in [_m("v01", "Shorts", "9:16", "m1", 30), _m("v02", "X", "1:1", "m1", 25)]:
        (d / f"{m['variant_id']}.json").write_text(json.dumps(m))
    (d / "v01.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)
    return tmp_path


def _creds(monkeypatch):
    for k in YT.ENV_KEYS:
        monkeypatch.setenv(k, "test-" + k.lower())


def test_without_credentials_everything_is_exported(run):
    out = publish_run("r1", variant_id="v01", channels=["shorts", "x"], root=run)
    assert out["youtube_configured"] is False
    st = {e["channel"]: e for e in out["entries"]}
    assert st["shorts"]["status"] == "exported" and "YT_CLIENT_ID" in st["shorts"]["note"]
    assert st["x"]["status"] == "exported" and st["x"]["variant_id"] == "v02"
    saved = json.loads((run / "r1" / "publish.json").read_text())
    assert set(saved["entries"]) == {"shorts", "x"} and len(saved["history"]) == 2


def test_schedule_is_demo(run):
    export_run("r1", variant_id="v01", root=run, mode="template")
    out = publish_run("r1", channels=["tiktok", "linkedin"], schedule_at="2026-10-20T17:00:00Z", root=run)
    for e in out["entries"]:
        assert e["status"] == "scheduled (demo)" and e["demo"] is True and e["scheduled_for"].startswith("2026-10-20T17:00:00")
    with pytest.raises(DistributionError):
        publish_run("r1", schedule_at="next tuesday", root=run)


def test_publish_uses_named_export_and_rejects_missing_channel(run):
    doc = export_run("r1", variant_id="v01", channels=["x"], root=run, mode="template")
    with pytest.raises(DistributionError) as e:
        publish_run("r1", export_id=doc["export_id"], channels=["tiktok"], root=run)
    assert e.value.status == 404
    with pytest.raises(DistributionError) as e:
        publish_run("r1", export_id="ex_20200101_000000_abcd", root=run)
    assert e.value.status == 404


def test_youtube_upload_with_mocked_http(run, monkeypatch):
    _creds(monkeypatch)
    calls = []

    def fake(method, url, data=None, headers=None, timeout=None):
        calls.append((method, url, data, headers))
        if url == YT.TOKEN_URL:
            assert b"grant_type=refresh_token" in data and b"test-yt_refresh_token" in data
            return 200, {}, json.dumps({"access_token": "AT"}).encode()
        if url == YT.UPLOAD_URL:
            meta = json.loads(data)
            assert meta["status"]["privacyStatus"] == "private"
            assert len(meta["snippet"]["title"]) <= 100 and "Shorts" in meta["snippet"]["tags"]
            assert headers["Authorization"] == "Bearer AT" and headers["X-Upload-Content-Type"] == "video/mp4"
            return 200, {"location": "https://upload.example/session1"}, b""
        if url == "https://upload.example/session1":
            assert method == "PUT" and data.startswith(b"\x00\x00\x00\x18ftyp") and headers["Content-Length"] == str(len(data))
            return 200, {}, json.dumps({"id": "abc123"}).encode()
        raise AssertionError(url)

    monkeypatch.setattr(YT, "_http", fake)
    out = publish_run("r1", variant_id="v01", channels=["shorts", "x"], root=run)
    st = {e["channel"]: e for e in out["entries"]}
    assert st["shorts"]["status"] == "uploaded (private)" and st["shorts"]["video_id"] == "abc123"
    assert st["shorts"]["url"] == "https://www.youtube.com/shorts/abc123" and st["shorts"]["privacy"] == "private"
    assert st["x"]["status"] == "exported"
    assert [c[0] for c in calls] == ["POST", "POST", "PUT"]
    status = publish_status("r1", root=run)
    assert status["youtube_configured"] and [c["channel"] for c in status["channels"]] == ["shorts", "x"]
    assert status["entries"]["shorts"]["state"] == "uploaded"


@pytest.mark.parametrize("stage", ["token", "session", "put"])
def test_youtube_failures_are_reported(run, monkeypatch, stage):
    _creds(monkeypatch)

    def fake(method, url, data=None, headers=None, timeout=None):
        if url == YT.TOKEN_URL:
            return (400, {}, b'{"error":"invalid_grant","error_description":"Token has been expired"}') if stage == "token" else (200, {}, b'{"access_token":"AT"}')
        if url == YT.UPLOAD_URL:
            return (403, {}, b'{"error":{"message":"quotaExceeded"}}') if stage == "session" else (200, {"location": "https://u/s"}, b"")
        return 500, {}, b"oops"

    monkeypatch.setattr(YT, "_http", fake)
    out = publish_run("r1", variant_id="v01", channels=["shorts"], root=run)
    e = out["entries"][0]
    assert e["status"] == "failed" and e["state"] == "failed"
    assert {"token": "expired", "session": "quotaExceeded", "put": "500"}[stage] in e["error"]
    assert "AT" not in e["error"] and "test-yt" not in e["error"]


def test_youtube_without_video_fails_cleanly(run, monkeypatch):
    _creds(monkeypatch)
    (run / "r1" / "v01.mp4").unlink()
    out = publish_run("r1", variant_id="v01", channels=["shorts"], root=run)
    assert out["entries"][0]["status"] == "failed" and "missing" in out["entries"][0]["error"]


def test_http_publish_routes(run, monkeypatch):
    import app as appmod
    appmod.app.state.runs_root = run
    appmod.app.state.publish_background = False
    try:
        c = TestClient(appmod.app)
        assert c.get("/api/distribution/publish/r1").json()["channels"] == []
        r = c.post("/api/distribution/publish", json={"run": "r1", "variant_id": "v01", "channels": ["shorts"], "schedule_at": "2026-10-20T09:00:00-07:00"})
        assert r.status_code == 200, r.text
        assert r.json()["entries"][0]["status"] == "scheduled (demo)"
        assert c.get("/api/distribution/publish/r1").json()["channels"][0]["status"] == "scheduled (demo)"
        assert c.post("/api/distribution/publish", json={"run": "nope"}).status_code == 404
    finally:
        del appmod.app.state.runs_root
        del appmod.app.state.publish_background
