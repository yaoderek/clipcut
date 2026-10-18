"""Metrics upload, weight calibration, and predicted-vs-actual. No network."""
import json
from pathlib import Path

import pytest

from clipcut.ab import analyze, variant_metrics
from clipcut.persona_api import DISCLAIMER, PersonaScoreError, compare_run, score_variants, store_metrics
from clipcut.persona_calibration import CALIBRATED_DISCLAIMER
from clipcut.persona_models import FEATURE_KEYS, PERSONA_ORDER, PERSONAS, heuristic_variant

ROOT = Path(__file__).resolve().parents[1]
MOCK = json.loads((ROOT / "runs" / "demo" / "mock_metrics.json").read_text())


def _manifest(vid, hook_end):
    return {
        "variant_id": vid,
        "duration_s": 28,
        "aspect": "9:16",
        "channel": "TikTok",
        "hook_end_s": hook_end,
        "hook_boundary": "pause",
        "hook_text": "Wait, did you know this secret hack?",
        "cut_times_s": [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24],
        "captions": [{"start": 0, "end": 2, "text": "WAIT"}],
        "silence_removed_s": 1.0,
        "loop_end": True,
        "recipe": {"captions": "word_pop", "ending": "loop", "hook_title": True, "channel": "TikTok", "aspect": "9:16"},
    }


def _plant(root, name="plant", n=10):
    directory = root / name
    directory.mkdir()
    hooks = [0.4, 0.8, 1.2, 1.8, 2.4, 3.2, 4.5, 6.0, 7.2, 8.5]
    metrics = {}
    for i in range(n):
        vid = f"v{i + 1:02d}"
        manifest = _manifest(vid, hooks[i])
        (directory / f"{vid}.json").write_text(json.dumps(manifest))
        # Later hooks were watched more. That opposes the hand-set hook_timing weight.
        metrics[vid] = {"views": 1000, "avg_watch_pct": round(25 + hooks[i] * 7, 2), "shares": 10, "channel": "tiktok"}
    (directory / "results.json").write_text('{"keep": true}\n')
    return directory, metrics


def _hand_set_snapshot():
    return json.loads(json.dumps({pid: {"weight": PERSONAS[pid]["weight"], "base": PERSONAS[pid]["base"], "weights": PERSONAS[pid]["weights"]} for pid in PERSONA_ORDER}))


def test_ab_ignores_metadata_and_mock_file_is_flagged():
    assert MOCK["_meta"]["simulated"] is True
    assert "make_mock_metrics" in MOCK["_meta"]["generator"]
    bare = {k: v for k, v in MOCK.items() if k != "_meta"}
    assert variant_metrics(MOCK).keys() == bare.keys()
    flagged = analyze(MOCK, n_draws=2000, seed=0)
    plain = analyze(bare, n_draws=2000, seed=0)
    assert flagged["winner"] == plain["winner"]
    assert [row["variant_id"] for row in flagged["table"]] == [row["variant_id"] for row in plain["table"]]


def test_metrics_merge_is_additive_and_leaves_sibling_files_alone(tmp_path):
    directory, metrics = _plant(tmp_path, n=4)
    original_variant = (directory / "v01.json").read_bytes()
    original_results = (directory / "results.json").read_bytes()
    (directory / "metrics.json").write_text(json.dumps({
        "v01": {"views": 100, "avg_watch_pct": 40, "shares": 2},
        "note": {"hello": "keep"},
    }))
    first = store_metrics(run="plant", payload={"metrics": {"v02": metrics["v02"]}}, runs_root=tmp_path)
    assert first["added"] == ["v02"]
    assert first["updated"] == []
    assert first["simulated"] is False
    stored = json.loads((directory / "metrics.json").read_text())
    assert list(stored)[0] == "_meta"
    assert stored["_meta"]["simulated"] is False
    assert stored["v01"]["views"] == 100
    assert stored["v02"]["views"] == 1000
    assert stored["note"] == {"hello": "keep"}
    assert (directory / "v01.json").read_bytes() == original_variant
    assert (directory / "results.json").read_bytes() == original_results
    again = store_metrics(run="plant", payload={"v01": metrics["v01"], "v03": metrics["v03"]}, runs_root=tmp_path)
    assert again["updated"] == ["v01"]
    assert "v03" in again["added"]
    stored = json.loads((directory / "metrics.json").read_text())
    assert stored["v01"]["views"] == 1000
    assert stored["v02"]["shares"] == 10
    assert stored["note"]["hello"] == "keep"
    assert (directory / "v01.json").read_bytes() == original_variant


