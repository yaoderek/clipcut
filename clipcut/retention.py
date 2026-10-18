"""Public scoring API (owner: retention agent).
score(manifest_path_or_dict) -> dict. Default = score_dummy(): a HEURISTIC, NOT TRAINED, rule-based persona scorer that
reads manifest fields only (no video decoding, <1 ms). The old KuaiRand/MicroLens model: set env CLIPCUT_SCORER=model.
All numbers are hand-set from common short-form best practice; they are NOT predictions learned from data."""
import json, os, math
from pathlib import Path

MODEL_VERSION = "dummy-heuristic-v0"
CONFIDENCE = "none - untrained heuristic"
LABEL = "heuristic, not trained"

# persona: base rates + preferences (all hand-set)
PERSONAS = {
    "fast_swiper":    dict(weight=0.30, base_swipe=0.55, base_watch=0.30, base_share=0.002, sweet=(15, 30), max_ok=40,
                           hook_w=2.0, pace_w=1.5, cap_w=1.0, silence_w=1.2, loop_w=1.0),
    "casual_browser": dict(weight=0.30, base_swipe=0.45, base_watch=0.40, base_share=0.003, sweet=(20, 45), max_ok=60,
                           hook_w=1.2, pace_w=1.0, cap_w=1.5, silence_w=1.0, loop_w=0.8),
    "completionist":  dict(weight=0.30, base_swipe=0.25, base_watch=0.60, base_share=0.004, sweet=(30, 60), max_ok=90,
                           hook_w=0.6, pace_w=0.5, cap_w=0.8, silence_w=0.5, loop_w=0.5),
    "sharer_engager": dict(weight=0.10, base_swipe=0.30, base_watch=0.55, base_share=0.012, sweet=(20, 45), max_ok=60,
                           hook_w=1.0, pace_w=0.8, cap_w=1.2, silence_w=0.6, loop_w=1.5),
}
CHANNEL_FIT = {  # persona -> (primary, secondary, reason)
    "fast_swiper": ("TikTok", "Shorts", "short attention, swipe feed; rewards a hook in <=3 s and 15-30 s length"),
    "casual_browser": ("Reels", "Shorts", "lean-back browsing; captions matter (often muted); 20-45 s"),
    "completionist": ("LinkedIn", "Shorts", "tolerates 45-90 s substantive clips; professional audience finishes informative content"),
    "sharer_engager": ("X", "TikTok", "shares/quote-posts strong claims; loopable, captioned clips travel"),
}

def _load(x):
    if isinstance(x, dict): return dict(x)
    p = str(x)
    if p.lower().endswith((".mp4", ".mov", ".webm", ".mkv")): return {"video_path": p, "variant_id": Path(p).stem}
    return json.load(open(p))

def _aspect(m):
    """Aspect string from manifest 'aspect' / recipe.aspect / resolution [w,h] / video dims (ffprobe metadata only, no decode)."""
    a = m.get("aspect") or (m.get("recipe") or {}).get("aspect")
    if a: return str(a)
    r = m.get("resolution")
    if isinstance(r, (list, tuple)) and len(r) == 2: w, h = r
    else:
        vp = m.get("video_path")
        if not vp or not Path(vp).exists(): return None
        import subprocess
        o = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", vp],
                           capture_output=True, text=True).stdout.strip().split(",")
        try: w, h = int(o[0]), int(o[1])
        except Exception: return None
    x = w / h; best = min(["9:16", "4:5", "1:1", "16:9"], key=lambda k: abs(x - eval(k.replace(":", "/"))))
    return best

