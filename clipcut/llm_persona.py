"""Optional LLM-simulated persona judgments.

When XAI_API_KEY or GROK_API_KEY is set, an LLM role-plays one audience persona
and returns structured JSON about a variant's transcript and metadata. This is
a simulated judgment, not a trained model and not a real viewer.

With no key, or if the HTTP call or the reply fails, rate() returns None.
Callers keep the deterministic heuristic in persona_models.py. This module
does not raise into the scoring pipeline.

v1 manifests (length_s, jump_cuts, caption_style) and cutter-v2 vNN.json
(duration_s, cut_times_s, recipe.captions) both work. Missing keys are omitted
from the brief instead of raising KeyError.
"""
from __future__ import annotations

import json
import os
import urllib.request

API_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = "grok-4"
DEFAULT_TIMEOUT = 30

FEATURE_KEYS = (
    "hook_timing",
    "hook_strength",
    "pacing",
    "captions",
    "length",
    "topic_match",
    "loop",
    "silence",
    "hook_title",
)

PROMPT = """You are role-playing a short-form video viewer. This is a simulated judgment, not a measurement of real viewers, and you cannot watch the video.

Persona:
{persona}

Variant (metadata and transcript excerpt only):
{variant}

Reply with ONLY a JSON object, no markdown, using exactly this shape:
{{"score": <float 0-1 how likely this persona is to keep watching and to share>, "reasons": [<one to three short strings>], "contributions": {{{contribs}}}}}

contributions are signed attributions for those features. They do not have to sum to score. Use 0 when the brief does not say."""


def available():
    return bool(os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY"))


class XAIClient:
    """Minimal chat-completions client. Tests pass any object with complete(prompt) -> str."""

    def __init__(self, api_key, model=DEFAULT_MODEL, timeout=DEFAULT_TIMEOUT, url=API_URL):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.url = url

    def complete(self, prompt):
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.load(resp)
        return payload["choices"][0]["message"]["content"]


def default_client(model=None):
    key = os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
    if not key:
        return None
    chosen = model or os.environ.get("XAI_MODEL") or DEFAULT_MODEL
    try:
        timeout = float(os.environ.get("CLIPCUT_LLM_TIMEOUT", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return XAIClient(key, chosen, timeout=timeout)


def variant_brief(variant):
    """Text features only. Tolerates v1 and v2 manifests; never KeyErrors on missing fields."""
    if not isinstance(variant, dict):
        return {"note": "unstructured variant"}
    rec = variant.get("recipe") if isinstance(variant.get("recipe"), dict) else {}
    dur = variant.get("duration_s", variant.get("length_s", variant.get("duration")))
    cuts = variant.get("cut_times_s") if isinstance(variant.get("cut_times_s"), list) else variant.get("cuts") if isinstance(variant.get("cuts"), list) else None
    n_cuts = variant.get("n_cuts")
    if n_cuts is None and isinstance(cuts, list):
        n_cuts = len(cuts)
    style = rec.get("captions") if isinstance(rec.get("captions"), str) else variant.get("caption_style")
    excerpt = []
    caps = variant.get("captions") if isinstance(variant.get("captions"), list) else []
    for c in caps:
        if isinstance(c, dict) and c.get("text"):
            excerpt.append(str(c["text"]))
        elif isinstance(c, str):
            excerpt.append(c)
        if sum(len(x) for x in excerpt) > 500:
            break
    text = " ".join(excerpt)[:600] or None
    loop = variant.get("loop_end")
    if loop is None and rec.get("ending"):
        loop = rec.get("ending") == "loop"
    brief = {
        "variant_id": variant.get("variant_id") or variant.get("id"),
        "duration_s": dur,
        "hook_text": variant.get("hook_text"),
        "hook_end_s": variant.get("hook_end_s", variant.get("hook_end")),
        "n_cuts": n_cuts,
        "cuts_per_min": variant.get("cuts_per_min"),
        "jump_cuts": variant.get("jump_cuts"),
        "caption_style": style,
        "caption_excerpt": text,
        "hook_zoom": variant.get("hook_zoom"),
        "hook_title": rec.get("hook_title", variant.get("hook_title")),
        "loop_end": loop,
        "silence_removed_s": variant.get("silence_removed_s", variant.get("silence_removed")),
        "channel": variant.get("channel") or rec.get("channel"),
        "aspect": variant.get("aspect") or rec.get("aspect"),
    }
    return {k: v for k, v in brief.items() if v is not None}


def parse_judgment(text):
    """Pull a JSON object out of a model reply. None when it is not usable."""
    if not isinstance(text, str) or "{" not in text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "score" not in data and "swipe_away_in_3s_prob" not in data:
        return None
    return data


def judgment_score(data):
    """Map either the current JSON shape or the old swipe/watch/share shape onto 0-1. None if unusable."""
    if not isinstance(data, dict):
        return None
    if "score" in data:
        try:
            score = float(data["score"])
        except (TypeError, ValueError):
            return None
    elif "swipe_away_in_3s_prob" in data and "expected_watch_pct" in data:
        try:
            swipe = float(data["swipe_away_in_3s_prob"])
            watch = float(data["expected_watch_pct"]) / 100.0
        except (TypeError, ValueError):
            return None
        share = 1.0 if data.get("would_share") else 0.0
        score = 0.4 * (1 - swipe) + 0.4 * watch + 0.2 * share
    else:
        return None
    if score != score:  # NaN
        return None
    return 0.0 if score < 0 else 1.0 if score > 1 else score


def parse_rewrites(text):
    """Pull {"suggestions": [...]} out of a model reply. None when it is not usable."""
    if not isinstance(text, str) or "{" not in text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("suggestions")
    if not isinstance(rows, list) or not rows:
        return None
    if not any(isinstance(row, dict) and row.get("hook") for row in rows):
        return None
    return data


def rate(persona_desc, variant, model=None, client=None):
    """Role-play persona_desc on variant. Returns parsed JSON, or None when skipped or failed.

    Never raises. client.complete(prompt) -> str overrides the env-gated HTTP client
    (used by tests and by score_variants).
    """
    try:
        c = client if client is not None else default_client(model)
        if c is None:
            return None
        brief = variant_brief(variant if isinstance(variant, dict) else {})
        contribs = ", ".join(f'"{k}": <signed float>' for k in FEATURE_KEYS)
        prompt = PROMPT.format(persona=str(persona_desc), variant=json.dumps(brief, ensure_ascii=False, default=str), contribs=contribs)
        return parse_judgment(c.complete(prompt))
    except Exception:
        return None
