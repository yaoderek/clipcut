"""python train_retention.py -> models/retention/{retention.pkl,metrics.json}"""
import json, pickle, numpy as np, pandas as pd
from clipcut import retention_model as R
logs, vid, st = R.load(); print("logs", len(logs), logs.user_id.nunique(), logs.video_id.nunique(), flush=True)
lab, names, prof, sizes = R.personas(logs); print(names, sizes.tolist(), flush=True)
a = R.table(logs, lab, vid, st); print("video x persona rows", len(a), a.video_id.nunique(), flush=True)
res, _ = R.evaluate(a); print(json.dumps(res, indent=1), flush=True)
models = R.fit_final(a)
w = {names[p]: float(s / sizes.sum()) for p, s in sizes.items()}
try:
    V2 = pickle.load(open(R.ROOT / "models/personas_v2.pkl", "rb"))
    F = pd.read_json(R.ROOT / "data/microlens/features.jsonl", lines=True)
except Exception: F = None
pickle.dump(dict(models=models, names=names, weights=w, version=R.VERSION), open(R.MD / "retention.pkl", "wb"))
json.dump(dict(n_impressions=int(len(logs)), n_users=int(logs.user_id.nunique()), n_videos=int(logs.video_id.nunique()), n_rows=int(len(a)),
               n_videos_eval=int(a.video_id.nunique()), personas={names[p]: dict(size=int(sizes[p]), **prof.loc[p].round(4).to_dict()) for p in names},
               heldout_by_video=res), open(R.MD / "metrics.json", "w"), indent=1)
print("DONE")