def features(m):
    """Manifest-only edit features (tolerates alternate keys)."""
    dur = float(m.get("duration_s") or m.get("duration") or 0.0)
    cuts = sorted(float(t) for t in (m.get("cut_times_s") or m.get("cuts") or []))
    gaps = [b - a for a, b in zip([0.0] + cuts, cuts + [dur])] if dur else []
    caps = m.get("captions") or []
    rec = m.get("recipe") or {}
    hook = m.get("hook_end_s", m.get("hook_end"))
    mean_g = sum(gaps) / len(gaps) if gaps else 0
    cv = (sum((g - mean_g) ** 2 for g in gaps) / len(gaps)) ** 0.5 / mean_g if gaps and mean_g else None
    ht = m.get("hook_text") or ""
    hs = m.get("hook_strength", m.get("hook_score"))
    if hs is None and ht:
        from .channels import hook_strength; hs = hook_strength(ht)[0]
    cap_style = rec.get("captions") if isinstance(rec.get("captions"), str) else ("sentence" if caps else "none")
    return dict(duration_s=dur, n_cuts=len(cuts), max_gap_s=max(gaps) if gaps else None, gap_cv=cv,
                frac_gaps_2_4s=(sum(1 for g in gaps if 2 <= g <= 4) / len(gaps)) if gaps else 0.0,
                median_gap_s=sorted(gaps)[len(gaps) // 2] if gaps else None,
                captions=bool(caps) or cap_style not in (None, "none"), caption_style=cap_style,
                hook_end_s=float(hook) if hook is not None else None, hook_boundary=m.get("hook_boundary"),
                hook_strength=float(hs) if hs is not None else None, hook_text=ht[:120],
                silence_removed_s=float(m.get("silence_removed_s") or 0.0),
                loop_end=bool(m.get("loop_end") or rec.get("ending") == "loop"),
                hook_title=bool(rec.get("hook_title")), aspect=_aspect(m), channel=m.get("channel") or rec.get("channel"))

def _rules(f):
    """Each rule -> (name, quality in [-1,1], reason). Shared by all personas; personas weight them differently."""
    r = []
    h = f["hook_end_s"]
    if h is None: q, t = 0.0, "hook timing unknown"
    elif h <= 3: q, t = 0.4 + 0.6 * (3 - h) / 1.5, f"hook lands by {h:.2f}s"   # 3.0s -> 0.4, 1.5s -> 1.0
    elif h <= 5: q, t = -0.3, f"hook ends at {h:.1f}s (>3 s, slow open)"
    else: q, t = -1.0, f"hook ends at {h:.1f}s (well past 3 s)"
    bnd = f["hook_boundary"]; adj = {"pause": 0.3, "punct": 0.2, "word": -0.4}.get(bnd, 0.0)
    if bnd: t += f", cut at {bnd}" + (" (mid-phrase)" if bnd == "word" else "")
    r.append(("hook", max(-1, min(1, q + adj)), t))
    hs = f["hook_strength"]
    if hs is not None: r.append(("hook_text", max(-1, min(1, (hs - 0.3) / 0.25)), f"hook line strength {hs:.2f} (question/number/claim/'you')"))
    if f["n_cuts"] == 0: r.append(("pace", -1.0, "no cuts"))
    else:
        q = 2 * f["frac_gaps_2_4s"] - 1 - (0.5 if (f["max_gap_s"] or 0) > 8 else 0) - (0.4 if (f["gap_cv"] or 0) > 0.6 else 0)
        r.append(("pace", max(-1, min(1, q)), f"{f['frac_gaps_2_4s']:.0%} shots 2-4 s, longest {f['max_gap_s']:.1f}s, CV {f['gap_cv'] or 0:.2f}"))
    cs = f["caption_style"]
    r.append(("cap", {"word_pop": 1.0, "sentence": 0.6}.get(cs, 1.0 if f["captions"] else -1.0),
              f"captions: {cs}" if f["captions"] else "no captions (most feeds autoplay muted)"))
    s = f["silence_removed_s"]; r.append(("silence", min(1.0, s / 2.0) if s > 0 else -0.5, f"{s:.1f}s dead air removed" if s > 0 else "dead air not trimmed"))
    r.append(("loop", 1.0 if f["loop_end"] else 0.0, "loop-friendly ending" if f["loop_end"] else "non-looping ending"))
    r.append(("title", 0.6 if f["hook_title"] else -0.2, "hook title overlay" if f["hook_title"] else "no hook title"))
    return r

def _length_q(d, p):
    lo, hi = p["sweet"]
    if d <= 0: return 0.0, "duration unknown"
    if lo <= d <= hi:
        mid = (lo + hi) / 2; return 1.0 - 0.3 * abs(d - mid) / ((hi - lo) / 2), f"{d:.0f}s in sweet spot {lo}-{hi}s"
    if d < lo: return 0.3 - 0.5 * (lo - d) / lo, f"{d:.0f}s shorter than sweet spot {lo}-{hi}s"
    if d <= p["max_ok"]: return -0.5 * (d - hi) / (p["max_ok"] - hi), f"{d:.0f}s a bit long (sweet {lo}-{hi}s)"
    return -1.0, f"{d:.0f}s too long (>{p['max_ok']}s)"

def _sig(x): return 1 / (1 + math.exp(-x))

# channel -> personas whose fit drives it (mean)
def _channel_personas():
    from .channels import CHANNEL_SPECS
    def norm(a):
        a = a if isinstance(a, dict) else {n: 1.0 for n in a}; t = sum(a.values()); return {n: w / t for n, w in a.items()}
    return {k: norm(v["audience"]) for k, v in CHANNEL_SPECS.items()}

def _spec(ch):
    from .channels import CHANNEL_SPECS
    sp = CHANNEL_SPECS.get(ch, {}); return float(sp.get("share_weight", 1.0)), float(sp.get("views_scale", 1.0))
FORMAT_MISMATCH = 0.6     # multiplier when the clip's aspect isn't in the channel's accepted aspects
DUR_FIT = {"ideal": 1.0, "ok": 0.85, "too_long": 0.3}

def _channel_fit(f, ch):
    from .channels import CHANNEL_SPECS, fit_for_channel
    sp = CHANNEL_SPECS[ch]; a = f["aspect"]
    fm = 1.0 if a is None or a in sp["aspect"] else FORMAT_MISMATCH
    df = fit_for_channel(f["duration_s"], ch) if f["duration_s"] else "ok"
    why = (f"aspect {a} ok" if fm == 1 else f"aspect {a} not in {sp['aspect']} (x{FORMAT_MISMATCH})") + f"; duration {df} (ideal {sp['ideal_s'][0]}-{sp['ideal_s'][1]}s)"
    return fm * DUR_FIT[df], why

W_LEN = 1.5
def score_dummy(manifest_path_or_dict):
    m = _load(manifest_path_or_dict); f = features(m); rules = _rules(f); per = {}; reasons = []; U = {}
    for name, p in PERSONAS.items():
        lq, lr = _length_q(f["duration_s"], p)
        W = dict(hook=p["hook_w"], hook_text=p["hook_w"] * 0.8, pace=p["pace_w"], cap=p["cap_w"], silence=p["silence_w"],
                 loop=p["loop_w"], title=0.5, length=W_LEN)
        u = (sum(W[k] * q for k, q, _ in rules) + W["length"] * lq) / sum(W[k] for k, _, _ in rules + [("length", 0, "")])  # -1..1
        U[name] = u
        hook_q = next(q for k, q, _ in rules if k == "hook")
        swipe = p["base_swipe"] * math.exp(-0.9 * (0.6 * hook_q + 0.4 * u) * p["hook_w"] / 1.2)
        watch = _sig(math.log(p["base_watch"] / (1 - p["base_watch"])) + 2.0 * u)
        share = p["base_share"] * math.exp(1.2 * u + 0.4 * (f["loop_end"]) * p["loop_w"])
        swipe, share = min(max(swipe, 0.02), 0.95), min(share, 0.1)
        per[name] = dict(p_swipe3s=round(swipe, 4), watch_pct=round(100 * watch, 1), p_share=round(share, 5), score=round(_comp(swipe, watch, share), 4))
        reasons.append(dict(persona=name, length=lr, top=[t for _, q, t in sorted(rules, key=lambda r: -abs(r[1]) * W[r[0]])[:3]]))
    w = {n: p["weight"] for n, p in PERSONAS.items()}
    for n, p in PERSONAS.items():
        neutral = _comp(p["base_swipe"], p["base_watch"], p["base_share"])
        per[n]["channel"] = CHANNEL_FIT[n][0]; per[n]["fit"] = round(per[n]["score"] - neutral, 4)
    # per-channel score: persona score for that channel's audience x format/duration fit
    channels = []
    for ch, ps in _channel_personas().items():
        # relative to each persona's neutral clip so channels with "easier" audiences don't always win
        shw, vsc = _spec(ch)
        fit = sum(w_ * per[n]["fit"] for n, w_ in ps.items())
        sfit = sum(w_ * (per[n]["p_share"] / PERSONAS[n]["base_share"] - 1) for n, w_ in ps.items())  # share lift vs neutral
        fit += 0.02 * (shw - 1) * sfit                                                              # share-weighted channels reward shareable clips
        base = min(1.0, max(0.0, 0.5 + 1.5 * fit)); mult, why = _channel_fit(f, ch)
        channels.append(dict(channel=ch, score=round(base * mult, 4), audience_fit=round(fit, 4), format_fit=round(mult, 3),
                             personas=ps, reason=f"{why}; " + "; ".join(CHANNEL_FIT[n][2] for n in ps), predicted=_sim_pred(per, ps, mult, shw, vsc)))
    channels.sort(key=lambda c: -c["score"])
    tgt = f["channel"]; tc = next((c for c in channels if c["channel"] == tgt), None)
    ov = {k: sum(w[n] * per[n][k] for n in per) for k in ["p_swipe3s", "watch_pct", "p_share"]}
    raw = _comp(ov["p_swipe3s"], ov["watch_pct"] / 100, ov["p_share"])
    # overall score: target channel's score if the clip names a channel (format-aware), else audience-weighted
    sc = tc["score"] if tc else raw
    ov = dict(p_swipe3s=round(ov["p_swipe3s"], 4), watch_pct=round(ov["watch_pct"], 1), p_share=round(ov["p_share"], 5), score=round(sc, 4))
    return dict(variant_id=m.get("variant_id"), overall=ov, audience_score=round(raw, 4), target_channel=tgt,
                target_channel_fit=tc and tc["format_fit"], personas=per, model_version=MODEL_VERSION, confidence=CONFIDENCE,
                label=LABEL, best_persona=max(per, key=lambda n: per[n]["fit"]), best_channel=channels[0]["channel"],
                channels=channels, reasons=reasons, rule_inputs=f)

VIEWS_PER_SCORE = (500, 20000)  # mock: views range for a 0..1 channel score, hand-set
def _sim_pred(per, ps, mult, shw=1.0, vsc=1.0):
    """Mock predicted results for a channel (simulated: True). Derived from the heuristic personas (weighted), not data."""
    sw = min(0.95, sum(w * per[n]["p_swipe3s"] for n, w in ps.items()) / mult)
    wa = sum(w * per[n]["watch_pct"] for n, w in ps.items()) * mult; sh = sum(w * per[n]["p_share"] for n, w in ps.items()) * mult * shw
    q = max(0.0, min(1.0, (1 - sw) * 0.5 + wa / 200)); lo = int(VIEWS_PER_SCORE[0] + q * q * VIEWS_PER_SCORE[1] * 0.4); hi = int(lo * 2.5)
    lo, hi = int(lo * vsc), int(hi * vsc)
    return dict(views_range=[lo, hi], watch_pct=round(wa, 1), p_swipe3s=round(sw, 4), shares_range=[int(lo * sh), int(hi * sh) + 1], simulated=True)

def _comp(swipe, watch, share):
    return (1 - swipe) * 0.4 + watch * 0.4 + min(share / 0.02, 1) * 0.2

def score_batch(manifests):
    """Score many clips; adds 'batch_rank' and 'batch_score' (min-max rescaled overall.score to 0-100 within the batch)."""
    rs = [score(x) for x in manifests]; v = [r["overall"]["score"] for r in rs]; lo, hi = min(v), max(v)
    for r in rs: r["batch_score"] = round(100 * (r["overall"]["score"] - lo) / (hi - lo), 1) if hi > lo else 50.0
    for i, r in enumerate(sorted(rs, key=lambda r: -r["overall"]["score"]), 1): r["batch_rank"] = i
    return rs

def simulate_ab(channel, variant_manifests, n_impressions=5000, seed=0):
    """SIMULATED A/B (mock data, simulated=True). Draws per-variant views/watch/shares/swipes around each variant's heuristic
    prediction for `channel`, then runs clipcut.ab.analyze (Beta posteriors + Thompson P(best)). Deterministic per seed.
    Traffic is split evenly for the mock round; `split` is the Thompson allocation for the NEXT round."""
    import numpy as np
    from . import ab
    rng = np.random.default_rng(seed); scored = [score(x) for x in variant_manifests]; n = max(1, n_impressions // max(1, len(scored)))
    ps = _channel_personas().get(channel, {n: 1 / len(PERSONAS) for n in PERSONAS}); results = {}
    for r in scored:
        cf = next((c for c in r["channels"] if c["channel"] == channel), None); mult = cf["format_fit"] if cf else 1.0
        sw = sum(w * r["personas"][p]["p_swipe3s"] for p, w in ps.items()); wa = sum(w * r["personas"][p]["watch_pct"] for p, w in ps.items()) / 100
        sh = sum(w * r["personas"][p]["p_share"] for p, w in ps.items()) * _spec(channel)[0]
        sw = min(0.95, sw / mult); wa *= mult; sh *= mult                     # format mismatch hurts the simulated results too
        sw_t = float(np.clip(sw + rng.normal(0, 0.03), 0.01, 0.99)); wa_t = float(np.clip(wa + rng.normal(0, 0.03), 0.01, 0.99))
        swipes = int(rng.binomial(n, sw_t)); stayed = n - swipes
        watch = float(np.clip(rng.beta(1 + 50 * wa_t, 1 + 50 * (1 - wa_t)), 0, 1)) if stayed else 0.0
        shares = int(rng.poisson(n * sh * (1 + rng.normal(0, 0.2)) if sh > 0 else 0))
        results[r["variant_id"]] = dict(views=n, swipes_3s=swipes, swipe_rate=round(swipes / n, 4), avg_watch_pct=round(100 * watch, 1),
                                        shares=shares, share_rate=round(shares / n, 5), heuristic_score=r["overall"]["score"], simulated=True)
    an = ab.analyze({v: dict(views=x["views"], avg_watch_pct=x["avg_watch_pct"], shares=x["shares"]) for v, x in results.items()}, n_draws=4000, seed=seed)
    # winner metric = same composite as the heuristic (0.4 stay-past-3s + 0.4 watch + 0.2 share), Thompson draws from Beta posteriors
    ids = list(results); D = 4000
    st = np.array([rng.beta(1 + results[v]["views"] - results[v]["swipes_3s"], 1 + results[v]["swipes_3s"], D) for v in ids])
    wa = np.array([rng.beta(1 + (results[v]["views"] - results[v]["swipes_3s"]) * results[v]["avg_watch_pct"] / 100,
                            1 + (results[v]["views"] - results[v]["swipes_3s"]) * (1 - results[v]["avg_watch_pct"] / 100), D) for v in ids])
    sh = np.array([rng.beta(1 + results[v]["shares"], 1 + results[v]["views"] - results[v]["shares"], D) for v in ids])
    comp = 0.4 * st + 0.4 * wa * st + 0.2 * np.minimum(sh / 0.02, 1)  # watch % of all impressions = stayers' watch x stay rate
    pb = np.bincount(comp.argmax(0), minlength=len(ids)) / D
    p_best = {v: round(float(pb[i]), 4) for i, v in sorted(enumerate(ids), key=lambda t: -pb[t[0]])}
    win = next(iter(p_best))
    return dict(channel=channel, split=p_best.copy(), results=results, p_best=p_best, winner=win, confident=p_best[win] >= 0.95,
                ab_py_watch_share_only=dict(winner=an["winner"], p_best={r["variant_id"]: round(r["p_best"], 4) for r in an["table"]}),
                n_impressions=n * len(results), seed=seed, simulated=True, label="SIMULATED mock A/B - not real traffic")

def score_model(manifest_path_or_dict):
    """Old retention-v1 (KuaiRand length+persona + hand-set MicroLens edit link); slow (~10 s, decodes video)."""
    from . import retention_model as RM
    x = manifest_path_or_dict
    if isinstance(x, (str, Path)) and not str(x).lower().endswith((".mp4", ".mov", ".webm", ".mkv")):
        p = Path(x); m = json.load(open(p)); vp = m.get("video_path")
        if vp and not Path(vp).is_absolute() and (p.parent / vp).exists(): m["video_path"] = str(p.parent / vp)
    else: m = _load(x)
    return RM.predict(m)

def score(manifest_path_or_dict):
    if os.environ.get("CLIPCUT_SCORER", "dummy") == "model": return score_model(manifest_path_or_dict)
    return score_dummy(manifest_path_or_dict)
