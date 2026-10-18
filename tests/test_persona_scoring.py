"""Persona scoring and POST /api/persona/score. Offline: no network, no data/ or models/."""
import json
from pathlib import Path

import pytest

from clipcut.channels import CHANNEL_SPECS
from clipcut.llm_persona import judgment_score, parse_judgment, rate, variant_brief
from clipcut.persona_api import DISCLAIMER, PersonaScoreError, score_variants
from clipcut.persona_models import FEATURE_KEYS, PERSONA_ORDER, PERSONAS, heuristic_variant, score_persona
from clipcut.retention import score as legacy_score

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "runs" / "v2_niosh_multi"

GOOD = {
    "variant_id": "v07",
    "duration_s": 28,
    "aspect": "9:16",
    "channel": "TikTok",
    "hook_end_s": 1.4,
    "hook_boundary": "pause",
    "hook_text": "Wait, did you know this secret hack?",
    "cut_times_s": [1.4, 3.2, 5.4, 7.6, 10.0, 12.4, 15.0, 17.8, 20.5, 23.2, 26.0],
    "captions": [{"start": 0, "end": 1, "text": "WAIT"}, {"start": 1, "end": 2, "text": "SECRET HACK"}],
    "silence_removed_s": 1.8,
    "loop_end": True,
    "recipe": {"captions": "word_pop", "ending": "loop", "hook_title": True, "channel": "TikTok", "aspect": "9:16"},
}

BAD = {
    "variant_id": "v99",
    "duration_s": 150,
    "aspect": "16:9",
    "channel": "YouTube",
    "hook_end_s": 12,
    "hook_boundary": "word",
    "hook_text": "Um so anyway we should probably begin the meeting now",
    "cut_times_s": [],
    "captions": [],
    "silence_removed_s": 0,
    "loop_end": False,
    "recipe": {"captions": "none", "ending": "hard", "hook_title": False, "channel": "YouTube", "aspect": "16:9"},
}


def _no_keys(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)


def test_persona_catalog_matches_contract():
    assert tuple(PERSONAS) == PERSONA_ORDER
    assert [p["weight"] for p in PERSONAS.values()] == [0.30, 0.30, 0.30, 0.10]
    assert [p["base"] for p in PERSONAS.values()] == [0.40, 0.46, 0.52, 0.48]
    for persona in PERSONAS.values():
        assert tuple(persona["weights"]) == FEATURE_KEYS
        assert all(w > 0 for w in persona["weights"].values())


def test_heuristic_score_is_clamped_sum_of_contributions():
    for manifest in (GOOD, BAD):
        for pid in PERSONA_ORDER:
            row = score_persona(manifest, pid)
            assert set(row["contributions"]) == set(FEATURE_KEYS)
            raw = round(PERSONAS[pid]["base"] + sum(row["contributions"].values()), 4)
            expect = 0.0 if raw < 0 else 1.0 if raw > 1 else raw
            assert row["score"] == expect
            assert 0.0 <= row["score"] <= 1.0
            assert row["reasons"] and all(isinstance(r, str) and r for r in row["reasons"])


def test_fast_swiper_prefers_a_tight_hook_over_a_long_uncut_clip():
    good = score_persona(GOOD, "fast_swiper")["score"]
    bad = score_persona(BAD, "fast_swiper")["score"]
    assert good > bad
    assert score_persona(GOOD, "fast_swiper")["contributions"]["hook_timing"] > 0
    assert score_persona(BAD, "fast_swiper")["contributions"]["hook_timing"] < 0
    assert score_persona(BAD, "fast_swiper")["contributions"]["captions"] < 0
    assert score_persona(BAD, "fast_swiper")["contributions"]["pacing"] < 0


def test_aggregate_is_weighted_mean_and_winner_breaks_ties_by_id():
    out = score_variants(manifests=[BAD, GOOD], mode="heuristic")
    by_id = {v["id"]: v for v in out["variants"]}
    for vid, manifest in (("v99", BAD), ("v07", GOOD)):
        manual = round(sum(PERSONAS[p["id"]]["weight"] * p["score"] for p in by_id[vid]["personas"]), 4)
        assert by_id[vid]["aggregate"] == manual
    assert out["winner"] == "v07"
    assert out["source"] == "heuristic"
    assert out["model_version"] == "persona-heuristic-v1"
    assert out["disclaimer"] == DISCLAIMER
    tied = score_variants(manifests=[{**GOOD, "variant_id": "v02"}, {**GOOD, "variant_id": "v01"}], mode="heuristic")
    assert tied["variants"][0]["aggregate"] == tied["variants"][1]["aggregate"]
    assert tied["winner"] == "v01"


