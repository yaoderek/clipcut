"""Persona simulator trained on REAL KuaiRand-Pure logs (Kuaishou short-video app, Gao et al. 2022, CC BY-SA 4.0).
- Users clustered into personas with KMeans on behavior features.
- Per persona: LightGBM regressor (watch ratio) + classifier (swipe-away < 3s).
- Content features available in the real data: duration, tag (category), upload_type, music_type, video_type, hour.
  Hook/pacing/caption features do NOT exist in KuaiRand; their effect is an explicit, labeled heuristic prior."""
import json, pickle, numpy as np, pandas as pd, lightgbm as lgb
from pathlib import Path
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_absolute_error, r2_score, roc_auc_score

D = Path(__file__).resolve().parent.parent / "data" / "KuaiRand-Pure" / "data"
MODEL = Path(__file__).resolve().parent.parent / "models" / "personas.pkl"
FEATS = ["duration_s", "tag", "upload_type", "music_type", "video_type", "hour"]
CATS = ["tag", "upload_type", "music_type", "video_type"]
SKIP_MS = 3000

def load():
    logs = pd.concat([pd.read_csv(D / f) for f in ["log_standard_4_08_to_4_21_pure.csv", "log_standard_4_22_to_5_08_pure.csv",
                                                   "log_random_4_22_to_5_08_pure.csv"]])
    logs = logs[(logs.play_time_ms > 0) & (logs.duration_ms > 0)].copy()  # actually-played impressions
    logs["watch_ratio"] = (logs.play_time_ms / logs.duration_ms).clip(0, 2.0)
    logs["skip"] = (logs.play_time_ms < SKIP_MS).astype(int)
    vid = pd.read_csv(D / "video_features_basic_pure.csv")[["video_id", "tag", "upload_type", "music_type", "video_type"]]
    vid["tag"] = vid["tag"].astype(str).str.split(",").str[0]
    df = logs.merge(vid, on="video_id", how="left")
    df["duration_s"] = df.duration_ms / 1000; df["hour"] = df.hourmin // 100
    for c in CATS: df[c] = df[c].astype(str).astype("category")
    return df

def user_features(df):
    g = df.groupby("user_id")
    u = pd.DataFrame(dict(n=g.size(), wr=g.watch_ratio.mean(), skip=g.skip.mean(), dur=g.duration_s.mean(),
                          like=g.is_like.mean(), fwd=g.is_forward.mean(), long_view=g.long_view.mean()))
    u["n"] = np.log1p(u.n)
    return u

def train(k=5, seed=0):
    df = load(); print(f"[data] {len(df):,} played impressions, {df.user_id.nunique():,} users, {df.video_id.nunique():,} videos")
    u = user_features(df); sc = StandardScaler().fit(u)
    km = KMeans(k, n_init=10, random_state=seed).fit(sc.transform(u)); u["persona"] = km.labels_
    df = df.merge(u[["persona"]], left_on="user_id", right_index=True)
    cut = df.date.quantile(0.85)  # time-based holdout: last ~15% of days
    tr, te = df[df.date < cut], df[df.date >= cut]
    personas, metrics = {}, {}
    for p in range(k):
        a, b = tr[tr.persona == p], te[te.persona == p]
        prm = dict(n_estimators=300, learning_rate=0.05, num_leaves=63, min_child_samples=100, verbose=-1)
        reg = lgb.LGBMRegressor(**prm).fit(a[FEATS], a.watch_ratio)
        clf = lgb.LGBMClassifier(**prm).fit(a[FEATS], a.skip)
        pr, pc = reg.predict(b[FEATS]), clf.predict_proba(b[FEATS])[:, 1]
        base = np.full(len(b), a.watch_ratio.mean())
        prof = u[u.persona == p].mean()
        metrics[p] = dict(n_users=int((u.persona == p).sum()), n_train=len(a), n_test=len(b),
                          watch_ratio_MAE=mean_absolute_error(b.watch_ratio, pr), baseline_MAE=mean_absolute_error(b.watch_ratio, base),
                          watch_ratio_R2=r2_score(b.watch_ratio, pr), skip_AUC=roc_auc_score(b.skip, pc),
                          mean_watch_ratio=float(prof.wr), skip_rate=float(prof.skip), like_rate=float(prof.like))
        personas[p] = dict(reg=reg, clf=clf, name=name_persona(prof, u), weight=metrics[p]["n_users"] / len(u),
                           hook_sensitivity=float(prof.skip))  # high-skip users punish weak hooks more
        print(f"[persona {p}] {personas[p]['name']}: users={metrics[p]['n_users']} MAE={metrics[p]['watch_ratio_MAE']:.3f} "
              f"(baseline {metrics[p]['baseline_MAE']:.3f}) R2={metrics[p]['watch_ratio_R2']:.3f} skipAUC={metrics[p]['skip_AUC']:.3f}")
    cats = {c: list(df[c].cat.categories) for c in CATS}
    common = {c: df[c].mode()[0] for c in CATS}
    MODEL.parent.mkdir(exist_ok=True)
    pickle.dump(dict(personas=personas, cats=cats, common=common), open(MODEL, "wb"))
    json.dump({str(k_): {kk: (round(v, 4) if isinstance(v, float) else v) for kk, v in m.items()} for k_, m in metrics.items()},
              open(MODEL.parent / "metrics.json", "w"), indent=2)
    return metrics

