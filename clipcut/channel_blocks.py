"""Channel result blocks for the UI (owner: cutter/UI agent). Prefers retention-modeler pieces when importable:
  list_channels()              in clipcut.channels / clipcut.scorer / clipcut.retention
  simulate_ab(channel, manifests, seed=...)  in clipcut.retention / clipcut.ab / clipcut.scorer / clipcut.channels
  per-channel predictions inside score() output (results.json "channels" entries)
Falls back to local config + a local simulation through clipcut.ab (Thompson). Everything here is SIMULATED."""
import importlib, json
from pathlib import Path
import numpy as np

SEED = 0
FALLBACK_CHANNELS = {"TikTok": dict(aspect=["9:16"], ideal_s=(15, 34)), "Reels": dict(aspect=["9:16"], ideal_s=(15, 45)),
                     "Shorts": dict(aspect=["9:16"], ideal_s=(20, 50)), "X": dict(aspect=["16:9", "1:1"], ideal_s=(15, 45)),
                     "LinkedIn": dict(aspect=["1:1", "4:5"], ideal_s=(30, 90)),
                     # not yet in channels.py (retention-modeler adding): local fallback specs, merged in when missing
                     "YouTube": dict(aspect=["16:9"], ideal_s=(120, 900)), "IG_Feed": dict(aspect=["1:1", "4:5"], ideal_s=(15, 60)),
                     "IG_Stories": dict(aspect=["9:16"], ideal_s=(5, 15), max_s=60), "FB_Feed": dict(aspect=["1:1", "4:5", "16:9"], ideal_s=(15, 60))}

def _find(name, mods):
    for m in mods:
        try:
            f = getattr(importlib.import_module(f"clipcut.{m}"), name, None)
            if callable(f): return f, m
        except Exception: pass
    return None, None

def list_channels():
    f, src = _find("list_channels", ["channels", "scorer", "retention"])
    if f:
        try:
            ch = f(); out = {}
            for c in (ch.items() if isinstance(ch, dict) else ch):
                if isinstance(c, tuple): out[c[0]] = dict(c[1])
                elif isinstance(c, dict): out[c.get("name") or c.get("channel")] = c
                else: out[str(c)] = {}
            if out: return out, f"{src}.list_channels"
        except Exception as e: print("[channel_blocks] list_channels failed:", e)
    try:
        from .channels import CHANNEL_SPECS
        out = {k: dict(v) for k, v in CHANNEL_SPECS.items()}; extra = [k for k in FALLBACK_CHANNELS if k not in out]
        out.update({k: dict(FALLBACK_CHANNELS[k], fallback=True) for k in extra})
        return out, "channels.CHANNEL_SPECS" + (f" + fallback ({', '.join(extra)})" if extra else "")
    except Exception: return dict(FALLBACK_CHANNELS), "fallback config"

def _fits(spec, aspect):
    al = spec.get("aspect") or ["9:16"]; al = [al] if isinstance(al, str) else al
    return aspect in al

def _chan_entry(r, ch):
    for c in r.get("channels") or []:
        if isinstance(c, dict) and c.get("channel") == ch: return c
    return {}

def _pred(r, ch):
    """Prefer scorer-provided per-channel predictions; else derive from overall."""
    e = _chan_entry(r, ch); p = e.get("predicted") or e.get("predicted_results") or {k: e[k] for k in ("views", "watch_pct", "swipe_away", "shares") if k in e}
    o = r["overall"]
    out = dict(watch_pct=o.get("watch_pct"), swipe_away=o.get("p_swipe3s"), share_rate=o.get("p_share"), views=None, shares=None,
               fit=e.get("score", e.get("fit")), format_fit=e.get("format_fit"), source="ClipCut fallback (from overall score)")
    if p:
        out.update({k: v for k, v in p.items() if v is not None}); out["source"] = "scorer per-channel prediction"
        if "swipe_away" not in p and "p_swipe3s" in p: out["swipe_away"] = p["p_swipe3s"]
    return out

