"""Hook rewrites and POST /api/persona/hooks. Offline: no network."""
import json
import re
from pathlib import Path

import pytest

from clipcut.hook_rewrite import DISCLAIMER, predicted_delta
from clipcut.llm_persona import parse_rewrites
from clipcut.persona_api import PersonaScoreError, rewrite_hooks
from clipcut.persona_models import PERSONA_ORDER, score_persona

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "runs" / "v2_niosh_multi"

V07 = {
    "variant_id": "v07",
    "duration_s": 32.95,
    "aspect": "9:16",
    "channel": "Reels",
    "hook_text": "There's just no price you can put on that.",
    "hook_end_s": 2.3,
    "hook_score": 0.2,
    "recipe": {"captions": "word_pop", "hook_title": True, "channel": "Reels"},
}


def _no_keys(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)


def _words(text):
    return {w.lower() for w in re.findall(r"[A-Za-z0-9']+", text)}


def test_v07_heuristic_matches_the_documented_example(monkeypatch):
    _no_keys(monkeypatch)
    doc = (ROOT / "docs" / "PERSONA_API.md").read_text()
    fence = doc.index("### Example: `v2_niosh_multi` / `v07`, heuristic")
    start = doc.index("```json", fence) + len("```json")
    example = json.loads(doc[start:doc.index("```", start)])
    live = rewrite_hooks(run="v2_niosh_multi", variant_ids=["v07"], mode="heuristic")
    assert live == example
    assert live["source"] == "heuristic"
    assert live["model_version"] == "hook-heuristic-v1"
    assert live["disclaimer"] == DISCLAIMER
    assert "trained" in live["disclaimer"] and "not trained" in live["disclaimer"]
    assert "not validated" in live["disclaimer"]
    variant = live["variants"][0]
    assert variant["original_hook"] == "There's just no price you can put on that."
    assert [s["persona"] for s in variant["suggestions"]] == [p for p in PERSONA_ORDER for _ in range(3)]
    hooks = [s["hook"] for s in variant["suggestions"]]
    assert hooks[0].startswith("Wait — did you know")
    assert "Why does this matter" in hooks[6]
    assert hooks[9].startswith("Would you share this")
    assert len(set(hooks)) >= 8
    for suggestion in variant["suggestions"]:
        assert suggestion["origin"] == "heuristic"
        assert suggestion["predicted_delta_label"] == "heuristic_estimate"
        assert suggestion["title_card"] == "NO PRICE"
        assert "channel" not in suggestion
        assert "not a trained or validated line" in suggestion["reason"]
        assert "heuristic rescore" in suggestion["reason"]
        assert set(re.findall(r"\d+", suggestion["hook"])) <= set(re.findall(r"\d+", variant["original_hook"]))
        assert _words(suggestion["title_card"]) <= _words(variant["original_hook"])
        assert "price" in suggestion["hook"]
    by_persona = {}
    for suggestion in variant["suggestions"]:
        by_persona.setdefault(suggestion["persona"], []).append(suggestion["predicted_delta"])
    for deltas in by_persona.values():
        assert deltas == sorted(deltas, reverse=True)
        assert deltas[0] > 0


def test_predicted_delta_is_a_rescore_and_does_not_mutate(monkeypatch):
    _no_keys(monkeypatch)
    manifest = json.loads(json.dumps(V07))
    out = rewrite_hooks(manifests=[manifest], personas=["fast_swiper"], n=1, mode="heuristic")
    suggestion = out["variants"][0]["suggestions"][0]
    edited = dict(manifest)
    edited.pop("hook_score")
    edited["hook_text"] = suggestion["hook"]
    manual = round(score_persona(edited, "fast_swiper")["score"] - score_persona(manifest, "fast_swiper")["score"], 4)
    assert suggestion["predicted_delta"] == manual
    assert suggestion["predicted_delta"] == predicted_delta(manifest, "fast_swiper", suggestion["hook"])
    assert manifest["hook_score"] == 0.2
    assert manifest["hook_text"] == V07["hook_text"]
    # An explicit hook_score hides the new sentence's hook_strength. The rescore drops it on the copy.
    stuck = dict(manifest)
    stuck["hook_text"] = suggestion["hook"]
    assert score_persona(stuck, "fast_swiper")["contributions"]["hook_strength"] == score_persona(manifest, "fast_swiper")["contributions"]["hook_strength"]
    assert suggestion["predicted_delta"] != 0


