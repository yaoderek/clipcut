"""Persona simulator v2: edit-aware content features (clipcut.features) on REAL MicroLens-100k videos + REAL user interactions.
Label reality check: MicroLens has NO watch time / retention. Per-user signal = interaction pairs (user commented on/engaged with
video). Per-video: likes & views. So targets here are ENGAGEMENT, not retention:
  - persona engagement:  log1p(#interactions from persona p with video i)   (rows = video x persona)
  - like_rate:           log(likes / views)                                 (video level)
Personas: users clustered by their *content-feature taste* (mean standardized features of videos they engaged with).
Evaluation: GroupKFold by VIDEO (unseen videos). Ablation: length-only vs +edit features vs +CLIP."""
import json, pickle, numpy as np, pandas as pd, lightgbm as lgb
from pathlib import Path
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.model_selection import GroupKFold
from sklearn.metrics import r2_score
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parent.parent; D = ROOT / "data" / "microlens"; MD = ROOT / "models"
EDIT = ["cuts_per_10s", "cuts_first3s", "mean_shot_len", "motion_mean", "motion_first3s", "motion_std", "face_frac_first3s",
        "face_area_first3s", "text_edge_lower", "text_edge_ratio", "text_blobs", "brightness", "contrast", "saturation",
        "hook_brightness_delta", "loudness_db", "loudness_std", "dead_air_frac", "spectral_flatness", "beat_regularity",
        "words_per_sec", "time_to_first_word", "speech_frac", "n_speech_gaps_per_10s", "music_score", "is_vertical"]
LEN = ["log_duration"]; NCLIP = 8
CLIPF = [f"clip{i}" for i in range(NCLIP)]
SETS = {"length_only": LEN, "length+edit": LEN + EDIT, "length+edit+clip": LEN + EDIT + CLIPF, "clip_only": LEN + CLIPF}
PRM = dict(n_estimators=200, learning_rate=0.04, num_leaves=15, min_child_samples=20, subsample=0.8, subsample_freq=1,
           colsample_bytree=0.8, reg_lambda=1.0, verbose=-1, n_jobs=1)

def load_features():
    rows = [json.loads(l) for l in open(D / "features.jsonl")]
    rows = [r for r in rows if "error" not in r and np.isfinite(r.get("duration_s", np.nan))]
    F = pd.DataFrame(rows).set_index("item")
    clip = np.array(F.pop("clip").tolist()); pca = PCA(NCLIP, random_state=0).fit(clip)
    F[CLIPF] = pca.transform(clip); F["log_duration"] = np.log1p(F.duration_s)
    return F, pca