def _local_sim(ch, rows, seed=SEED, budget=1000):
    from .ab import analyze, thompson_allocate
    import zlib; rng = np.random.default_rng(zlib.crc32(ch.encode()) + seed); met = {}; sim = {}
    for r in rows:
        p = r["pred"]; v = r["variant_id"]; n = int(p.get("views") or budget // max(len(rows), 1))
        w = float(p["watch_pct"] or 50); w = w / 100 if w > 1 else w; sw = float(p["swipe_away"] or 0.4); sh = float(p["share_rate"] or 0.01)
        w_obs = float(np.clip(rng.normal(w, 0.03), 0, 1)); shares = int(rng.binomial(n, min(sh, 1))); swipes = int(rng.binomial(n, min(sw, 1)))
        met[v] = dict(views=n, avg_watch_pct=w_obs * 100, shares=shares); sim[v] = dict(views=n, watch_pct=round(w_obs * 100, 1), shares=shares, swipe_away=round(swipes / n, 3))
    a = analyze(met, seed=seed); split = thompson_allocate(met, budget=budget, seed=seed); pb = {t["variant_id"]: t["p_best"] for t in a["table"]}
    return {v: dict(sim[v], p_best=round(pb[v], 4), traffic=split[v], winner=v == a["winner"]) for v in met}, "ClipCut local sim (clipcut.ab Thompson)"

def _norm_ab(out):
    """Accepts {results:{vid:{...}}, p_best:{}, split:{}, winner} (retention.simulate_ab) or a list/dict of per-variant rows."""
    top = out if isinstance(out, dict) else {}
    rows = top.get("results", top.get("variants", out))
    if isinstance(rows, dict): rows = [dict(variant_id=k, **v) for k, v in rows.items() if isinstance(v, dict)]
    pb, sp, win = top.get("p_best") or {}, top.get("split") or {}, top.get("winner")
    res = {}
    for x in rows:
        v = x.get("variant_id") or x.get("id")
        res[v] = dict(views=x.get("views"), watch_pct=x.get("watch_pct", x.get("avg_watch_pct")), shares=x.get("shares"),
                      swipe_away=x.get("swipe_away", x.get("swipe_rate", x.get("p_swipe3s"))),
                      p_best=x.get("p_best", x.get("p_winner", pb.get(v))), traffic=x.get("traffic", x.get("split", sp.get(v))),
                      winner=bool(x.get("winner")) or v == win)
    return res, top.get("label")

def build(run_dir, results):
    run_dir = Path(run_dir); chans, csrc = list_channels(); sim_f, sim_src = _find("simulate_ab", ["retention", "ab", "scorer", "channels"])
    blocks = []
    for ch, spec in chans.items():
        rows = [r for r in results if _fits(spec, r.get("aspect", "9:16"))]
        if not rows: continue
        for r in rows: r = r; r.setdefault("_pred", {})[ch] = _pred(r, ch)
        rows_c = [dict(variant_id=r["variant_id"], pred=r["_pred"][ch]) for r in rows]
        ab, absrc = None, None
        if sim_f:
            try:
                ab, lab = _norm_ab(sim_f(ch, [str(run_dir / f"{r['variant_id']}.json") for r in rows], seed=SEED))
                absrc = f"{sim_src}.simulate_ab(seed={SEED})" + (f" · {lab}" if lab else "")
            except Exception as e: print("[channel_blocks] simulate_ab failed, local sim:", e)
        if not ab: ab, absrc = _local_sim(ch, rows_c)
        items = []
        for r in rows:
            a = ab.get(r["variant_id"], {}); items.append(dict(variant_id=r["variant_id"], aspect=r.get("aspect"), duration_s=r["duration_s"], poster=r.get("poster"),
                video=r.get("video"), score=r["overall"]["score"], pred=r["_pred"][ch], ab=a))
        tot = sum((i["ab"].get("traffic") or 0) for i in items) or 1
        for i in items: i["ab"]["traffic_pct"] = round(100 * (i["ab"].get("traffic") or 0) / tot, 1) if i["ab"].get("traffic") is not None else None
        items.sort(key=lambda i: -(i["ab"].get("p_best") or 0))
        win = next((i["variant_id"] for i in items if i["ab"].get("winner")), items[0]["variant_id"])
        close = None
        if len(items) > 1:  # 'too close to call': top-2 by P(best) within ~0.01 on channel fit score or P(best)
            a, b = items[0], items[1]; ds = abs((a["pred"].get("fit") or a["score"]) - (b["pred"].get("fit") or b["score"]))
            dp = abs((a["ab"].get("p_best") or 0) - (b["ab"].get("p_best") or 0))
            if ds <= 0.01 or dp <= 0.01: close = dict(between=[a["variant_id"], b["variant_id"]], score_diff=round(ds, 4), p_best_diff=round(dp, 4))
        blocks.append(dict(channel=ch, spec={k: spec.get(k) for k in ("aspect", "ideal_s", "max_s", "resolution") if k in spec}, winner=win,
                           items=items, ab_source=absrc, too_close=close, simulated=True))
    for r in results: r.pop("_pred", None)
    return dict(channels_source=csrc, simulate_ab_available=bool(sim_f), blocks=blocks, simulated=True)