def test_long_line_uses_a_source_clause_and_does_not_invent(monkeypatch):
    _no_keys(monkeypatch)
    manifest = json.loads((RUN / "v01.json").read_text())
    out = rewrite_hooks(manifests=[manifest], mode="heuristic")
    variant = out["variants"][0]
    assert variant["original_hook"].startswith("She really helped me with statistics")
    for suggestion in variant["suggestions"]:
        assert "research assistant" in suggestion["hook"]
        assert "strongest short clause" in suggestion["reason"]
        assert suggestion["title_card"] == "RESEARCH ASSISTANT"
        assert "You — you" not in suggestion["hook"]


def test_channels_override_persona_voice(monkeypatch):
    _no_keys(monkeypatch)
    out = rewrite_hooks(
        manifests=[V07],
        personas=["fast_swiper", "completionist"],
        channels=["TikTok", "YouTube"],
        n=1,
        mode="heuristic",
    )
    suggestions = out["variants"][0]["suggestions"]
    assert [(s["persona"], s["channel"], s["hook"]) for s in suggestions] == [
        ("fast_swiper", "tiktok", "Wait — did you know there's just no price you can put on that?"),
        ("fast_swiper", "youtube", "Why does this matter: there's just no price you can put on that?"),
        ("completionist", "tiktok", "Wait — did you know there's just no price you can put on that?"),
        ("completionist", "youtube", "Why does this matter: there's just no price you can put on that?"),
    ]
    assert all("Shaped for TikTok." in s["reason"] or "Shaped for YouTube." in s["reason"] for s in suggestions)


def test_caption_and_transcript_are_used_when_hook_text_is_missing(monkeypatch):
    _no_keys(monkeypatch)
    caps = rewrite_hooks(manifests=[{"captions": [{"start": 0, "end": 1.2, "text": "Secret hack for you"}]}], n=1, mode="heuristic")
    assert "Secret hack" in caps["variants"][0]["original_hook"]
    assert "secret" in caps["variants"][0]["suggestions"][0]["hook"].lower()
    transcript = rewrite_hooks(manifests=[{"transcript": [{"text": "Sleep is the secret"}, "later"]}], personas=["sharer_engager"], n=1, mode="heuristic")
    assert "secret" in transcript["variants"][0]["original_hook"].lower()
    assert "secret" in transcript["variants"][0]["suggestions"][0]["hook"].lower()


def test_empty_variant_invents_nothing(monkeypatch):
    _no_keys(monkeypatch)
    out = rewrite_hooks(manifests=[{"note": "no words"}], personas=["fast_swiper"], n=2, mode="heuristic")
    variant = out["variants"][0]
    assert variant["id"] == "m01"
    assert variant["original_hook"] == ""
    assert len(variant["suggestions"]) == 2
    for suggestion in variant["suggestions"]:
        assert suggestion["hook"] == ""
        assert "title_card" not in suggestion
        assert suggestion["predicted_delta"] == 0.0
        assert "no line was invented" in suggestion["reason"]


def test_full_run_is_deterministic_and_ordered(monkeypatch):
    _no_keys(monkeypatch)
    first = rewrite_hooks(run="v2_niosh_multi", mode="heuristic")
    second = rewrite_hooks(run="v2_niosh_multi", mode="heuristic")
    assert first == second
    assert [v["id"] for v in first["variants"]] == [f"v{i:02d}" for i in range(1, 17)]
    assert all(len(v["suggestions"]) == 12 for v in first["variants"])


