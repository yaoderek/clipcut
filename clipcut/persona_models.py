"""Rule-based audience personas for short-form variants.

Scores are hand-set heuristics: each persona has interpretable feature weights
(hook timing, pacing, captions, length versus a platform norm, and so on).
Nothing here is trained, and nothing is a real A/B or platform result.
Coefficients live in PERSONAS below. See docs/PERSONA_API.md.
"""
from __future__ import annotations

import re

from .channels import CHANNEL_SPECS, fit_for_channel, hook_strength

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

PERSONA_ORDER = ("fast_swiper", "casual_browser", "completionist", "sharer_engager")

# base + sum(weight * quality). quality is in [-1, 1]. Bases are hand-set.
PERSONAS = {
    "fast_swiper": dict(
        label="Fast swiper",
        description="Younger swipe-feed viewer (TikTok / Stories, roughly 16-24). Leaves in the first seconds unless the hook is immediate and the cut rate stays high.",
        weight=0.30,
        base=0.40,
        interests=("trend", "hack", "secret", "wait", "pov", "challenge", "storytime"),
        sweet=(15.0, 30.0),
        max_ok=40.0,
        hook_deadline=2.5,
        pace=(1.0, 3.0),
        no_cut=-1.0,
        loop_miss=-0.35,
        weights=dict(hook_timing=0.16, hook_strength=0.10, pacing=0.12, captions=0.06, length=0.10, topic_match=0.04, loop=0.05, silence=0.05, hook_title=0.02),
    ),
    "casual_browser": dict(
        label="Casual browser",
        description="Lean-back Reels / Shorts viewer (roughly 18-34), often watching muted. Captions and a mid-length clip matter more than a frantic cut rate.",
        weight=0.30,
        base=0.46,
        interests=("story", "day", "life", "relatable", "family", "mom", "work", "you"),
        sweet=(20.0, 45.0),
        max_ok=60.0,
        hook_deadline=3.5,
        pace=(2.0, 4.5),
        no_cut=-0.55,
        loop_miss=-0.15,
        weights=dict(hook_timing=0.10, hook_strength=0.08, pacing=0.07, captions=0.14, length=0.10, topic_match=0.05, loop=0.04, silence=0.04, hook_title=0.04),
    ),
    "completionist": dict(
        label="Completionist",
        description="Will finish a longer informative clip (LinkedIn / YouTube, roughly 25-44). Tolerates a slower open and longer shots; drops off when the clip runs long.",
        weight=0.30,
        base=0.52,
        interests=("research", "learn", "career", "study", "health", "data", "because", "work"),
        sweet=(30.0, 60.0),
        max_ok=90.0,
        hook_deadline=6.0,
        pace=(3.0, 8.0),
        no_cut=-0.15,
        loop_miss=0.0,
        weights=dict(hook_timing=0.06, hook_strength=0.08, pacing=0.05, captions=0.08, length=0.12, topic_match=0.10, loop=0.02, silence=0.04, hook_title=0.03),
    ),
    "sharer_engager": dict(
        label="Sharer",
        description="Forwards a claim-shaped, loopable clip (X / Facebook, roughly 18-40). The hook line and a loop weigh more than raw pacing.",
        weight=0.10,
        base=0.48,
        interests=("truth", "never", "always", "share", "wrong", "stop", "actually", "secret"),
        sweet=(20.0, 45.0),
        max_ok=60.0,
        hook_deadline=4.0,
        pace=(2.0, 5.0),
        no_cut=-0.45,
        loop_miss=-0.30,
        weights=dict(hook_timing=0.10, hook_strength=0.14, pacing=0.05, captions=0.08, length=0.08, topic_match=0.08, loop=0.08, silence=0.03, hook_title=0.04),
    ),
}

_STYLE_Q = {"word_pop": 1.0, "karaoke": 0.75, "sentence": 0.50, "minimal": 0.20, "none": -1.0}
_ASPECTS = (("9:16", 9 / 16), ("4:5", 4 / 5), ("1:1", 1.0), ("16:9", 16 / 9))


