"""Score cutter-v2 variants with the v2 (edit-aware) persona model. Usage: python score_v2.py runs/v2_niosh"""
import sys, json, pickle, numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
from pathlib import Path
from clipcut import personas_v2 as P2, personas as P1
d = Path(sys.argv[1]); m = json.load(open(d / "manifest.json"))
rank, df, b = P2.score(d / "manifest.json")
feats = {v["variant_id"]: v for v in m["variants"]}
for c in ["length_s", "hook", "pacing", "captions", "hook_title", "ending", "cuts_per_10s"]:
    rank[c] = [feats[v][c] for v in rank.index]
rank["rank_like_rate"] = rank.pred_like_rate.rank(ascending=False).astype(int)
rank.round(4).to_csv(d / "rankings_v2.csv"); df.round(4).to_csv(d / "persona_scores_v2.csv", index=False)
print("Personas:", json.dumps(b["desc"], indent=1)); print(rank.round(3).to_string())
# chart: left = learned per-persona engagement (MicroLens); right = length-only retention curve from v1 KuaiRand model (real watch time)
r1, _, curves, PP = P1.score({"variants": [dict(variant_id=v, length_s=feats[v]["length_s"], hook_score=0, hook_zoom=False, caption_style="none",
                                                 jump_cuts=False, cuts_per_min=0) for v in rank.index]})
fig, ax = plt.subplots(1, 2, figsize=(14, 5.5))
piv = rank[[c for c in rank.columns if c.startswith("persona_")]]
piv.columns = [f"P{c.split('_')[1]}: {b['desc'][int(c.split('_')[1])][:38]}" for c in piv.columns]
piv.plot.bar(ax=ax[0], width=0.8); ax[0].set(title="v2: predicted engagement per persona (edit-aware, MicroLens)", ylabel="pred. interactions per video (expm1 scale)")
ax[0].legend(fontsize=7)
for v in rank.index:
    c = curves[v]; t = np.array(next(iter(c.values()))[0]); s = sum(PP[p]["weight"] * np.array(c[p][1]) for p in c)
    ax[1].plot(t, s, lw=2.5 if v == rank.index[0] else 1, label=f"{v} ({feats[v]['length_s']:.0f}s)")
ax[1].set(title="Retention curve: v1 KuaiRand model (LENGTH-ONLY, real watch time)", xlabel="seconds", ylim=(0, 1.02)); ax[1].legend(fontsize=8)
fig.text(0.5, 0.005, "Left: learned from real videos+interactions (no watch time in MicroLens). Right: no edit info; included only as a length prior.", ha="center", fontsize=8, style="italic")
fig.tight_layout(rect=(0, 0.03, 1, 1)); fig.savefig(d / "rankings_v2.png", dpi=120)
json.dump({"winner": rank.index[0], "variant": feats[rank.index[0]]}, open(d / "deploy_v2.json", "w"), indent=1)
print("DONE winner", rank.index[0])