def train(k=4, seed=0):
    F, pca = load_features()
    pairs = pd.read_csv(D / "MicroLens-100k_pairs.csv"); pairs = pairs[pairs.item.isin(F.index)]
    lv = pd.read_csv(D / "MicroLens-100k_likes_and_views.txt", sep="\t", header=None, names=["item", "likes", "views"]).set_index("item")
    print(f"[data] {len(F)} videos with features, {len(pairs):,} interactions, {pairs.user.nunique():,} users")
    # --- personas by content taste ---
    Z = (F[EDIT + LEN] - F[EDIT + LEN].mean()) / (F[EDIT + LEN].std() + 1e-9)
    ucnt = pairs.user.value_counts(); act = ucnt[ucnt >= 3].index
    up = pairs[pairs.user.isin(act)]
    taste = Z.loc[up.item].set_index(up.user.values).groupby(level=0).mean()
    km = KMeans(k, n_init=10, random_state=seed).fit(taste.values)
    persona = pd.Series(km.labels_, index=taste.index)
    pairs = pairs.assign(persona=pairs.user.map(persona)).dropna(subset=["persona"]); pairs["persona"] = pairs.persona.astype(int)
    psize = persona.value_counts().sort_index()
    cnt = pairs.groupby(["item", "persona"]).size().unstack(fill_value=0).reindex(index=F.index, columns=range(k), fill_value=0)
    # describe personas by their taste centroid (top distinguishing features)
    cent = pd.DataFrame(km.cluster_centers_, columns=taste.columns)
    desc = {p: ", ".join(f"{'+' if cent.loc[p, c] > 0 else '-'}{c}" for c in cent.loc[p].abs().sort_values(ascending=False).index[:4]) for p in range(k)}
    # long table video x persona
    L = cnt.stack().rename("n").reset_index().rename(columns={"level_0": "item"})
    L["y"] = np.log1p(L.n); L = L.join(F, on="item")
    # --- held-out-VIDEO evaluation, leakage-safe: personas are re-clustered inside each fold using TRAIN videos only ---
    items = F.index.values; res = {n: dict(pred=[], y=[], p=[]) for n in SETS}; res_base = dict(pred=[], y=[])
    for fold, (tri, tei) in enumerate(GroupKFold(5).split(items, groups=items)):
        tr_items, te_items = set(items[tri]), set(items[tei])
        upt = up[up.item.isin(tr_items)]; uc = upt.user.value_counts(); upt = upt[upt.user.isin(uc[uc >= 3].index)]
        mu, sd = F.loc[list(tr_items), EDIT + LEN].mean(), F.loc[list(tr_items), EDIT + LEN].std() + 1e-9
        tz = ((F[EDIT + LEN] - mu) / sd).loc[upt.item].set_index(upt.user.values).groupby(level=0).mean()
        lab = pd.Series(KMeans(k, n_init=10, random_state=seed).fit_predict(tz.values), index=tz.index)
        pp = pairs[["user", "item"]].assign(persona=pairs.user.map(lab)).dropna(); pp["persona"] = pp.persona.astype(int)
        c = pp.groupby(["item", "persona"]).size().unstack(fill_value=0).reindex(index=F.index, columns=range(k), fill_value=0)
        Lf = c.stack().rename("n").reset_index().rename(columns={"level_0": "item"}); Lf["y"] = np.log1p(Lf.n); Lf = Lf.join(F, on="item")
        tr, te = Lf[Lf.item.isin(tr_items)], Lf[Lf.item.isin(te_items)]
        for name, cols in SETS.items():
            m = lgb.LGBMRegressor(**PRM).fit(tr[cols + ["persona"]], tr.y, categorical_feature=["persona"])
            res[name]["pred"] += list(m.predict(te[cols + ["persona"]])); res[name]["y"] += list(te.y); res[name]["p"] += list(te.persona + 10 * fold)
        res_base["pred"] += list(te.persona.map(tr.groupby("persona").y.mean())); res_base["y"] += list(te.y)
        print(f"[fold {fold}] personas sizes {lab.value_counts().sort_index().tolist()}", flush=True)
    out_res = {}
    for name, r in res.items():
        y, pr, g = np.array(r["y"]), np.array(r["pred"]), np.array(r["p"])
        sp = [spearmanr(y[g == q], pr[g == q])[0] for q in np.unique(g) if (g == q).sum() > 10]
        out_res[name] = dict(R2=r2_score(y, pr), MAE=float(np.abs(y - pr).mean()), spearman_within_persona=float(np.nanmean(sp)))
    yb, pb = np.array(res_base["y"]), np.array(res_base["pred"])
    out_res["persona_mean_baseline"] = dict(R2=r2_score(yb, pb), MAE=float(np.abs(yb - pb).mean()), spearman_within_persona=0.0)
    res = out_res
    # final table for deployment (personas from all data)
    L = cnt.stack().rename("n").reset_index().rename(columns={"level_0": "item"}); L["y"] = np.log1p(L.n); L = L.join(F, on="item")
    # baseline: persona mean only
    # video-level like rate
    V = F.join(lv); V["like_rate"] = np.log(V.likes / V.views); vres = {}; vp = {}
    for name, cols in SETS.items():
        pred = np.zeros(len(V))
        for tr, te in GroupKFold(5).split(V, groups=V.index):
            pred[te] = lgb.LGBMRegressor(**PRM).fit(V.iloc[tr][cols], V.like_rate.iloc[tr]).predict(V.iloc[te][cols])
        vres[name] = dict(R2=r2_score(V.like_rate, pred), spearman=spearmanr(V.like_rate, pred)[0]); vp[name] = pred
    # paired bootstrap over videos: does adding edit features beat length-only?
    rng = np.random.default_rng(0); y = V.like_rate.values; diffs = []
    for _ in range(500):
        i = rng.integers(0, len(y), len(y))
        diffs.append(spearmanr(y[i], vp["length+edit"][i])[0] - spearmanr(y[i], vp["length_only"][i])[0])
    vres["bootstrap_edit_minus_length_spearman"] = dict(mean=float(np.mean(diffs)), ci95=[float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))])
    like_model = lgb.LGBMRegressor(**PRM).fit(V[SETS["length+edit+clip"]], V.like_rate)
    # final model on all data + importance
    cols = SETS["length+edit+clip"]
    final = lgb.LGBMRegressor(**PRM).fit(L[cols + ["persona"]], L.y, categorical_feature=["persona"])
    imp = pd.Series(final.booster_.feature_importance("gain"), index=cols + ["persona"]).sort_values(ascending=False)
    imp = (imp / imp.sum()).round(4)
    # permutation-style group importance on held-out fold: shuffle edit block
    MD.mkdir(exist_ok=True)
    pickle.dump(dict(model=final, like_model=like_model, pca=pca, cols=cols, k=k, weights=(psize / psize.sum()).to_dict(), desc=desc,
                     feat_mean=F[cols].mean().to_dict()), open(MD / "personas_v2.pkl", "wb"))
    out = dict(n_videos=len(F), n_interactions=int(len(pairs)), n_users_in_personas=int(len(persona)), persona_sizes=psize.to_dict(),
               persona_desc=desc, persona_engagement_heldout_videos=res, like_rate_heldout_videos=vres, feature_importance_gain=imp.head(20).to_dict())
    json.dump(out, open(MD / "metrics_v2.json", "w"), indent=1, default=float)
    return out

