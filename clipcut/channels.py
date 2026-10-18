"""Channel specs + moment picking for the clipper (owner: retention agent). Pure functions, no I/O.
Specs are hand-collected best practice / platform limits as of 2026 (verify before publishing; platforms change limits)."""
import re

CHANNEL_SPECS = {
    "TikTok":   dict(audience=["fast_swiper"], platform="TikTok", aspect=["9:16"], resolution=(1080, 1920), max_s=600, ideal_s=(15, 34),
                     captions="burned-in, large, lower-middle; avoid bottom ~20% (caption/UI) and right ~15% (buttons)",
                     safe_zone=dict(top=0.10, bottom=0.20, left=0.05, right=0.15), notes="hook <=3 s; loopable endings help"),
    "Reels":    dict(audience=["casual_browser"], platform="Reels", aspect=["9:16"], resolution=(1080, 1920), max_s=180, ideal_s=(15, 45),
                     captions="burned-in; keep text in center 4:5 area (grid crop shows 4:5/1:1)",
                     safe_zone=dict(top=0.14, bottom=0.20, left=0.05, right=0.12), notes="cover frame shown cropped in profile grid"),
    "Shorts":   dict(audience=["fast_swiper", "casual_browser"], platform="Shorts", aspect=["9:16"], resolution=(1080, 1920), max_s=180, ideal_s=(20, 50),
                     captions="burned-in; avoid bottom ~25% (title/channel bar) and right edge buttons",
                     safe_zone=dict(top=0.10, bottom=0.25, left=0.05, right=0.15), notes="loops automatically; search/title matter"),
    "X":        dict(audience=["sharer_engager"], platform="X", aspect=["16:9", "1:1"], resolution=(1280, 720), max_s=140, ideal_s=(15, 45),
                     captions="burned-in (autoplay muted); upload .srt too", safe_zone=dict(top=0.05, bottom=0.10, left=0.05, right=0.05),
                     notes="pair with a strong claim in post text; 1:1 takes more feed space on mobile"),
    "LinkedIn": dict(audience=["completionist"], platform="LinkedIn", aspect=["1:1", "4:5"], resolution=(1080, 1350), max_s=600, ideal_s=(30, 90),
                     captions="burned-in + uploaded SRT; most views are muted", safe_zone=dict(top=0.05, bottom=0.10, left=0.05, right=0.05),
                     notes="informative/expert framing; first line of post text is the hook"),
    "YouTube":  dict(audience=["completionist"], platform="YouTube", aspect=["16:9"], resolution=(1920, 1080), max_s=43200, ideal_s=(60, 600),
                     captions="upload SRT/auto-captions; burned-in optional", safe_zone=dict(top=0.05, bottom=0.12, left=0.05, right=0.05),
                     notes="long-form; first 30 s retention matters; thumbnail + title do the hooking"),
    "IG_Feed":  dict(audience=["casual_browser"], platform="Instagram", aspect=["4:5", "1:1"], resolution=(1080, 1350), max_s=180, ideal_s=(15, 60),
                     captions="burned-in; muted autoplay", safe_zone=dict(top=0.05, bottom=0.10, left=0.05, right=0.05), notes="4:5 maximizes feed area"),
    "IG_Stories": dict(audience=["fast_swiper"], platform="Instagram", aspect=["9:16"], resolution=(1080, 1920), max_s=60, ideal_s=(5, 15),
                     captions="burned-in; avoid top ~14% (progress bar/profile) and bottom ~20% (reply bar)",
                     safe_zone=dict(top=0.14, bottom=0.20, left=0.05, right=0.05), notes="tap-forward is the swipe; 15 s per frame segment"),
    "FB_Reels": dict(audience={"casual_browser": 0.6, "completionist": 0.4}, platform="Facebook", aspect=["9:16"], resolution=(1080, 1920), max_s=90, ideal_s=(20, 60),
                     share_weight=1.5, views_scale=0.6,  # hand-set: older audience shares more, smaller reach than IG Reels
                     captions="burned-in; avoid bottom ~25% and right buttons", safe_zone=dict(top=0.10, bottom=0.25, left=0.05, right=0.15),
                     notes="older audience skew than TikTok"),
    "FB_Feed":  dict(audience=["completionist", "sharer_engager"], platform="Facebook", aspect=["1:1", "4:5"], resolution=(1080, 1350), max_s=600, ideal_s=(30, 90),
                     captions="burned-in + SRT; muted autoplay", safe_zone=dict(top=0.05, bottom=0.10, left=0.05, right=0.05),
                     notes="shares to groups/friends drive reach"),
}

