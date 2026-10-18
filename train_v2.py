import json, time; t=time.time()
from clipcut import personas_v2 as P
o = P.train(k=4)
print(json.dumps({k: o[k] for k in ['n_videos','n_interactions','n_users_in_personas','persona_sizes','persona_desc','persona_engagement_heldout_videos','like_rate_heldout_videos']}, indent=1, default=float))
print(json.dumps(o['feature_importance_gain'], indent=1)); print('secs', time.time()-t)
