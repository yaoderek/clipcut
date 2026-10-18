"""Campaign dashboard: wraps persona_api.compare_run, joins publish.json and the latest export. No network."""
import json

import pytest
from fastapi.testclient import TestClient

from clipcut import persona_api
from clipcut.distribution import DistributionError, export_run
from clipcut.distribution.dashboard import dashboard
from clipcut.distribution.publish import publish_run


def _m(vid, channel, aspect, hook_end):
    return {"variant_id": vid, "channel": channel, "aspect": aspect, "duration_s": 28, "hook_end_s": hook_end,
            "hook_text": "Did you know most people get this wrong?", "cut_times_s": [2, 4, 6, 8, 10], "loop_end": True,
            "captions": [{"start": 0, "end": 2, "text": "DID YOU KNOW"}],
            "recipe": {"hook": "m1", "captions": "word_pop", "channel": channel, "aspect": aspect, "ending": "loop"}}


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)


@pytest.fixture
def run(tmp_path):
    d = tmp_path / "r1"
    d.mkdir()
    spec = [("v01", "TikTok", "9:16", 0.8), ("v02", "TikTok", "9:16", 4.0), ("v03", "Shorts", "9:16", 1.5), ("v04", "X", "1:1", 2.0)]
    for vid, ch, asp, he in spec:
        (d / f"{vid}.json").write_text(json.dumps(_m(vid, ch, asp, he)))
    return tmp_path


def _metrics(root, simulated=True):
    persona_api.store_metrics(run="r1", runs_root=root, payload={"simulated": simulated, "metrics": {
        "v01": {"views": 5000, "avg_watch_pct": 40.0, "shares": 50, "channel": "tiktok"},
        "v02": {"views": 5000, "avg_watch_pct": 62.0, "shares": 120, "channel": "tiktok"},
        "v03": {"views": 3000, "avg_watch_pct": 55.0, "shares": 30, "channel": "shorts"},
        "v04": {"views": 2000, "avg_watch_pct": 30.0, "shares": 90},
    }})


def test_no_metrics_still_shows_export_and_publish(run):
    export_run("r1", root=run, mode="template", channels=["tiktok", "x"])
    publish_run("r1", root=run, schedule_at="2026-10-20T17:00:00Z")
    out = dashboard("r1", root=run)
    assert out["metrics_available"] is False and "POST /api/persona/metrics" in out["metrics_note"]
    assert out["overall"] is None and out["predicted_vs_actual"] == []
    assert [c["channel"] for c in out["channels"]] == ["tiktok", "x"]
    assert out["channels"][0]["publish"]["status"] == "scheduled (demo)" and out["channels"][0]["export"]["copy_source"] == "template"


def test_grouped_by_channel_from_compare_rows(run):
    _metrics(run)
    export_run("r1", root=run, mode="template")
    out = dashboard("r1", root=run)
    cmp = persona_api.compare_run("r1", runs_root=run)
    assert out["metrics_available"] and out["simulated"] is True and out["disclaimer"] == cmp["disclaimer"]
    assert out["overall"]["thompson_winner"] == cmp["thompson_winner"]
    assert out["by_channel_source"] == "compare"
    assert set(cmp["by_channel"]) == {"tiktok", "shorts"}
    tik_rows = cmp["by_channel"]["tiktok"]["variants"]
    assert [row["id"] for row in tik_rows] == ["v01", "v02"]
    assert set(tik_rows[0]) >= {"id", "predicted", "actual", "views", "avg_watch_pct", "shares", "channel"}
    assert cmp["by_channel"]["tiktok"]["thompson_winner"] in {"v01", "v02"}
    assert cmp["by_channel"]["shorts"]["thompson_winner"] == "v03"
    ch = {c["channel"]: c for c in out["channels"]}
    assert ch["tiktok"]["variants"] == ["v01", "v02"] and ch["tiktok"]["winner_actual"]["id"] == "v02"
    assert ch["tiktok"]["thompson_winner"] == cmp["by_channel"]["tiktok"]["thompson_winner"]
    assert ch["shorts"]["winner_actual"]["id"] == "v03" and ch["shorts"]["thompson_winner"] == "v03"
    assert all(row["id"] != "v04" for block in cmp["by_channel"].values() for row in block["variants"])
    assert ch["reels"]["n"] == 0 and ch["reels"]["export"] is not None
    pva = {r["id"]: r for r in out["predicted_vs_actual"]}
    assert pva["v01"]["predicted"] is not None and pva["v01"]["channel"] == "tiktok"
    assert pva["v04"]["channel"] == "unassigned"