def test_csv_upload_and_query_run(tmp_path):
    directory, _metrics = _plant(tmp_path, n=2)
    csv_text = "variant_id,channel,views,avg_watch_pct,shares,likes\nv01,TikTok,500,55.5,4,9\nv02,Reels,800,0.40,3,\n"
    out = store_metrics(run="plant", csv_bytes=csv_text.encode(), filename="metrics.csv", runs_root=tmp_path)
    assert out["n"] == 2
    assert out["simulated"] is False
    stored = json.loads((directory / "metrics.json").read_text())
    assert stored["v01"] == {"views": 500, "avg_watch_pct": 55.5, "shares": 4, "channel": "TikTok", "likes": 9}
    assert stored["v02"]["avg_watch_pct"] == 0.40
    assert "likes" not in stored["v02"]
    assert stored["_meta"]["source"] == "csv"


def test_mock_metrics_are_never_labeled_real(tmp_path):
    directory, _metrics = _plant(tmp_path, name="demo", n=2)
    (directory / "persona_calibration.json").write_text('{"stale": true}\n')
    declared_real = store_metrics(
        run="demo",
        payload={"simulated": False, "metrics": {k: v for k, v in MOCK.items() if k != "_meta"}},
        runs_root=tmp_path,
    )
    assert declared_real["simulated"] is True
    assert "make_mock_metrics.py" in declared_real["simulated_reason"]
    assert declared_real["calibration"]["fitted"] is False
    stored = json.loads((directory / "metrics.json").read_text())
    assert stored["_meta"]["simulated"] is True
    assert all(row.get("simulated") is True for key, row in stored.items() if key != "_meta" and isinstance(row, dict) and "views" in row)
    assert not (directory / "persona_calibration.json").exists()

    other = tmp_path / "named"
    other.mkdir()
    (other / "v01.json").write_text(json.dumps(_manifest("v01", 1.0)))
    named = store_metrics(
        run="named",
        csv_bytes=b"variant_id,views,avg_watch_pct,shares\nv01,100,50,1\n",
        filename="mock_metrics.csv",
        simulated="false",
        runs_root=tmp_path,
    )
    assert named["simulated"] is True
    assert "filename" in named["simulated_reason"]
    with pytest.raises(PersonaScoreError) as refused:
        score_variants(run="demo", mode="auto", calibration="run", runs_root=tmp_path)
    assert refused.value.status == 400
    assert "simulated" in str(refused.value)


def test_simulated_flag_sticks_when_later_rows_look_real(tmp_path):
    _plant(tmp_path, name="demo", n=2)
    store_metrics(run="demo", payload=MOCK, runs_root=tmp_path)
    follow = store_metrics(
        run="demo",
        payload={"simulated": False, "metrics": {"v09": {"views": 900, "avg_watch_pct": 60, "shares": 8}}},
        runs_root=tmp_path,
    )
    assert follow["simulated"] is True
    assert "already marked simulated" in follow["simulated_reason"]
    stored = json.loads((tmp_path / "demo" / "metrics.json").read_text())
    assert stored["v09"]["simulated"] is True
    assert follow["calibration"]["fitted"] is False


def test_corrupt_metrics_file_is_not_overwritten(tmp_path):
    directory, metrics = _plant(tmp_path, n=2)
    (directory / "metrics.json").write_text("{")
    with pytest.raises(PersonaScoreError) as err:
        store_metrics(run="plant", payload={"metrics": {"v01": metrics["v01"]}}, runs_root=tmp_path)
    assert err.value.status == 409
    assert (directory / "metrics.json").read_text() == "{"