_Q = re.compile(r"\?\s*$|^(what|why|how|who|when|where|did you|do you|have you|is it|can you)\b", re.I)
_NUM = re.compile(r"\b\d+(\.\d+)?%?|\b(one|two|three|five|ten|hundred|thousand|million|billion|first|top)\b", re.I)
_CLAIM = re.compile(r"\b(never|always|most|best|worst|secret|mistake|nobody|everyone|truth|actually|wrong|stop|only|biggest|"
                    r"surprising|shocking|you need|the reason|here's why|don't)\b", re.I)
_YOU = re.compile(r"\byou(r)?\b", re.I)

def hook_strength(text, energy=None, face=None):
    """Score (0..~1+) how well a sentence works as an opening hook, plus reasons.
    text: sentence; energy: optional loudness z-score near it; face: optional face fraction 0..1."""
    s, why = 0.0, []
    t = text.strip()
    if _Q.search(t): s += 0.30; why.append("question")
    if _NUM.search(t): s += 0.20; why.append("number")
    n = len(_CLAIM.findall(t))
    if n: s += min(0.30, 0.15 * n); why.append("strong claim")
    if _YOU.search(t): s += 0.10; why.append("addresses 'you'")
    w = len(t.split())
    if 5 <= w <= 20: s += 0.10; why.append("punchy length")
    elif w > 35: s -= 0.10
    if energy is not None and energy > 0.5: s += min(0.2, 0.1 * energy); why.append("energy peak")
    if face is not None and face > 0.5: s += 0.10; why.append("face on screen")
    return round(s, 3), why

def _seg(x):
    if isinstance(x, dict): return float(x.get("start", x.get("t0"))), float(x.get("end", x.get("t1"))), x.get("text", "")
    return float(x[0]), float(x[1]), x[2]

def _feat_at(features, t0, t1, key):
    """features: optional dict with per-time arrays, e.g. {'times':[...], 'loudness':[...], 'face':[...]} (any subset)."""
    if not features or key not in features or "times" not in features: return None
    v = [x for t, x in zip(features["times"], features[key]) if t0 <= t <= t1]
    return sum(v) / len(v) if v else None

def pick_moments(transcript_segments, features=None, n=5, min_len=15.0, max_len=45.0):
    """Pick up to n distinct, non-overlapping source spans for clips, ranked by hook strength.
    transcript_segments: list of {start,end,text} (or (start,end,text)) in source seconds, sentence-level preferred.
    features: optional {'times':[s...], 'loudness':[z...], 'face':[0..1...]} time series; loudness should be z-scored.
    Each span starts at a hook sentence and extends over following sentences until >=min_len (stops before max_len).
    Returns [{src_start, src_end, duration_s, hook_text, hook_score, reasons, rank}] sorted by hook_score desc."""
    segs = sorted((_seg(s) for s in transcript_segments), key=lambda s: s[0])
    cands = []
    for i, (a, b, txt) in enumerate(segs):
        hs, why = hook_strength(txt, _feat_at(features, a, b, "loudness"), _feat_at(features, a, b, "face"))
        end = b; j = i
        while end - a < min_len and j + 1 < len(segs) and segs[j + 1][1] - a <= max_len: j += 1; end = segs[j][1]
        if end - a < min_len * 0.6: continue  # not enough material
        body = [hook_strength(segs[k][2])[0] for k in range(i + 1, j + 1)]
        total = hs + 0.2 * (sum(body) / len(body) if body else 0)
        cands.append(dict(src_start=round(a, 3), src_end=round(end, 3), duration_s=round(end - a, 2), hook_text=txt.strip(),
                          hook_score=round(total, 3), reasons=why))
    out = []
    for c in sorted(cands, key=lambda c: -c["hook_score"]):
        if all(c["src_end"] <= o["src_start"] or c["src_start"] >= o["src_end"] for o in out): out.append(c)
        if len(out) >= n: break
    for r, c in enumerate(out, 1): c["rank"] = r
    return out

def list_channels():
    """Channel specs for the UI: [{name, platform, audience, aspect, resolution, max_s, ideal_s, captions, safe_zone, notes}].
    Add a channel by adding one CHANNEL_SPECS entry (needs audience, aspect, max_s, ideal_s).
    audience: list of personas (equal weight) or {persona: weight}. Optional hand-set knobs (default 1.0):
    share_weight (multiplies simulated shares and the share term of the channel score), views_scale (multiplies simulated views)."""
    return [dict(name=k, **v) for k, v in CHANNEL_SPECS.items()]

def fit_for_channel(duration_s, channel):
    """'ideal' | 'ok' | 'too_long' for a duration on a channel."""
    s = CHANNEL_SPECS[channel]; lo, hi = s["ideal_s"]
    return "too_long" if duration_s > s["max_s"] else "ideal" if lo <= duration_s <= hi else "ok"