def test_channels_use_specs_and_penalize_a_wrong_aspect():
    scored = heuristic_variant(GOOD)
    ids = [c["id"] for c in scored["channels"]]
    assert ids[0] == min(ids, key=lambda i: (-next(c["fit"] for c in scored["channels"] if c["id"] == i), i))
    assert set(ids) == {name.lower() for name in CHANNEL_SPECS}
    tiktok = next(c for c in scored["channels"] if c["id"] == "tiktok")
    youtube = next(c for c in scored["channels"] if c["id"] == "youtube")
    assert tiktok["label"] == "TikTok"
    assert 0.0 <= tiktok["fit"] <= 1.0
    assert tiktok["fit"] > youtube["fit"]
    assert any("accepted" in r for r in tiktok["reasons"])
    wide = heuristic_variant({**GOOD, "aspect": "16:9", "resolution": [1920, 1080]})
    wide_tt = next(c for c in wide["channels"] if c["id"] == "tiktok")
    assert wide_tt["fit"] < tiktok["fit"]
    assert any("not in" in r for r in wide_tt["reasons"])


def test_resolution_snaps_when_aspect_is_absent():
    scored = heuristic_variant({"variant_id": "v01", "duration_s": 20, "resolution": [1080, 1920], "hook_end_s": 1.0, "hook_text": "Hi"})
    tiktok = next(c for c in scored["channels"] if c["id"] == "tiktok")
    assert any("9:16" in r and "accepted" in r for r in tiktok["reasons"])


def test_v1_manifest_and_missing_fields_do_not_crash():
    v1 = {
        "variants": [
            {"variant_id": "v01", "length_s": 15, "hook_text": "Number 1.", "hook_score": 1.1, "jump_cuts": False,
             "n_cuts": 1, "cuts_per_min": 4.0, "caption_style": "minimal", "hook_zoom": False, "spans": [[20.0, 23.0]]},
            {"length_s": 30, "hook_text": "Number 1.", "caption_style": "none"},
        ]
    }
    out = score_variants(manifests=[v1], variant_ids=["v01"], mode="heuristic")
    assert out["variants"][0]["id"] == "v01"
    assert out["variants"][0]["personas"][0]["contributions"]["hook_timing"] != 0
    bare = score_variants(manifests=[{"note": "empty"}], mode="heuristic")
    assert bare["variants"][0]["id"] == "m01"
    assert bare["winner"] == "m01"


def test_run_loader_scores_v2_manifests(monkeypatch):
    _no_keys(monkeypatch)
    out = score_variants(run="v2_niosh_multi", variant_ids=["v07"], mode="auto", runs_root=ROOT / "runs")
    assert out["winner"] == "v07"
    variant = out["variants"][0]
    assert variant["source"] == "heuristic"
    assert [p["id"] for p in variant["personas"]] == list(PERSONA_ORDER)
    assert variant["personas"][0]["contributions"]["hook_timing"] > 0  # hook_end_s is 2.3s
    full = score_variants(run="v2_niosh_multi", mode="heuristic", runs_root=ROOT / "runs")
    again = score_variants(run="v2_niosh_multi", mode="heuristic", runs_root=ROOT / "runs")
    assert [v["id"] for v in full["variants"]] == [f"v{i:02d}" for i in range(1, 17)]
    assert full == again
    assert full["winner"] in {v["id"] for v in full["variants"]}


def test_request_errors(monkeypatch):
    _no_keys(monkeypatch)
    with pytest.raises(PersonaScoreError) as both:
        score_variants(run="v2_niosh_multi", manifests=[GOOD])
    assert both.value.status == 400
    with pytest.raises(PersonaScoreError) as neither:
        score_variants()
    assert neither.value.status == 400
    with pytest.raises(PersonaScoreError) as mode:
        score_variants(manifests=[GOOD], mode="trained")
    assert mode.value.status == 400
    with pytest.raises(PersonaScoreError) as missing:
        score_variants(run="does_not_exist", runs_root=ROOT / "runs")
    assert missing.value.status == 404
    with pytest.raises(PersonaScoreError) as ids:
        score_variants(run="v2_niosh_multi", variant_ids=["v07", "nope"], runs_root=ROOT / "runs")
    assert ids.value.status == 404
    with pytest.raises(PersonaScoreError) as traversal:
        score_variants(run="../clipcut", runs_root=ROOT / "runs")
    assert traversal.value.status == 400
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    with pytest.raises(PersonaScoreError) as llm:
        score_variants(manifests=[GOOD], mode="llm")
    assert llm.value.status == 503