def test_rejects_bad_metrics(tmp_path):
    _plant(tmp_path, n=1)
    with pytest.raises(PersonaScoreError) as shares:
        store_metrics(run="plant", payload={"metrics": {"v01": {"views": 10, "avg_watch_pct": 50, "shares": 11}}}, runs_root=tmp_path)
    assert shares.value.status == 400
    with pytest.raises(PersonaScoreError):
        store_metrics(run="plant", payload={"metrics": {"v01": {"views": True, "avg_watch_pct": 50, "shares": 1}}}, runs_root=tmp_path)
    with pytest.raises(PersonaScoreError):
        store_metrics(run="../clipcut", payload={"metrics": {"v01": {"views": 10, "avg_watch_pct": 50, "shares": 1}}}, runs_root=tmp_path)
    with pytest.raises(PersonaScoreError) as missing:
        store_metrics(run="missing", payload={"metrics": {"v01": {"views": 10, "avg_watch_pct": 50, "shares": 1}}}, runs_root=tmp_path)
    assert missing.value.status == 404
    with pytest.raises(PersonaScoreError):
        store_metrics(run="plant", csv_bytes=b"variant_id,views\nv01,10\n", runs_root=tmp_path)


def test_feature_weight_moves_toward_the_observed_watch_pattern(tmp_path):
    before = _hand_set_snapshot()
    directory, metrics = _plant(tmp_path, n=10)
    out = store_metrics(run="plant", payload={"run": "plant", "metrics": metrics}, runs_root=tmp_path)
    with pytest.raises(PersonaScoreError):
        store_metrics(run="plant", payload={"metrics": metrics, "likes_ignored": 1}, runs_root=tmp_path)
    assert out["calibration"]["fitted"] is True
    assert out["calibration"]["guard"] == "ok"
    assert out["calibration"]["n_samples"] == 10
    mult = out["calibration"]["feature_multipliers"]
    assert mult["hook_timing"] == 0.5
    assert all(mult[key] == 1.0 for key in FEATURE_KEYS if key != "hook_timing")
    quality = out["calibration"]["fit_quality"]
    assert quality["in_sample"] is True
    assert quality["guard"] == "ok"
    assert quality["weighted_by"] == "views"
    assert quality["spearman"] < 0
    assert quality["active_features"] == ["hook_timing"]
    assert quality["mean_predicted"] > quality["mean_actual"]
    doc = json.loads((directory / "persona_calibration.json").read_text())
    assert doc["schema"] == "persona-calibration-v1"
    assert doc["simulated"] is False
    assert doc["bases"] == {pid: PERSONAS[pid]["base"] for pid in PERSONA_ORDER}
    assert doc["feature_weights"]["fast_swiper"]["hook_timing"] == round(0.16 * 0.5, 6)
    assert abs(sum(doc["persona_weights"].values()) - 1) < 1e-6
    assert before == _hand_set_snapshot()
    spreads_hand, spreads_cal = [], []
    for i in range(10):
        manifest = json.loads((directory / f"v{i + 1:02d}.json").read_text())
        spreads_hand.append(heuristic_variant(manifest)["aggregate"])
        spreads_cal.append(heuristic_variant(manifest, feature_weights=doc["feature_weights"], persona_weights=doc["persona_weights"])["aggregate"])
    assert max(spreads_cal) - min(spreads_cal) < max(spreads_hand) - min(spreads_hand)


def test_under_eight_variants_the_fit_shrinks_toward_the_hand_set_weights(tmp_path):
    _directory, metrics = _plant(tmp_path, name="small", n=3)
    out = store_metrics(run="small", payload={"metrics": {k: metrics[k] for k in ("v01", "v02", "v03")}}, runs_root=tmp_path)
    cal = out["calibration"]
    assert cal["fitted"] is True
    assert cal["guard"] == "shrink"
    assert cal["n_samples"] == 3
    assert cal["fit_quality"]["n_prior"] > 100
    hook = cal["feature_multipliers"]["hook_timing"]
    assert 0.92 <= hook < 1.0
    for key, value in cal["feature_multipliers"].items():
        assert 0.92 <= value <= 1.08
    for value in cal["persona_weights"].values():
        assert value > 0