def test_heuristic_does_not_touch_the_network_or_the_client(monkeypatch):
    _no_keys(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "present-but-unused")

    def explode(*args, **kwargs):
        raise AssertionError("heuristic rewriting must not open a network connection")

    def no_client(model=None):
        raise AssertionError("heuristic rewriting must not build an LLM client")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    monkeypatch.setattr("clipcut.llm_persona.default_client", no_client)

    class Boom:
        def complete(self, prompt):
            raise AssertionError("heuristic mode must ignore llm_client")

    rewrite_hooks(manifests=[V07], mode="heuristic", llm_client=Boom())
    braces = rewrite_hooks(manifests=[{"hook_text": "Keep {this} brace"}], personas=["casual_browser"], n=1, mode="heuristic")
    assert "{this}" in braces["variants"][0]["suggestions"][0]["hook"]


def test_request_errors(monkeypatch):
    _no_keys(monkeypatch)
    cases = [
        (dict(run="v2_niosh_multi", manifests=[V07]), 400),
        (dict(), 400),
        (dict(manifests=[V07], mode="trained"), 400),
        (dict(manifests=[V07], personas="fast_swiper"), 400),
        (dict(manifests=[V07], personas=["viewer"]), 400),
        (dict(manifests=[V07], personas=["fast_swiper", "fast_swiper"]), 400),
        (dict(manifests=[V07], personas=[]), 400),
        (dict(manifests=[V07], channels=[]), 400),
        (dict(manifests=[V07], channels=["myspace"]), 400),
        (dict(manifests=[V07], channels=["TikTok", "tiktok"]), 400),
        (dict(manifests=[V07], n=0), 400),
        (dict(manifests=[V07], n=5), 400),
        (dict(manifests=[V07], n=1.5), 400),
        (dict(manifests=[V07], n=True), 400),
        (dict(run="../clipcut"), 400),
        (dict(run="does_not_exist"), 404),
        (dict(run="v2_niosh_multi", variant_ids=["missing"]), 404),
        (dict(manifests=[V07], mode="llm"), 503),
    ]
    for kwargs, status in cases:
        with pytest.raises(PersonaScoreError) as caught:
            rewrite_hooks(**kwargs)
        assert caught.value.status == status, kwargs


def test_llm_suggestions_are_rescored_and_gaps_fall_back(monkeypatch):
    _no_keys(monkeypatch)
    seen = {}

    class Client:
        def complete(self, prompt):
            seen["prompt"] = prompt
            return "```json\n" + json.dumps({"suggestions": [
                {
                    "persona": "fast_swiper",
                    "hook": "Wait — there's just no price you can put on that.",
                    "title_card": "NO PRICE",
                    "reason": "simulated open",
                    "predicted_delta": 0.99,
                },
                {
                    "persona": "casual_browser",
                    "hook": "Did you know there's just no price you can put on that?",
                    "reason": "This line was validated in production",
                },
                {
                    "persona": "completionist",
                    "hook": "Here's why: there's just no price you can put on that.",
                    "title_card": "PRICE",
                    "reason": "informative frame",
                },
                {
                    "persona": "sharer_engager",
                    "hook": "The truth: there's just no price you can put on that.",
                    "reason": "not a validated measurement",
                },
            ]}) + "\n```"

    out = rewrite_hooks(manifests=[V07], n=1, mode="auto", llm_client=Client())
    assert out["source"] == "llm"
    assert out["model_version"] == "llm-hook-v1"
    assert "There's just no price you can put on that." in seen["prompt"]
    assert "Do not invent" in seen["prompt"]
    assert "predicted_delta" in seen["prompt"]
    by_id = {s["persona"]: s for s in out["variants"][0]["suggestions"]}
    assert by_id["fast_swiper"]["origin"] == "llm"
    assert by_id["fast_swiper"]["predicted_delta"] != 0.99
    assert by_id["fast_swiper"]["predicted_delta"] == predicted_delta(V07, "fast_swiper", by_id["fast_swiper"]["hook"])
    assert by_id["fast_swiper"]["predicted_delta_label"] == "heuristic_estimate"
    assert "heuristic rescore" in by_id["fast_swiper"]["reason"]
    assert "production" not in by_id["casual_browser"]["reason"]
    assert "Not a trained or validated line" in by_id["casual_browser"]["reason"]
    assert "validated" in by_id["sharer_engager"]["reason"]
    assert by_id["casual_browser"]["title_card"] == "NO PRICE"
    assert by_id["completionist"]["title_card"] == "PRICE"

    class Partial:
        def complete(self, prompt):
            return json.dumps({"suggestions": [{
                "persona": "fast_swiper",
                "hook": "Wait — did you know there's just no price you can put on that?",
                "reason": "only one slot",
            }]})

    mixed = rewrite_hooks(manifests=[V07], personas=["fast_swiper"], n=2, mode="llm", llm_client=Partial())
    assert mixed["source"] == "mixed"
    assert mixed["model_version"] == "hook-mixed-v1"
    origins = [s["origin"] for s in mixed["variants"][0]["suggestions"]]
    assert origins.count("llm") == 1 and origins.count("heuristic") == 1
    assert len({s["hook"] for s in mixed["variants"][0]["suggestions"]}) == 2

    class Garbage:
        def complete(self, prompt):
            return "I would rewrite it, but not as JSON."

    fallen = rewrite_hooks(manifests=[V07], mode="auto", llm_client=Garbage())
    assert fallen == rewrite_hooks(manifests=[V07], mode="heuristic")

    class Boom:
        def complete(self, prompt):
            raise RuntimeError("network down")

    assert rewrite_hooks(manifests=[V07], mode="llm", llm_client=Boom())["source"] == "heuristic"
    assert parse_rewrites("nope") is None
    assert parse_rewrites('{"suggestions": []}') is None