def _clamp(x, lo=-1.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def _duration(m):
    for key in ("duration_s", "length_s", "duration"):
        if m.get(key) not in (None, ""):
            try:
                return float(m[key])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def manifest_aspect(m):
    """Explicit aspect, else recipe.aspect, else nearest standard ratio to resolution [w, h]. No ffprobe."""
    rec = m.get("recipe") if isinstance(m.get("recipe"), dict) else {}
    a = m.get("aspect") or rec.get("aspect")
    if a:
        return str(a)
    r = m.get("resolution")
    if isinstance(r, (list, tuple)) and len(r) == 2:
        try:
            w, h = float(r[0]), float(r[1])
        except (TypeError, ValueError):
            return None
        if w <= 0 or h <= 0:
            return None
        x = w / h
        return min(_ASPECTS, key=lambda t: abs(x - t[1]))[0]
    return None


def _hook_end(m):
    for key in ("hook_end_s", "hook_end"):
        if m.get(key) not in (None, ""):
            try:
                return float(m[key])
            except (TypeError, ValueError):
                return None
    spans = m.get("spans")
    if isinstance(spans, list) and spans and isinstance(spans[0], (list, tuple)) and len(spans[0]) >= 2:
        try:
            return float(spans[0][1]) - float(spans[0][0])
        except (TypeError, ValueError):
            return None
    return None


def _hook_timing(m, deadline):
    h = _hook_end(m)
    if h is None:
        return 0.0, None
    if deadline <= 0:
        q, reason = 0.0, None
    elif h <= deadline:
        q = 0.35 + 0.65 * (1 - h / deadline)
        reason = f"hook lands by {h:.1f}s"
    elif h <= deadline + 2.5:
        q, reason = -0.45, f"hook late at {h:.1f}s (deadline {deadline:.1f}s)"
    else:
        q, reason = -1.0, f"hook at {h:.1f}s, well past {deadline:.1f}s"
    bnd = m.get("hook_boundary")
    if reason and bnd == "word":
        reason += " (cut mid-phrase)"
    elif reason and bnd:
        reason += f" (cut on {bnd})"
    return q, reason


def _hook_line(m):
    text = (m.get("hook_text") or "").strip()
    explicit = "hook_strength" in m or "hook_score" in m
    if explicit:
        try:
            hs = float(m["hook_strength"] if "hook_strength" in m else m["hook_score"])
        except (TypeError, ValueError):
            hs = None
    else:
        hs = None
    why = []
    if hs is None and text:
        hs, why = hook_strength(text)
    elif text:
        _, why = hook_strength(text)
    if hs is None:
        return 0.0, None
    q = _clamp((float(hs) - 0.25) / 0.45)
    detail = ", ".join(why) if why else f"strength {float(hs):.2f}"
    return q, f"hook line: {detail}"


def _pacing_inputs(m, dur):
    """Return (gaps, cut_count). cut_count 0 means a known absence of cuts. None gaps means unknown."""
    raw = m["cut_times_s"] if isinstance(m.get("cut_times_s"), list) else m.get("cuts") if isinstance(m.get("cuts"), list) else None
    if raw is not None:
        try:
            cuts = sorted(float(t) for t in raw)
        except (TypeError, ValueError):
            return None, None
        if dur <= 0:
            return None, len(cuts)
        gaps = [b - a for a, b in zip([0.0, *cuts], [*cuts, dur]) if b - a > 1e-6]
        return gaps, len(cuts)
    if m.get("cuts_per_min") not in (None, ""):
        try:
            cpm = float(m["cuts_per_min"])
        except (TypeError, ValueError):
            return None, None
        if cpm <= 0:
            return ([dur] if dur > 0 else None), 0
        return [60.0 / cpm], None
    if m.get("n_cuts") not in (None, ""):
        try:
            n = int(m["n_cuts"])
        except (TypeError, ValueError):
            return None, None
        if dur <= 0:
            return None, n
        if n <= 0:
            return [dur], 0
        return [dur / (n + 1)], n
    return None, None


def _pacing(m, dur, window, no_cut):
    gaps, n_cuts = _pacing_inputs(m, dur)
    if not gaps:
        return 0.0, None
    lo, hi = window
    if n_cuts == 0:
        return no_cut, "no cuts"
    frac = sum(1 for g in gaps if lo <= g <= hi) / len(gaps)
    q = _clamp(2 * frac - 1)
    if max(gaps) > hi * 2.5:
        q = _clamp(q - 0.35)
    return q, f"{frac:.0%} of shots in {lo:.0f}-{hi:.0f}s"


def _caption_style(m):
    rec = m.get("recipe") if isinstance(m.get("recipe"), dict) else {}
    if isinstance(rec.get("captions"), str):
        return rec["captions"].lower()
    if isinstance(m.get("caption_style"), str):
        return m["caption_style"].lower()
    caps = m.get("captions")
    if isinstance(caps, list) and caps:
        return "sentence"
    if "captions" in m or "caption_style" in m or "captions" in rec:
        return "none"
    return None


def _captions(m):
    style = _caption_style(m)
    if style is None:
        return 0.0, None
    q = _STYLE_Q.get(style, 0.4 if style != "none" else -1.0)
    caps = m.get("captions") if isinstance(m.get("captions"), list) else []
    lengths = []
    for c in caps:
        if isinstance(c, dict) and c.get("text"):
            lengths.append(len(str(c["text"]).split()))
        elif isinstance(c, str) and c.strip():
            lengths.append(len(c.split()))
    if lengths and (sum(lengths) / len(lengths)) > 10:
        avg = sum(lengths) / len(lengths)
        return _clamp(q - 0.35), f"captions ({style}) run long (~{avg:.0f} words)"
    if style == "none":
        return q, "no captions (muted autoplay)"
    if style == "word_pop":
        return q, "captions: word_pop, short on-screen cues"
    return q, f"captions: {style}"


def _length_quality(dur, lo, hi, max_ok):
    if dur <= 0:
        return 0.0, "duration unknown"
    if lo <= dur <= hi:
        mid = (lo + hi) / 2
        half = (hi - lo) / 2 or 1
        return 1.0 - 0.25 * abs(dur - mid) / half, f"{dur:.0f}s inside {lo:.0f}-{hi:.0f}s"
    if dur < lo:
        return _clamp(0.2 - 0.8 * (lo - dur) / lo), f"{dur:.0f}s shorter than {lo:.0f}-{hi:.0f}s"
    if max_ok > hi and dur <= max_ok:
        return -0.5 * (dur - hi) / (max_ok - hi), f"{dur:.0f}s a bit long for {lo:.0f}-{hi:.0f}s"
    return -1.0, f"{dur:.0f}s past {max_ok:.0f}s"


def _length(m, dur, persona):
    q, reason = _length_quality(dur, persona["sweet"][0], persona["sweet"][1], persona["max_ok"])
    if dur <= 0:
        return 0.0, None
    channel = _channel_name(m)
    spec = CHANNEL_SPECS.get(channel) if channel else None
    if spec:
        lo, hi = spec["ideal_s"]
        cq, cr = _length_quality(dur, float(lo), float(hi), float(spec["max_s"]))
        q = (q + cq) / 2
        reason = f"{reason}; {channel} norm {cr}"
    return _clamp(q), reason


def _blob(m):
    parts = []
    if m.get("hook_text"):
        parts.append(str(m["hook_text"]))
    if m.get("transcript"):
        parts.append(str(m["transcript"]))
    caps = m.get("captions")
    if isinstance(caps, list):
        for c in caps:
            if isinstance(c, dict) and c.get("text"):
                parts.append(str(c["text"]))
            elif isinstance(c, str):
                parts.append(c)
    return " ".join(parts).lower()


def _topic(m, interests):
    blob = _blob(m)
    if not blob.strip():
        return 0.0, None
    hits = [w for w in interests if re.search(rf"\b{re.escape(w)}\b", blob)]
    if not hits:
        return -0.25, "topic does not match this persona's interests"
    return min(1.0, len(hits) / 2), "topic touches " + ", ".join(hits[:3])


def _loop(m, miss):
    rec = m.get("recipe") if isinstance(m.get("recipe"), dict) else {}
    if m.get("loop_end") or rec.get("ending") == "loop":
        return 1.0, "loop-friendly ending"
    if "loop_end" in m or rec.get("ending"):
        return miss, "ending does not loop"
    return 0.0, None


def _silence(m):
    if "silence_removed_s" not in m and "silence_removed" not in m:
        return 0.0, None
    try:
        s = float(m.get("silence_removed_s") if "silence_removed_s" in m else m.get("silence_removed") or 0)
    except (TypeError, ValueError):
        return 0.0, None
    if s > 0:
        return min(1.0, s / 2.0), f"{s:.1f}s of dead air removed"
    return -0.35, "dead air not trimmed"


def _hook_title(m):
    rec = m.get("recipe") if isinstance(m.get("recipe"), dict) else {}
    if "hook_title" in rec or "hook_title" in m:
        on = rec["hook_title"] if "hook_title" in rec else m["hook_title"]
        return (0.8, "hook title overlay") if on else (-0.2, "no hook title")
    return 0.0, None


def _channel_name(m):
    rec = m.get("recipe") if isinstance(m.get("recipe"), dict) else {}
    name = m.get("channel") or rec.get("channel")
    return name if isinstance(name, str) and name in CHANNEL_SPECS else None


def _qualities(m, persona):
    dur = _duration(m)
    return {
        "hook_timing": _hook_timing(m, persona["hook_deadline"]),
        "hook_strength": _hook_line(m),
        "pacing": _pacing(m, dur, persona["pace"], persona["no_cut"]),
        "captions": _captions(m),
        "length": _length(m, dur, persona),
        "topic_match": _topic(m, persona["interests"]),
        "loop": _loop(m, persona["loop_miss"]),
        "silence": _silence(m),
        "hook_title": _hook_title(m),
    }


def score_persona(manifest, persona_id):
    """One persona. contributions are signed points; score = clamp(base + sum(contributions), 0, 1)."""
    persona = PERSONAS[persona_id]
    quals = _qualities(manifest if isinstance(manifest, dict) else {}, persona)
    contributions = {k: round(persona["weights"][k] * quals[k][0], 4) for k in FEATURE_KEYS}
    raw = round(persona["base"] + sum(contributions.values()), 4)
    score = 0.0 if raw < 0 else 1.0 if raw > 1 else raw
    ranked = []
    for k in FEATURE_KEYS:
        reason = quals[k][1]
        if reason and abs(contributions[k]) > 0:
            ranked.append((abs(persona["weights"][k] * quals[k][0]), k, reason))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    reasons = [t[2] for t in ranked[:4]] or ["no strong edit signals in the manifest"]
    return dict(
        id=persona_id,
        label=persona["label"],
        description=persona["description"],
        score=score,
        reasons=reasons,
        contributions=contributions,
    )


def heuristic_variant(manifest):
    """Score one variant for every persona. aggregate is the hand-set audience-weighted mean."""
    m = manifest if isinstance(manifest, dict) else {}
    personas = [score_persona(m, pid) for pid in PERSONA_ORDER]
    scores = {p["id"]: p["score"] for p in personas}
    weight = sum(PERSONAS[pid]["weight"] for pid in PERSONA_ORDER)
    aggregate = round(sum(PERSONAS[p["id"]]["weight"] * p["score"] for p in personas) / weight, 4)
    return dict(source="heuristic", aggregate=aggregate, personas=personas, channels=channel_fits(scores, m))


def _audience(spec):
    aud = spec["audience"]
    if isinstance(aud, dict):
        total = sum(float(v) for v in aud.values()) or 1.0
        return {k: float(v) / total for k, v in aud.items()}
    return {k: 1.0 / len(aud) for k in aud}


def channel_fits(persona_scores, manifest):
    """Heuristic channel match in [0, 1], best fit first. Not a view prediction."""
    m = manifest if isinstance(manifest, dict) else {}
    aspect = manifest_aspect(m)
    dur = _duration(m)
    out = []
    for name, spec in CHANNEL_SPECS.items():
        mix = _audience(spec)
        known = {k: w for k, w in mix.items() if k in persona_scores}
        mix_w = sum(known.values()) or 1.0
        audience_score = sum(w * persona_scores[k] for k, w in known.items()) / mix_w
        if aspect is None:
            fmt, fmt_reason = 1.0, "aspect not specified"
        elif aspect in spec["aspect"]:
            fmt, fmt_reason = 1.0, f"aspect {aspect} is accepted"
        else:
            fmt, fmt_reason = 0.6, f"aspect {aspect} is not in {list(spec['aspect'])}"
        lo, hi = spec["ideal_s"]
        if dur <= 0:
            dm, dreason = 1.0, "duration unknown"
        else:
            bucket = fit_for_channel(dur, name)
            if bucket == "ideal":
                dm, dreason = 1.0, f"{dur:.0f}s is inside the {lo:.0f}-{hi:.0f}s norm"
            elif bucket == "ok":
                dm, dreason = 0.85, f"{dur:.0f}s is outside the {lo:.0f}-{hi:.0f}s norm but within {spec['max_s']:.0f}s"
            else:
                dm, dreason = 0.3, f"{dur:.0f}s is past the {spec['max_s']:.0f}s max"
        fit = audience_score * fmt * dm
        fit = round(0.0 if fit < 0 else 1.0 if fit > 1 else fit, 4)
        who = ", ".join(f"{k} {known[k]:.2f}" if isinstance(spec["audience"], dict) else k for k in known)
        out.append(dict(id=name.lower(), label=name, fit=fit, reasons=[fmt_reason, dreason, f"audience mix: {who}"]))
    out.sort(key=lambda c: (-c["fit"], c["id"]))
    return out
