"""Score every per-variant manifest in a run dir and write ranked results.json. Usage: python rank_v2.py runs/X
Uses score_batch() (clipcut.scorer or clipcut.retention) when available for batch_score/batch_rank; else score() + local 0-100 rescale."""
import sys, json
from pathlib import Path
from clipcut.scorer import score
d = Path(sys.argv[1]); files = sorted(d.glob("v[0-9][0-9].json"))
batch = None
for mod in ("clipcut.scorer", "clipcut.retention"):
    try: batch = getattr(__import__(mod, fromlist=["x"]), "score_batch"); break
    except Exception: pass
res = batch([str(f) for f in files]) if batch else [score(str(f)) for f in files]
v = [r["overall"]["score"] for r in res]; lo, hi = min(v), max(v)
for f, s in zip(files, res):
    m = json.load(open(f)); s["recipe"] = m["recipe"]; s["duration_s"] = m["duration_s"]
    s.setdefault("batch_score", round(100 * (s["overall"]["score"] - lo) / (hi - lo), 1) if hi > lo else 50.0)
    s.setdefault("target_channel", m.get("channel")); print(s["variant_id"], s["overall"], s.get("batch_score"), flush=True)
res.sort(key=lambda s: -s["overall"]["score"])
for i, s in enumerate(res): s["rank"] = i + 1
json.dump(dict(winner=res[0]["variant_id"], batch_source="score_batch" if batch else "local", results=res), open(d / "results.json", "w"), indent=1)
print("WINNER", res[0]["variant_id"], res[0]["recipe"])