def test_llm_fallback_and_structured_judgment(monkeypatch):
    _no_keys(monkeypatch)
    calls = []

    class Client:
        def complete(self, prompt):
            calls.append(prompt)
            if "fast_swiper" in prompt:
                return "Sure!\n" + json.dumps({
                    "score": 0.81,
                    "reasons": ["simulated: the open is fast enough"],
                    "contributions": {"hook_timing": 0.2, "captions": "nope"},
                })
            return "I can't score this."

    mixed = score_variants(manifests=[GOOD], mode="auto", llm_client=Client())
    assert mixed["source"] == "mixed"
    assert mixed["model_version"] == "persona-mixed-v1"
    fast = mixed["variants"][0]["personas"][0]
    assert fast["id"] == "fast_swiper"
    assert fast["score"] == 0.81
    assert fast["reasons"] == ["simulated: the open is fast enough"]
    assert fast["contributions"]["hook_timing"] == 0.2
    assert fast["contributions"]["captions"] == 0.0
    assert fast["contributions"]["pacing"] == 0.0
    casual = mixed["variants"][0]["personas"][1]
    assert casual["id"] == "casual_browser"
    assert casual["score"] == score_persona(GOOD, "casual_browser")["score"]
    assert "length_s" not in calls[0] or "duration_s" in calls[0]
    assert "Wait, did you know this secret hack?" in calls[0]

    class All:
        def complete(self, prompt):
            return json.dumps({"score": 0.42, "reasons": ["llm-simulated"], "contributions": {k: 0.01 for k in FEATURE_KEYS}})

    llm = score_variants(manifests=[GOOD, BAD], mode="llm", llm_client=All())
    assert llm["source"] == "llm"
    assert llm["model_version"] == "llm-persona-v1"
    assert all(v["source"] == "llm" and v["aggregate"] == 0.42 for v in llm["variants"])

    class Boom:
        def complete(self, prompt):
            raise RuntimeError("network down")

    fallen = score_variants(manifests=[GOOD], mode="auto", llm_client=Boom())
    assert fallen["source"] == "heuristic"
    assert fallen["variants"][0]["personas"] == heuristic_variant(GOOD)["personas"]


def test_llm_persona_accepts_v2_manifest_without_v1_keys(monkeypatch):
    _no_keys(monkeypatch)
    manifest = json.loads((RUN / "v07.json").read_text())
    assert "length_s" not in manifest
    assert rate("Fast swiper", manifest) is None
    seen = {}

    class Client:
        def complete(self, prompt):
            seen["prompt"] = prompt
            return json.dumps({"swipe_away_in_3s_prob": 0.2, "expected_watch_pct": 70, "would_share": True, "why": "would share the claim"})

    parsed = rate("Sharer", manifest, client=Client())
    assert parsed["why"] == "would share the claim"
    assert "There's just no price" in seen["prompt"]
    score = judgment_score(parsed)
    assert abs(score - (0.4 * 0.8 + 0.4 * 0.7 + 0.2)) < 1e-9
    brief = variant_brief(manifest)
    assert brief["duration_s"] == manifest["duration_s"]
    assert "caption_excerpt" in brief
    assert variant_brief({"length_s": 15, "jump_cuts": False, "caption_style": "minimal"})["duration_s"] == 15
    assert parse_judgment("not json") is None
    assert rate("x", manifest, client=type("E", (), {"complete": lambda self, p: (_ for _ in ()).throw(TimeoutError())})()) is None


def test_heuristic_does_not_touch_the_network(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("heuristic scoring must not open a network connection")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    score_variants(manifests=[GOOD], mode="heuristic")


def test_legacy_scorer_payload_unchanged():
    legacy = legacy_score(GOOD)
    assert legacy["model_version"] == "dummy-heuristic-v0"
    assert legacy["label"] == "heuristic, not trained"
    assert set(legacy["personas"]) == set(PERSONA_ORDER)
    for row in legacy["personas"].values():
        assert {"p_swipe3s", "watch_pct", "p_share", "score", "channel", "fit"} <= set(row)
    assert "overall" in legacy and "channels" in legacy


def test_http_contract(monkeypatch):
    _no_keys(monkeypatch)
    from fastapi.testclient import TestClient
    from app import app

    client = TestClient(app)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/score" in paths and "/api/persona/score" in paths

    ok = client.post("/api/persona/score", json={"manifests": [GOOD, BAD], "mode": "heuristic"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["winner"] == "v07"
    assert body["source"] == "heuristic"
    assert set(body) == {"source", "model_version", "disclaimer", "winner", "variants"}
    variant = body["variants"][0]
    assert set(variant) == {"id", "source", "aggregate", "personas", "channels"}
    persona = variant["personas"][0]
    assert set(persona) == {"id", "label", "description", "score", "reasons", "contributions"}

    bad = client.post("/api/persona/score", json={})
    assert bad.status_code == 400 and "error" in bad.json() and "detail" not in bad.json()
    missing = client.post("/api/persona/score", json={"run": "nope"})
    assert missing.status_code == 404 and "error" in missing.json()
    needs_key = client.post("/api/persona/score", json={"manifests": [GOOD], "mode": "llm"})
    assert needs_key.status_code == 503 and "XAI_API_KEY" in needs_key.json()["error"]
    junk = client.post("/api/persona/score", content=b"not-json", headers={"content-type": "application/json"})
    assert junk.status_code == 400 and junk.json()["error"]

    legacy = client.post("/api/score", json=GOOD)
    assert legacy.status_code == 200
    assert legacy.json()["model_version"] == "dummy-heuristic-v0"
    assert "p_swipe3s" in legacy.json()["overall"]