def test_auto_uses_default_client_only_when_a_key_is_set(monkeypatch):
    _no_keys(monkeypatch)
    calls = []

    class Client:
        def complete(self, prompt):
            calls.append(prompt)
            assert "Keep {braces}" in prompt
            return json.dumps({"suggestions": [{
                "persona": "casual_browser",
                "hook": "Did you know to keep {braces} in the line?",
                "reason": "simulated",
            }]})

    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr("clipcut.llm_persona.default_client", lambda model=None: Client())
    out = rewrite_hooks(manifests=[{"hook_text": "Keep {braces} in the line"}], personas=["casual_browser"], n=1, mode="auto")
    assert out["source"] == "llm"
    assert calls and "casual_browser" in calls[0]


def test_http_contract(monkeypatch):
    _no_keys(monkeypatch)
    from fastapi.testclient import TestClient
    from app import app

    client = TestClient(app)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/persona/hooks" in paths and "/api/persona/score" in paths and "/api/score" in paths

    ok = client.post("/api/persona/hooks", json={"run": "v2_niosh_multi", "variant_ids": ["v07"], "mode": "heuristic"})
    assert ok.status_code == 200
    body = ok.json()
    assert body == rewrite_hooks(run="v2_niosh_multi", variant_ids=["v07"], mode="heuristic")
    assert "winner" not in body

    nulled = client.post("/api/persona/hooks", json={"manifests": [V07], "n": None, "mode": None, "personas": ["sharer_engager"]})
    assert nulled.status_code == 200
    assert len(nulled.json()["variants"][0]["suggestions"]) == 3

    bad = client.post("/api/persona/hooks", json={})
    assert bad.status_code == 400 and bad.json()["error"] and "detail" not in bad.json()
    missing = client.post("/api/persona/hooks", json={"run": "nope", "mode": "heuristic"})
    assert missing.status_code == 404
    needs_key = client.post("/api/persona/hooks", json={"manifests": [V07], "mode": "llm"})
    assert needs_key.status_code == 503 and "XAI_API_KEY" in needs_key.json()["error"]
    junk = client.post("/api/persona/hooks", content=b"not-json", headers={"content-type": "application/json"})
    assert junk.status_code == 400
    listed = client.post("/api/persona/hooks", json=["nope"])
    assert listed.status_code == 400 and listed.json()["error"]

    score = client.post("/api/persona/score", json={"manifests": [V07], "mode": "heuristic"})
    assert score.status_code == 200 and score.json()["variants"][0]["id"] == "v07"