def score(manifest_path, feats_cache=True):
    """Extract the SAME features from each rendered variant and score per persona."""
    from .features import extract
    b = pickle.load(open(MD / "personas_v2.pkl", "rb")); m = json.load(open(manifest_path)); rows = []
    for v in m["variants"]:
        f = extract(v["file"]); clip = np.array(f.pop("clip"))[None]
        f.update(dict(zip(CLIPF, b["pca"].transform(clip)[0]))); f["log_duration"] = np.log1p(f["duration_s"])
        Xl = pd.DataFrame([{c: f[c] for c in b["cols"]}]); like = float(np.exp(b["like_model"].predict(Xl)[0]))
        for p in range(b["k"]):
            X = pd.DataFrame([{c: f[c] for c in b["cols"]} | {"persona": p}]); X["persona"] = X.persona.astype(int)
            rows.append(dict(variant_id=v["variant_id"], persona=p, weight=b["weights"][p], pred_like_rate=like, pred=float(np.expm1(b["model"].predict(X)[0])),
                             **{c: f[c] for c in ["cuts_per_10s", "text_edge_lower", "text_blobs", "face_frac_first3s", "time_to_first_word", "dead_air_frac", "words_per_sec", "motion_mean"]}))
    df = pd.DataFrame(rows); df["w"] = df.weight * df.pred
    rank = df.groupby("variant_id").w.sum().rename("audience_pred_engagement").sort_values(ascending=False).to_frame()
    rank = rank.join(df.groupby("variant_id").pred_like_rate.first())
    rank = rank.join(df.pivot(index="variant_id", columns="persona", values="pred").add_prefix("persona_"))
    rank.insert(0, "rank", range(1, len(rank) + 1))
    return rank, df, b
