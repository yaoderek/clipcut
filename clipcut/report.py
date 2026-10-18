import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, numpy as np

def plot_curves(rank, curves, P, path):
    top = list(rank.index[:4])
    fig, ax = plt.subplots(1, 2, figsize=(13, 5))
    for v in rank.index:  # audience-weighted curve per variant
        c = curves[v]; t = np.array(c[0][0]) if False else np.array(next(iter(c.values()))[0])
        s = sum(P[p]["weight"] * np.array(c[p][1]) for p in c)
        ax[0].plot(t, s, lw=2.5 if v == rank.index[0] else 1, label=f"{v} ({rank.loc[v,'audience_score']:.3f})")
    ax[0].set(title="Predicted audience retention (weighted personas)", xlabel="seconds", ylabel="share still watching", ylim=(0, 1.02))
    ax[0].legend(fontsize=8)
    w = rank.index[0]
    for p, (t, s) in curves[w].items():
        ax[1].plot(t, s, label=P[p]["name"][:40])
    ax[1].set(title=f"Winner {w}: per-persona curves", xlabel="seconds", ylim=(0, 1.02)); ax[1].legend(fontsize=7)
    fig.text(0.5, 0.005, "Base predictions: LightGBM on real KuaiRand-Pure logs. Hook/pacing/caption effects: heuristic prior (not learned).",
             ha="center", fontsize=8, style="italic")
    fig.tight_layout(rect=(0, 0.03, 1, 1)); fig.savefig(path, dpi=120); plt.close(fig)
