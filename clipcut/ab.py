"""A/B harness: Bayesian comparison of variants via Beta posteriors + Thompson sampling."""
import numpy as np

def analyze(metrics, n_draws=20000, w_watch=0.6, w_share=0.4, seed=0):
    """metrics: {variant_id: {views, avg_watch_pct (0-1 or 0-100), shares}}.
    Watch: Beta(1+views*w, 1+views*(1-w)) (pseudo-count approx). Share: Beta(1+shares, 1+views-shares).
    Combined score = w_watch*watch + w_share*share_rate_normalized. Returns P(best) per variant."""
    rng = np.random.default_rng(seed)
    ids = list(metrics)
    W, S = [], []
    for v in ids:
        m = metrics[v]; n = int(m["views"]); w = float(m["avg_watch_pct"]); w = w / 100 if w > 1 else w
        W.append(rng.beta(1 + n * w, 1 + n * (1 - w), n_draws))
        S.append(rng.beta(1 + m["shares"], 1 + n - m["shares"], n_draws))
    W, S = np.array(W), np.array(S)
    S_norm = S / max(S.mean(axis=1).max(), 1e-9)  # scale shares to ~[0,1] relative to best
    score = w_watch * W + w_share * S_norm
    best = score.argmax(axis=0)
    p_best = np.bincount(best, minlength=len(ids)) / n_draws
    # expected loss (regret) of choosing each variant
    loss = (score.max(axis=0) - score).mean(axis=1)
    rows = sorted([dict(variant_id=v, views=metrics[v]["views"], watch_mean=float(W[i].mean()),
                        share_rate=float(S[i].mean()), p_best=float(p_best[i]), expected_loss=float(loss[i]))
                   for i, v in enumerate(ids)], key=lambda r: -r["p_best"])
    return dict(winner=rows[0]["variant_id"], confident=rows[0]["p_best"] >= 0.95, table=rows)

def thompson_allocate(metrics, budget=1000, seed=0):
    """Next-round traffic split proportional to Thompson P(best)."""
    r = analyze(metrics, seed=seed)
    return {row["variant_id"]: int(round(budget * row["p_best"])) for row in r["table"]}