def test_uses_compare_by_channel_when_present(run, monkeypatch):
    _metrics(run)
    real = persona_api.compare_run

    def with_by_channel(r, runs_root=None):
        out = real(r, runs_root=runs_root)
        out["by_channel"] = {"tiktok": {"variants": ["v01", "v02"], "thompson_winner": "v01", "thompson_confident": False, "spearman": 1.0},
                             "shorts": {"variants": [{"id": "v03", "actual": 0.5, "predicted": 0.6}], "thompson_winner": "v03"}}
        return out

    monkeypatch.setattr(persona_api, "compare_run", with_by_channel)
    out = dashboard("r1", root=run)
    assert out["by_channel_source"] == "compare"
    ch = {c["channel"]: c for c in out["channels"]}
    assert ch["tiktok"]["thompson_winner"] == "v01" and ch["tiktok"]["winner_actual"]["id"] == "v02"
    assert ch["tiktok"]["compare"]["spearman"] == 1.0 and ch["shorts"]["thompson_winner"] == "v03"


def test_bad_run(run):
    with pytest.raises(DistributionError) as e:
        dashboard("nope", root=run)
    assert e.value.status == 404


def test_dashboard_does_not_write_metrics(run):
    out = dashboard("r1", root=run)
    assert not (run / "r1" / "metrics.json").exists() and out["metrics_available"] is False


def test_http_dashboard(run):
    import app as appmod
    _metrics(run)
    appmod.app.state.runs_root = run
    try:
        r = TestClient(appmod.app).get("/api/distribution/dashboard/r1")
        assert r.status_code == 200 and r.json()["overall"]["n"] == 4
        assert TestClient(appmod.app).get("/api/distribution/dashboard/..%2Fx").status_code in (400, 404)
    finally:
        del appmod.app.state.runs_root


def test_extra_channels_from_real_compare_by_channel(run):
    persona_api.store_metrics(run="r1", runs_root=run, payload={"simulated": True, "metrics": {
        "v01": {"views": 5000, "avg_watch_pct": 40.0, "shares": 50, "channel": "tiktok"},
        "v02": {"views": 5000, "avg_watch_pct": 62.0, "shares": 120, "channel": "tiktok"},
        "v03": {"views": 3000, "avg_watch_pct": 55.0, "shares": 30, "channel": "youtube"},
        "v04": {"views": 2000, "avg_watch_pct": 30.0, "shares": 90, "channel": "IG_Feed"},
    }})
    cmp = persona_api.compare_run("r1", runs_root=run)
    out = dashboard("r1", root=run)
    if cmp.get("by_channel"):
        assert out["by_channel_source"] == "compare"
    ch = {c["channel"]: c for c in out["channels"]}
    assert list(ch) == ["tiktok", "ig_feed", "youtube"]  # five export channels first, then others sorted
    assert ch["youtube"]["label"] == "YouTube" and ch["youtube"]["exportable"] is False and ch["youtube"]["n"] == 1
    assert ch["ig_feed"]["label"] == "IG_Feed" and ch["tiktok"]["exportable"] is True
    assert ch["tiktok"]["winner_actual"]["id"] == "v02"
    assert "unassigned" not in ch
    for c in out["channels"]:
        assert "thompson_confident" in c
