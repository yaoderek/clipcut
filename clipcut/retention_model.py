"""Per-persona retention model (owner: retention agent). Train: python train_retention.py
Targets (REAL, KuaiRand-Pure watch logs): p_swipe3s (play<3s), watch_pct (play/duration, clipped 0..1), p_share (is_forward).
Personas: KMeans(k=4) on user behavior. Model: per-target LightGBM on (persona, log duration[, category]) at the video x persona level.
Edit-awareness: KuaiRand has no raw video, so edit features cannot be learned against watch time. The edit signal comes from
the MicroLens v2 like-rate model (models/personas_v2.pkl, real videos + engagement) and is applied as a bounded multiplier
(EDIT_LINK below, hand-set, NOT learned against retention)."""
import json, pickle, numpy as np, pandas as pd, lightgbm as lgb
from pathlib import Path
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import GroupKFold
from sklearn.metrics import mean_absolute_error, roc_auc_score
from scipy.stats import spearmanr
ROOT = Path(__file__).resolve().parent.parent; D = ROOT / "data/KuaiRand-Pure/data"; MD = ROOT / "models/retention"
VERSION = "retention-v1-kuairand+microlens-edit"
LIKE_MEAN_LOG = -3.175  # mean log(likes/views) over MicroLens sample
TARGETS = ["p_swipe3s", "watch_pct", "p_share"]
EDIT_LINK = dict(p_share=1.0, watch_pct=0.15, p_swipe3s=-0.15)  # elasticity wrt log(pred like-rate / mean like-rate)
PRM = dict(n_estimators=300, learning_rate=0.05, num_leaves=31, min_child_samples=40, verbose=-1, n_jobs=1)

def load():
    logs = pd.concat([pd.read_csv(D / f, usecols=["user_id", "video_id", "play_time_ms", "duration_ms", "is_forward", "is_like", "long_view"])
                      for f in ["log_standard_4_08_to_4_21_pure.csv", "log_standard_4_22_to_5_08_pure.csv", "log_random_4_22_to_5_08_pure.csv"]])
    logs = logs[(logs.play_time_ms > 0) & (logs.duration_ms > 0)].copy()
    logs["watch_pct"] = (logs.play_time_ms / logs.duration_ms).clip(0, 1); logs["p_swipe3s"] = (logs.play_time_ms < 3000).astype(float)
    logs["p_share"] = logs.is_forward.astype(float)
    vid = pd.read_csv(D / "video_features_basic_pure.csv")[["video_id", "tag"]]; vid["tag"] = vid.tag.astype(str).str.split(",").str[0]
    st = pd.read_csv(D / "video_features_statistic_pure.csv")[["video_id", "show_cnt"]]
    return logs, vid, st

def personas(logs, k=4, seed=0):
    g = logs.groupby("user_id")
    u = pd.DataFrame(dict(watch=g.watch_pct.mean(), swipe=g.p_swipe3s.mean(), share=g.p_share.mean(), like=g.is_like.mean(),
                          dur=np.log(g.duration_ms.mean()), act=np.log1p(g.size())))
    sc = StandardScaler().fit(u); km = KMeans(k, n_init=10, random_state=seed).fit(sc.transform(u))
    lab = pd.Series(km.labels_, index=u.index); prof = u.groupby(lab).mean(); z = (prof - u.mean()) / u.std()
    names = {}
    for p in range(k):
        r = z.loc[p]
        if r.swipe > .5: n = "fast_swiper"
        elif r.share > .5 or r.like > .5: n = "sharer_engager"
        elif r.watch > .5: n = "completionist"
        else: n = "casual_browser"
        while n in names.values(): n += "_b"
        names[p] = n
    return lab, names, prof, lab.value_counts().sort_index()

def table(logs, lab, vid, st, min_n=5):
    l = logs.assign(persona=logs.user_id.map(lab))
    a = l.groupby(["video_id", "persona"]).agg(n=("watch_pct", "size"), dur=("duration_ms", "median"), **{t: (t, "mean") for t in TARGETS}).reset_index()
    a = a[a.n >= min_n].merge(vid, on="video_id", how="left").merge(st, on="video_id", how="left")
    a["log_dur"] = np.log(a.dur / 1000); a["tag"] = a.tag.astype("category"); a["log_pop"] = np.log1p(a.show_cnt.fillna(0))
    return a

SETS = {"duration_only": ["log_dur"], "duration+persona": ["log_dur", "persona"], "duration+persona+category": ["log_dur", "persona", "tag"]}