def name_persona(prof, u):
    q = lambda c: (u[c] < prof[c]).mean()  # percentile among users
    if q("skip") > 0.66: base = "Fast Scroller"
    elif q("wr") > 0.66: base = "Completionist"
    elif q("like") > 0.66: base = "Engaged Fan"
    else: base = "Casual Browser"
    return f"{base} (wr={prof.wr:.2f}, skip={prof.skip:.2f})"

# ---------- scoring clip-cutter variants ----------
def heuristic_multiplier(v, sens):
    """ASSUMPTION (not learned from data): hook/pacing/caption effects, scaled by persona skip-sensitivity."""
    m_skip = 1.0 - 0.06 * min(v["hook_score"], 3) - 0.08 * v["hook_zoom"] - 0.05 * (v["caption_style"] != "none")
    m_skip *= (1 + (sens - 0.3))  # fast scrollers react more
    m_wr = 1.0 + 0.04 * v["jump_cuts"] + 0.03 * (v["caption_style"] != "none") + 0.01 * min(v["cuts_per_min"], 20) / 2
    return max(m_skip, 0.3), m_wr

def retention_curve(L, p_skip, wr, n=31):
    """Approx survival curve: drop p_skip by 3s, then exponential decay whose mean matches predicted watch ratio."""
    t = np.linspace(0, L, n); wr = float(np.clip(wr, 0.05, 1.0))
    s3 = 1 - p_skip
    # mean of truncated exp on [3,L] relative to L ~= target; solve lambda by bisection
    def mean_ratio(lam):
        tt = np.linspace(0, L, 400)
        return np.trapezoid(np.where(tt < 3, 1 - p_skip * tt / 3, s3 * np.exp(-lam * (tt - 3))), tt) / L
    lo, hi = 1e-4, 5.0
    for _ in range(50):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if mean_ratio(mid) > wr else (lo, mid)
    lam = (lo + hi) / 2
    return t, np.where(t < 3, 1 - p_skip * t / 3, s3 * np.exp(-lam * (t - 3)))

def score(manifest, tag=None, hour=19):
    bundle = pickle.load(open(MODEL, "rb")); P = bundle["personas"]
    rows, curves = [], {}
    for v in manifest["variants"]:
        x = {"duration_s": v["length_s"], "hour": hour, **bundle["common"]}
        if tag is not None: x["tag"] = str(tag)
        X = pd.DataFrame([x])
        for c in CATS: X[c] = pd.Categorical(X[c].astype(str), categories=bundle["cats"][c])
        tot, curves[v["variant_id"]] = 0.0, {}
        for p, m in P.items():
            ms, mw = heuristic_multiplier(v, m["hook_sensitivity"])
            wr = float(np.clip(m["reg"].predict(X[FEATS])[0] * mw, 0, 1.0))
            ps = float(np.clip(m["clf"].predict_proba(X[FEATS])[0, 1] * ms, 0, 1))
            t, s = retention_curve(v["length_s"], ps, wr)
            ret = float(np.mean(s))  # retention score = area under curve / length
            curves[v["variant_id"]][p] = (t.tolist(), s.tolist())
            rows.append(dict(variant_id=v["variant_id"], persona=p, persona_name=m["name"], weight=m["weight"],
                             pred_watch_ratio=wr, pred_skip3s=ps, retention_score=ret))
    df = pd.DataFrame(rows)
    df["w_ret"] = df.weight * df.retention_score
    rank = df.groupby("variant_id").agg(audience_score=("w_ret", "sum")).sort_values("audience_score", ascending=False)
    piv = df.pivot(index="variant_id", columns="persona", values="retention_score").add_prefix("persona_")
    rank = rank.join(piv); rank.insert(0, "rank", range(1, len(rank) + 1))
    return rank, df, curves, P
