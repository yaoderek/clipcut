import numpy as np, pandas as pd, json
r=pd.read_csv('runs/demo/rankings.csv',index_col=0); rng=np.random.default_rng(1); met={}
for v,s in r['audience_score'].items():
    n=int(rng.integers(800,1500)); met[v]={"views":n,"avg_watch_pct":float(np.clip(rng.normal(s,0.04),0,1))*100,"shares":int(rng.binomial(n,0.01+0.03*s))}
# Simulated from predicted scores. Not platform data. _meta is ignored by cli.py ab.
payload={"_meta":{"simulated":True,"generator":"make_mock_metrics.py","note":"Simulated from audience_score. Not platform data."},**met}
json.dump(payload,open('runs/demo/mock_metrics.json','w'),indent=2)