def evaluate(a):
    """GroupKFold by VIDEO. Weighted by n impressions. Baselines: global mean, persona mean, popularity (log show_cnt + persona)."""
    out = {t: {} for t in TARGETS}; preds = {t: {} for t in TARGETS}
    for t in TARGETS:
        P = {m: np.zeros(len(a)) for m in ["global_mean", "persona_mean", "popularity", *SETS]}
        for tr, te in GroupKFold(5).split(a, groups=a.video_id):
            A, B = a.iloc[tr], a.iloc[te]; w = A.n
            P["global_mean"][te] = np.average(A[t], weights=w)
            pm = A.groupby("persona").apply(lambda d: np.average(d[t], weights=d.n)); P["persona_mean"][te] = B.persona.map(pm)
            for m, cols in [("popularity", ["log_pop", "persona"]), *SETS.items()]:
                P[m][te] = lgb.LGBMRegressor(**PRM).fit(A[cols], A[t], sample_weight=w).predict(B[cols])
        y, w = a[t].values, a.n.values
        for m, p in P.items():
            sp = [spearmanr(y[a.persona == q], p[a.persona == q])[0] for q in sorted(a.persona.unique())]
            out[t][m] = dict(wMAE=float(np.average(np.abs(y - p), weights=w)),
                             wR2=float(1 - np.average((y - p) ** 2, weights=w) / np.average((y - np.average(y, weights=w)) ** 2, weights=w)),
                             spearman_within_persona=float(np.nanmean(sp)) if m not in ("global_mean", "persona_mean") else 0.0)
        preds[t] = P
    return out, preds

def fit_final(a):
    return {t: lgb.LGBMRegressor(**PRM).fit(a[SETS["duration+persona"]], a[t], sample_weight=a.n) for t in TARGETS}

# ---------------- inference ----------------
_B = _V2 = None
def _bundle():
    global _B, _V2
    if _B is None: _B = pickle.load(open(MD / "retention.pkl", "rb"))
    if _V2 is None:
        try: _V2 = pickle.load(open(ROOT / "models/personas_v2.pkl", "rb"))
        except Exception: _V2 = False
    return _B, _V2

def _manifest_features(m):
    """Cheap edit features from the editor manifest (no decoding)."""
    dur = float(m.get("duration_s") or m.get("duration") or 0)
    cuts = m.get("cut_times_s") or m.get("cuts") or []
    caps = m.get("captions") or []
    return dict(duration_s=dur, cuts_per_10s=10 * sum(1 for t in cuts if float(t) < 15) / max(min(dur, 15) if dur else 15, 1) if cuts else None,
                n_captions=len(caps), hook_end_s=m.get("hook_end_s", m.get("hook_end")), silence_removed_s=m.get("silence_removed_s"))

def _edit_multiplier(video_path, V2, mf):
    """log(pred like-rate / training-mean like-rate) from the MicroLens edit-aware model; 0 if unavailable."""
    if not V2 or not video_path or not Path(video_path).exists(): return 0.0, "no_video_features"
    from .features import extract
    f = extract(video_path); clip = np.array(f.pop("clip"))[None]
    f.update({f"clip{i}": v for i, v in enumerate(V2["pca"].transform(clip)[0])}); f["log_duration"] = np.log1p(f["duration_s"])
    if mf.get("cuts_per_10s") is not None: f["cuts_per_10s"] = mf["cuts_per_10s"]  # editor's exact cuts beat the detector
    X = pd.DataFrame([{c: f.get(c, V2["feat_mean"].get(c, 0.0)) for c in V2["cols"]}])
    lr = float(V2["like_model"].predict(X)[0])
    return float(np.clip(lr - V2.get("like_rate_mean_log", LIKE_MEAN_LOG), -1.0, 1.0)), "microlens_edit_model"

def predict(m):
    B, V2 = _bundle(); mf = _manifest_features(m)
    vp = m.get("video_path") or m.get("file")
    if not mf["duration_s"] and vp:
        from .features import _dur; mf["duration_s"] = _dur(vp)
    dur = max(mf["duration_s"] or 15.0, 1.0)
    try: e, src = _edit_multiplier(vp, V2, mf)
    except Exception as ex: e, src = 0.0, f"edit_features_failed:{str(ex)[:80]}"
    per = {}
    for p, name in B["names"].items():
        X = pd.DataFrame([dict(log_dur=np.log(dur), persona=p)]); r = {}
        for t in TARGETS:
            v = float(B["models"][t].predict(X)[0]) * float(np.exp(EDIT_LINK[t] * e)); r[t] = float(np.clip(v, 0, 1))
        r["score"] = _score(r); per[name] = r
    w = B["weights"]; ov = {t: float(sum(w[n] * per[n][t] for n in per)) for t in TARGETS}; ov["score"] = _score(ov)
    rn = lambda d: {"p_swipe3s": round(d["p_swipe3s"], 4), "watch_pct": round(100 * d["watch_pct"], 2), "p_share": round(d["p_share"], 5), "score": round(d["score"], 4)}
    conf = "low" if src != "microlens_edit_model" else "low-medium"
    return dict(variant_id=m.get("variant_id"), overall=rn(ov), personas={n: rn(v) for n, v in per.items()}, model_version=VERSION,
                confidence=conf, edit_signal=dict(log_like_ratio=round(e, 3), source=src), manifest_features=mf)

def _score(r):  # 0..1 composite: stay past 3s, watch share, share bonus (share base rate ~0.2%)
    return float((1 - r["p_swipe3s"]) * 0.4 + r["watch_pct"] * 0.4 + min(r["p_share"] / 0.01, 1) * 0.2)