def test_one_variant_does_not_write_a_calibration(tmp_path):
    _directory, metrics = _plant(tmp_path, name="tiny", n=1)
    out = store_metrics(run="tiny", payload={"metrics": {"v01": metrics["v01"]}}, runs_root=tmp_path)
    assert out["calibration"]["fitted"] is False
    assert not (tmp_path / "tiny" / "persona_calibration.json").exists()
    with pytest.raises(PersonaScoreError) as err:
        score_variants(run="tiny", calibration="run", mode="heuristic", runs_root=tmp_path)
    assert err.value.status == 400


def test_score_calibration_reports_source_and_default_stays_heuristic(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("calibrated scoring must not open a network connection")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    directory, metrics = _plant(tmp_path, n=10)
    store_metrics(run="plant", payload={"metrics": metrics}, runs_root=tmp_path)

    class Boom:
        def complete(self, prompt):
            raise AssertionError("calibration must not call the LLM")

    scored = score_variants(run="plant", mode="auto", calibration="run", runs_root=tmp_path, llm_client=Boom())
    assert scored["source"] == "calibrated"
    assert scored["model_version"] == "persona-calibrated-v1"
    assert scored["disclaimer"] == CALIBRATED_DISCLAIMER
    assert scored["calibration"] == "run"
    assert scored["n_samples"] == 10
    assert scored["fit_quality"]["in_sample"] is True
    assert set(scored) == {"source", "model_version", "disclaimer", "winner", "variants", "calibration", "n_samples", "fit_quality"}
    variant = scored["variants"][0]
    assert variant["source"] == "calibrated"
    assert set(variant) == {"id", "source", "aggregate", "personas", "channels"}
    doc = json.loads((directory / "persona_calibration.json").read_text())
    manual = heuristic_variant(json.loads((directory / "v01.json").read_text()), feature_weights=doc["feature_weights"], persona_weights=doc["persona_weights"])
    assert variant["id"] == "v01"
    assert variant["aggregate"] == manual["aggregate"]

    plain = score_variants(manifests=[_manifest("v01", 0.4), _manifest("v02", 8.0)], mode="heuristic")
    explicit = score_variants(manifests=[_manifest("v01", 0.4), _manifest("v02", 8.0)], mode="heuristic", calibration="none")
    assert plain == explicit
    assert plain["source"] == "heuristic"
    assert plain["disclaimer"] == DISCLAIMER
    assert "n_samples" not in plain
    assert "fit_quality" not in plain

    with pytest.raises(PersonaScoreError) as llm:
        score_variants(run="plant", mode="llm", calibration="run", runs_root=tmp_path)
    assert llm.value.status == 400
    with pytest.raises(PersonaScoreError) as needs_run:
        score_variants(manifests=[_manifest("v01", 1.0)], calibration="run")
    assert needs_run.value.status == 400
    with pytest.raises(PersonaScoreError) as bad:
        score_variants(manifests=[_manifest("v01", 1.0)], calibration="trained")
    assert bad.value.status == 400


def test_global_pool_skips_simulated_runs(tmp_path):
    _directory, metrics = _plant(tmp_path, name="plant", n=8)
    store_metrics(run="plant", payload={"metrics": metrics, "scope": "run"}, runs_root=tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    for i in (1, 2):
        (other / f"v0{i}.json").write_text(json.dumps(_manifest(f"v0{i}", 1.0 + i)))
    pooled = store_metrics(
        run="other",
        payload={"scope": "global", "metrics": {
            "v01": {"views": 400, "avg_watch_pct": 70, "shares": 6},
            "v02": {"views": 400, "avg_watch_pct": 30, "shares": 2},
        }},
        runs_root=tmp_path,
    )
    assert pooled["calibration"]["fitted"] is True
    assert pooled["calibration"]["scope"] == "global"
    assert pooled["calibration"]["n_samples"] == 10
    assert not (other / "persona_calibration.json").exists()
    global_doc = json.loads((tmp_path / "_calibration" / "global.json").read_text())
    assert [row["run"] for row in global_doc["runs"]] == ["other", "plant"]
    scored = score_variants(manifests=[_manifest("m01", 1.0)], calibration="global", mode="heuristic", runs_root=tmp_path)
    assert scored["source"] == "calibrated"
    assert scored["calibration"] == "global"
    assert scored["n_samples"] == 10

    store_metrics(run="plant", payload={"simulated": True, "metrics": {"v01": metrics["v01"]}}, runs_root=tmp_path)
    assert not (tmp_path / "plant" / "persona_calibration.json").exists()
    refreshed = json.loads((tmp_path / "_calibration" / "global.json").read_text())
    assert [row["run"] for row in refreshed["runs"]] == ["other"]
    kept = score_variants(manifests=[_manifest("m01", 1.0)], calibration="global", runs_root=tmp_path)
    assert kept["source"] == "calibrated"
    assert kept["n_samples"] == 2
    assert kept["fit_quality"]["guard"] == "shrink"
    store_metrics(run="other", payload={"simulated": True, "metrics": {"v01": {"views": 400, "avg_watch_pct": 70, "shares": 6}}}, runs_root=tmp_path)
    assert not (tmp_path / "_calibration" / "global.json").exists()
    with pytest.raises(PersonaScoreError) as missing:
        score_variants(manifests=[_manifest("m01", 1.0)], calibration="global", runs_root=tmp_path)
    assert missing.value.status == 404


def test_compare_reports_spearman_and_the_thompson_winner(tmp_path):
    directory, metrics = _plant(tmp_path, n=10)
    store_metrics(run="plant", payload={"metrics": metrics}, runs_root=tmp_path)
    report = compare_run("plant", runs_root=tmp_path)
    stored = json.loads((directory / "metrics.json").read_text())
    thompson = analyze(stored, seed=0)
    assert report["simulated"] is False
    assert report["thompson_winner"] == thompson["winner"]
    assert report["thompson_confident"] == thompson["confident"]
    assert [row["variant_id"] for row in report["thompson"]] == [row["variant_id"] for row in thompson["table"]]
    assert report["ab_seed"] == 0 and report["ab_draws"] == 20000
    assert [row["id"] for row in report["variants"]] == [f"v{i:02d}" for i in range(1, 11)]
    assert report["n_paired"] == 10
    assert report["spearman"] < 0
    assert report["spearman_calibrated"] is not None
    assert report["calibration"]["n_samples"] == 10
    assert "not simulated" in report["disclaimer"]
    first = report["variants"][0]
    assert first["channel"] == "tiktok"
    assert first["views"] == 1000
    assert first["predicted"] == heuristic_variant(json.loads((directory / "v01.json").read_text()))["aggregate"]
    assert 0 <= first["actual"] <= 1
    assert "hand-set" in report["disclaimer"]

    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "v01.json").write_text(json.dumps(_manifest("v01", 1.2)))
    store_metrics(run="demo", payload=MOCK, runs_root=tmp_path)
    sim = compare_run("demo", runs_root=tmp_path)
    assert sim["simulated"] is True
    assert sim["calibration"] is None
    assert sim["spearman_calibrated"] is None
    assert all(row["predicted_calibrated"] is None for row in sim["variants"])
    assert "simulated" in sim["disclaimer"]
    assert sim["thompson_winner"] == analyze(MOCK, seed=0)["winner"]
    assert sim["by_channel"] == {}


def test_compare_by_channel_thompson_is_the_subset(tmp_path):
    _directory, metrics = _plant(tmp_path, n=4)
    metrics["v01"]["channel"] = "tiktok"
    metrics["v01"]["avg_watch_pct"] = 30
    metrics["v01"]["shares"] = 4
    metrics["v02"]["channel"] = "TikTok"
    metrics["v02"]["avg_watch_pct"] = 55
    metrics["v02"]["shares"] = 12
    metrics["v03"]["channel"] = "x"
    metrics["v03"]["avg_watch_pct"] = 92
    metrics["v03"]["shares"] = 80
    metrics["v04"]["channel"] = "x"
    metrics["v04"]["avg_watch_pct"] = 22
    metrics["v04"]["shares"] = 1
    metrics["v04"].pop("channel")
    store_metrics(run="plant", payload={"metrics": metrics}, runs_root=tmp_path)
    report = compare_run("plant", runs_root=tmp_path)
    assert set(report) >= {"run", "simulated", "variants", "thompson_winner", "spearman", "calibration", "by_channel"}
    assert set(report["by_channel"]) == {"tiktok", "x"}
    assert report["by_channel"]["tiktok"]["n"] == 2
    assert [row["id"] for row in report["by_channel"]["tiktok"]["variants"]] == ["v01", "v02"]
    assert "predicted" in report["by_channel"]["tiktok"]["variants"][0]
    assert "actual" in report["by_channel"]["x"]["variants"][0]
    assert all(row["id"] != "v04" for block in report["by_channel"].values() for row in block["variants"])

    def rows_of(block):
        return {row["id"]: {"views": row["views"], "avg_watch_pct": row["avg_watch_pct"], "shares": row["shares"]} for row in block["variants"]}

    tiktok = report["by_channel"]["tiktok"]
    x_block = report["by_channel"]["x"]
    assert tiktok["ab_seed"] == 0 and tiktok["ab_draws"] == 20000
    assert tiktok["thompson_winner"] == analyze(rows_of(tiktok), seed=0)["winner"]
    assert x_block["thompson_winner"] == analyze(rows_of(x_block), seed=0)["winner"]
    assert [row["variant_id"] for row in tiktok["thompson"]] == [row["variant_id"] for row in analyze(rows_of(tiktok), seed=0)["table"]]
    full = {row["id"]: {"views": row["views"], "avg_watch_pct": row["avg_watch_pct"], "shares": row["shares"]} for row in report["variants"]}
    assert report["thompson_winner"] == analyze(full, seed=0)["winner"]
    assert tiktok["thompson_winner"] in {"v01", "v02"}
    assert report["thompson_winner"] != tiktok["thompson_winner"]


def test_http_contracts(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    import app as app_module
    from app import app

    monkeypatch.setattr(app_module, "RUNS", tmp_path)
    _directory, metrics = _plant(tmp_path, n=8)
    client = TestClient(app)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/persona/metrics" in paths
    assert "/api/persona/compare/{run}" in paths
    assert "/api/persona/score" in paths and "/api/persona/hooks" in paths and "/api/score" in paths

    csv_body = "variant_id,channel,views,avg_watch_pct,shares,likes\n" + "\n".join(
        f"{vid},{row['channel']},{row['views']},{row['avg_watch_pct']},{row['shares']}," for vid, row in metrics.items()
    ) + "\n"
    uploaded = client.post(
        "/api/persona/metrics",
        data={"run": "plant", "scope": "run"},
        files={"file": ("metrics.csv", csv_body, "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["simulated"] is False
    assert uploaded.json()["calibration"]["fitted"] is True
    assert "detail" not in uploaded.json()

    scored = client.post("/api/persona/score", json={"run": "plant", "mode": "heuristic", "calibration": "run"})
    assert scored.status_code == 200
    body = scored.json()
    assert body["source"] == "calibrated"
    assert body["n_samples"] == 8
    assert body["fit_quality"]["guard"] == "ok"

    default = client.post("/api/persona/score", json={"manifests": [_manifest("v01", 1.0)], "mode": "heuristic"})
    assert default.status_code == 200
    assert set(default.json()) == {"source", "model_version", "disclaimer", "winner", "variants"}
    assert default.json()["source"] == "heuristic"

    nulled = client.post("/api/persona/score", json={"manifests": [_manifest("v01", 1.0)], "calibration": None, "mode": None})
    assert nulled.status_code == 200 and nulled.json()["source"] == "heuristic"

    compared = client.get("/api/persona/compare/plant")
    assert compared.status_code == 200
    assert compared.json()["thompson_winner"]
    assert compared.json()["n"] == 8

    missing = client.get("/api/persona/compare/nope")
    assert missing.status_code == 404 and "error" in missing.json() and "detail" not in missing.json()
    bad = client.post("/api/persona/metrics", json={"run": "plant", "metrics": {}})
    assert bad.status_code == 400 and bad.json()["error"]
    legacy = client.post("/api/score", json=_manifest("v01", 1.0))
    assert legacy.status_code == 200 and legacy.json()["model_version"] == "dummy-heuristic-v0"
